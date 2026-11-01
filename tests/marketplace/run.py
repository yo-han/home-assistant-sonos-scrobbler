"""Run isolated Marketplace install and persisted HACS migration checks.

Only public GitHub fixtures are downloaded. HA runs with an internal Docker network,
no ports, no host socket and no production volume. All input credentials are fake.
"""

import argparse
import base64
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOMAIN = "personal_sonos_scrobbler"
REPOSITORY = "yo-han/home-assistant-sonos-scrobbler"
REPOSITORY_ID = "1442063425"
METADATA_REF = "v0.8.1"  # A published fixture ref, independent of candidate version.
IMAGES = {
    "stable": (
        "ghcr.io/home-assistant/home-assistant:2026.10.4@sha256:"
        "23f4dfbb69ce76315dbc4de7915504f1c2dcecd7ca2648113a493a385972e133"
    ),
    "beta": (
        "ghcr.io/home-assistant/home-assistant:2026.11.0b1@sha256:"
        "0480fba9b81b3cda8226c3054b5bf4aa52589735ceb17f23ba47ddbf68790875"
    ),
}


def read(path):
    return json.loads(path.read_text())


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def fetch(url):
    headers = {"User-Agent": "Sonos-Scrobbler-Compatibility-Test"}
    if url.startswith("https://api.github.com/") and (
        token := os.environ.get("GH_TOKEN")
    ):
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def prepare(fixtures, published_release=None):
    fixture_ref = published_release or METADATA_REF
    fixtures.mkdir(parents=True)
    paths = [
        ROOT / "hacs.json",
        ROOT / "README.md",
        *sorted((ROOT / "custom_components" / DOMAIN).rglob("*")),
    ]
    paths = [
        path for path in paths if path.is_file() and "__pycache__" not in path.parts
    ]
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        for path in paths:
            output.writestr(
                "candidate/" + str(path.relative_to(ROOT)), path.read_bytes()
            )
    archive_url = f"https://github.com/{REPOSITORY}/archive/refs/tags/{fixture_ref}.zip"
    (fixtures / "candidate.zip").write_bytes(
        fetch(archive_url) if published_release else archive.getvalue()
    )
    write(
        fixtures / "component-hashes.json",
        {
            str(path.relative_to(ROOT / "custom_components" / DOMAIN)): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in paths
            if path.is_relative_to(ROOT / "custom_components" / DOMAIN)
        },
    )
    cache = {}
    base = f"https://api.github.com/repos/{REPOSITORY}"
    for number, suffix in enumerate(
        (
            "",
            "/releases",
            f"/git/trees/{fixture_ref}",
            f"/commits/{fixture_ref}",
            "/branches/main",
        )
    ):
        url = base + suffix
        payload = fetch(url + ("?recursive=1" if "/trees/" in suffix else ""))
        filename = f"github-{number}.json"
        (fixtures / filename).write_bytes(payload)
        cache[url.removeprefix("https://")] = {
            "file": filename,
            "content_type": "application/json",
        }
    for path in paths:
        relative = str(path.relative_to(ROOT))
        filename = "file-" + hashlib.sha256(relative.encode()).hexdigest()
        (fixtures / filename).write_bytes(path.read_bytes())
        for ref in (fixture_ref, f"tags/{fixture_ref}", "main"):
            cache[f"raw.githubusercontent.com/{REPOSITORY}/{ref}/{relative}"] = {
                "file": filename,
                "content_type": "text/plain",
            }
        encoded = {
            "name": path.name,
            "path": relative,
            "type": "file",
            "encoding": "base64",
            "content": base64.b64encode(path.read_bytes()).decode(),
        }
        json_filename = filename + ".json"
        write(fixtures / json_filename, encoded)
        cache[f"api.github.com/repos/{REPOSITORY}/contents/{relative}"] = {
            "file": json_filename,
            "content_type": "application/json",
        }
    cache[archive_url.removeprefix("https://")] = {
        "file": "candidate.zip",
        "content_type": "application/zip",
    }
    # This test models a connected account, not anonymous GitHub access.
    core_limit = {
        "limit": 5000,
        "remaining": 4999,
        "used": 1,
        "reset": int(time.time()) + 3600,
    }
    write(
        fixtures / "rate-limit.json",
        {"resources": {"core": core_limit}, "rate": core_limit},
    )
    cache["api.github.com/rate_limit"] = {
        "file": "rate-limit.json",
        "content_type": "application/json",
    }
    write(fixtures / "responses.json", cache)
    # A real HACS distribution is present before the core loader hands it over.
    hacs = json.loads(
        fetch("https://api.github.com/repos/hacs/integration/releases/tags/2.0.5")
    )
    asset = next(asset for asset in hacs["assets"] if asset["name"] == "hacs.zip")
    payload = fetch(asset["browser_download_url"])
    if hashlib.sha256(payload).hexdigest() != (
        "97be6b824a4f38e683728cc6dd72367f6b8bad0a43428b1b3b987a3087adf413"
    ):
        raise ValueError("Unexpected HACS 2.0.5 archive checksum")
    (fixtures / "hacs.zip").write_bytes(payload)
    write(
        fixtures / "provenance.json",
        {
            "github_metadata_ref": fixture_ref,
            "published_archive": bool(published_release),
            "candidate_component_sha256": read(fixtures / "component-hashes.json"),
            "hacs_release": hacs["tag_name"],
            "hacs_zip_sha256": hashlib.sha256(payload).hexdigest(),
            "images": IMAGES,
            "limits": [
                "GitHub metadata replayed offline; OAuth and catalog acceptance "
                "are not tested",
                (
                    "Archive downloaded anonymously from the published GitHub tag"
                    if published_release
                    else "Candidate archive built from this checkout, "
                    "not a published release"
                ),
                "HACS storage/config fixtures seeded after a real 2026.10.4 run",
                "Rate limit is synthetic and models a connected GitHub account",
                "No real speakers or Last.fm requests",
            ],
        },
    )


def docker_run(phase, image, config, fixtures, output, network):
    config.mkdir(exist_ok=True)
    container = "sonos-marketplace-" + phase + "-" + uuid.uuid4().hex[:10]
    command = [
        "docker",
        "run",
        "--rm",
        "--name",
        container,
        # Native Linux bind mounts keep the container's numeric owner. Keep HA's
        # private storage readable/writable by the host runner between phases.
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--memory",
        "1g",
        "--cpus",
        "2",
        "--network",
        network,
        "--read-only",
        "--tmpfs",
        "/tmp:rw,size=256m",
        "--tmpfs",
        "/run:rw,size=32m",
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        "--env",
        "TZ=UTC",
        "--mount",
        f"type=bind,src={ROOT},dst=/workspace,readonly",
        "--mount",
        f"type=bind,src={fixtures},dst=/fixtures,readonly",
        "--mount",
        f"type=bind,src={config},dst=/config",
        "--entrypoint",
        "python",
        image,
        "/workspace/tests/marketplace/runtime.py",
        phase,
    ]
    try:
        with (output / f"{phase}.log").open("w") as log:
            result = subprocess.run(
                command, stdout=log, stderr=subprocess.STDOUT, timeout=240
            )
    finally:
        subprocess.run(
            ["docker", "rm", "--force", container], check=False, capture_output=True
        )
    if result.returncode:
        tail = (output / f"{phase}.log").read_text().splitlines()[-40:]
        raise RuntimeError(f"{phase} failed ({result.returncode}):\n" + "\n".join(tail))
    report = read(config / f"{phase}-report.json")
    sys.stdout.write(
        json.dumps({key: report[key] for key in ("phase", "homeassistant", "checks")})
        + "\n"
    )
    sys.stdout.flush()


def seed_hacs(config, fixtures):
    fixture_ref = read(fixtures / "provenance.json")["github_metadata_ref"]
    before = read(config / "seed-report.json")["before"]
    entries_file = config / ".storage" / "core.config_entries"
    entries = read(entries_file)
    template = next(
        entry for entry in entries["data"]["entries"] if entry["domain"] == DOMAIN
    )
    hacs_id = uuid.uuid4().hex
    entries["data"]["entries"].append(
        {
            **template,
            "domain": "hacs",
            "entry_id": hacs_id,
            "title": "HACS",
            "unique_id": "hacs",
            "version": 1,
            "minor_version": 1,
            "data": {"token": "synthetic-github-connection"},
            "options": {},
            "disabled_by": None,
        }
    )
    write(entries_file, entries)
    entity_file = config / ".storage" / "core.entity_registry"
    registry = read(entity_file)
    entity = next(
        entity
        for entity in registry["data"]["entities"]
        if entity["config_entry_id"] == before["entry_id"]
    )
    update_id = "update.home_assistant_sonos_scrobbler_update"
    registry["data"]["entities"].append(
        {
            **entity,
            "id": uuid.uuid4().hex,
            "entity_id": update_id,
            "platform": "hacs",
            "unique_id": REPOSITORY_ID,
            "config_entry_id": hacs_id,
            "device_id": None,
            "name": None,
            "original_name": "Home Assistant Sonos Scrobbler update",
            "original_device_class": None,
            "capabilities": {},
            "options": {},
        }
    )
    write(entity_file, registry)
    for key, data in {
        "hacs.hacs": {"custom_repositories": [REPOSITORY_ID]},
        "hacs.repositories": {
            REPOSITORY_ID: {
                "full_name": REPOSITORY,
                "category": "integration",
                "domain": DOMAIN,
                "installed": True,
                "version_installed": fixture_ref,
                "last_version": fixture_ref,
                "config_flow": True,
                "releases": True,
                "manifest": read(ROOT / "hacs.json"),
            }
        },
        "hacs.critical": [],
    }.items():
        write(config / ".storage" / key, {"version": "6", "key": key, "data": data})
    hacs_dir = config / "custom_components" / "hacs"
    hacs_dir.mkdir()
    with zipfile.ZipFile(fixtures / "hacs.zip") as archive:
        archive.extractall(hacs_dir)
    write(
        config / "before.json",
        {**before, "hacs_entry_id": hacs_id, "update_entity_id": update_id},
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="New, disposable artifact directory; must not exist",
    )
    parser.add_argument(
        "--published-release",
        metavar="TAG",
        help="Download the real public source archive of this stable release",
    )
    args = parser.parse_args()
    if args.published_release and not re.fullmatch(
        r"v[0-9]+\.[0-9]+\.[0-9]+", args.published_release
    ):
        parser.error("--published-release must be a stable tag such as v0.8.2")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    fixtures = output / "fixtures"
    prepare(fixtures, args.published_release)
    network = "sonos-marketplace-" + uuid.uuid4().hex[:12]
    subprocess.run(
        ["docker", "network", "create", "--internal", network],
        check=True,
        capture_output=True,
    )
    try:
        docker_run(
            "clean", IMAGES["beta"], output / "clean-config", fixtures, output, network
        )
        upgrade = output / "upgrade-config"
        (upgrade / "custom_components").mkdir(parents=True)
        shutil.copytree(
            ROOT / "custom_components" / DOMAIN,
            upgrade / "custom_components" / DOMAIN,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        docker_run("seed", IMAGES["stable"], upgrade, fixtures, output, network)
        seed_hacs(upgrade, fixtures)
        docker_run("migrated", IMAGES["beta"], upgrade, fixtures, output, network)
    finally:
        subprocess.run(
            ["docker", "network", "rm", network], check=True, capture_output=True
        )
    write(
        output / "verification.json",
        {
            "passed": True,
            "phases": [
                read(output / "clean-config" / "clean-report.json"),
                read(upgrade / "seed-report.json"),
                read(upgrade / "migrated-report.json"),
            ],
            "provenance": read(fixtures / "provenance.json"),
        },
    )


if __name__ == "__main__":
    main()
