"""The installable ZIP is reproducible and holds runtime files only."""

import hashlib
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

from scripts.build_package import (
    COMPONENT,
    PACKAGE_FILES,
    PackageError,
    build_package,
    check_package,
    package_name,
)

ROOT = Path(__file__).parents[1]


def test_build_is_byte_identical(tmp_path: Path, release_version: str) -> None:
    first = build_package(ROOT, tmp_path / "first")
    second = build_package(ROOT, tmp_path / "second")

    assert first.name == f"personal_sonos_scrobbler-{release_version}.zip"
    assert first.read_bytes() == second.read_bytes()


def test_package_contains_only_allowlist(tmp_path: Path) -> None:
    archive = build_package(ROOT, tmp_path)

    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()

    assert names == sorted(PACKAGE_FILES)
    assert len(names) == 25
    assert {
        f"{COMPONENT}/brand/icon.png",
        f"{COMPONENT}/brand/icon@2x.png",
        f"{COMPONENT}/translations/en.json",
        f"{COMPONENT}/translations/nl.json",
    } <= set(names)
    assert not any(
        name.endswith((".sqlite3", ".db"))
        or any(part in {"tests", ".venv", "__pycache__"} for part in Path(name).parts)
        for name in names
    )
    assert not [name for name in names if "strings.json" in name]
    check_package(archive, ROOT)


def test_package_holds_every_runtime_file_of_the_component() -> None:
    component = ROOT / COMPONENT
    present = {
        path.relative_to(ROOT).as_posix()
        for path in component.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }

    assert present - set(PACKAGE_FILES) == {f"{COMPONENT}/strings.json"}


def test_unpacked_bytes_match_the_repository(tmp_path: Path) -> None:
    archive = build_package(ROOT, tmp_path)

    with zipfile.ZipFile(archive) as bundle:
        for name in PACKAGE_FILES:
            assert bundle.read(name) == (ROOT / name).read_bytes(), name


def test_project_version_matches_the_manifest() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]

    assert build_name_version() == project["version"]


def test_unpacked_package_imports_without_the_repository(tmp_path: Path) -> None:
    archive = build_package(ROOT, tmp_path / "dist")
    install = tmp_path / "config"
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(install)

    imported = subprocess.run(
        [
            sys.executable,
            "-c",
            "import custom_components.personal_sonos_scrobbler.config_flow as flow;"
            "print(flow.__file__)",
        ],
        cwd=install,
        env={"PYTHONPATH": str(install)},
        capture_output=True,
        text=True,
        check=True,
    )

    assert imported.stdout.startswith(str(install))


def build_name_version() -> str:
    return (
        package_name(ROOT)
        .removeprefix("personal_sonos_scrobbler-")
        .removesuffix(".zip")
    )


def test_check_rejects_foreign_file(tmp_path: Path) -> None:
    archive = build_package(ROOT, tmp_path)
    with zipfile.ZipFile(archive, "a") as bundle:
        bundle.writestr("custom_components/personal_sonos_scrobbler/.env", "x")

    with pytest.raises(PackageError, match="unexpected"):
        check_package(archive, ROOT)


def test_check_rejects_a_changed_brand_icon(tmp_path: Path) -> None:
    source = tmp_path / "source"
    for name in PACKAGE_FILES:
        (source / name).parent.mkdir(parents=True, exist_ok=True)
        (source / name).write_bytes((ROOT / name).read_bytes())
    archive = build_package(source, tmp_path / "dist")
    icon = source / COMPONENT / "brand" / "icon.png"
    icon.write_bytes(icon.read_bytes() + b"\0")

    with pytest.raises(PackageError, match="content"):
        check_package(archive, source)


def test_check_rejects_missing_checksum(tmp_path: Path) -> None:
    archive = build_package(ROOT, tmp_path)
    archive.with_name(f"{archive.name}.sha256").unlink()

    with pytest.raises(PackageError, match="checksum"):
        check_package(archive, ROOT)


def test_check_rejects_tampered_archive(tmp_path: Path) -> None:
    archive = build_package(ROOT, tmp_path)
    archive.write_bytes(archive.read_bytes() + b"\0")

    with pytest.raises(PackageError, match="checksum"):
        check_package(archive, ROOT)


def test_checksum_matches(tmp_path: Path) -> None:
    archive = build_package(ROOT, tmp_path)

    digest, name = archive.with_name(f"{archive.name}.sha256").read_text().split()

    assert digest == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert name == archive.name


def test_check_rejects_modified_content_even_with_matching_checksum(
    tmp_path: Path,
) -> None:
    archive = build_package(ROOT, tmp_path)
    with zipfile.ZipFile(archive) as bundle:
        entries = [(entry, bundle.read(entry)) for entry in bundle.infolist()]
    with zipfile.ZipFile(archive, "w") as bundle:
        for entry, data in entries:
            if entry.filename.endswith("/const.py"):
                data += b"\nimport aiohttp\n"
            bundle.writestr(entry, data)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_name(f"{archive.name}.sha256").write_text(
        f"{digest}  {archive.name}\n"
    )
    with pytest.raises(PackageError, match="content"):
        check_package(archive, ROOT)
