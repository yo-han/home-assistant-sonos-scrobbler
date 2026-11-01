"""Build and check the deterministic ZIP that installs the integration."""

import argparse
import hashlib
import json
import zipfile
from pathlib import Path

COMPONENT = "custom_components/personal_sonos_scrobbler"
PACKAGE_FILES = tuple(
    f"{COMPONENT}/{name}"
    for name in (
        "__init__.py",
        "brand/icon.png",
        "brand/icon@2x.png",
        "config_flow.py",
        "const.py",
        "controls.py",
        "delivery.py",
        "diagnostics.py",
        "lastfm.py",
        "lastfm_delivery.py",
        "lastfm_flow.py",
        "manifest.json",
        "normalize.py",
        "now_playing.py",
        "observation.py",
        "outbox.py",
        "playback.py",
        "runtime.py",
        "sensor.py",
        "speaker_controls.py",
        "streams.py",
        "switch.py",
        "topology.py",
        "translations/en.json",
        "translations/nl.json",
    )
)
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
FILE_MODE = 0o644 << 16
UNIX_SYSTEM = 3
COMPRESSION_LEVEL = 9


class PackageError(ValueError):
    """The archive differs from what may be installed."""


def package_name(root: Path) -> str:
    """Return the archive name for the version in the manifest."""
    manifest = json.loads((root / COMPONENT / "manifest.json").read_text())
    return f"personal_sonos_scrobbler-{manifest['version']}.zip"


def build_package(root: Path, output_dir: Path) -> Path:
    """Write the archive and its checksum, returning the archive path."""
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / package_name(root)
    with zipfile.ZipFile(archive, "w") as bundle:
        for name in sorted(PACKAGE_FILES):
            entry = zipfile.ZipInfo(name, date_time=ZIP_EPOCH)
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.create_system = UNIX_SYSTEM
            entry.external_attr = FILE_MODE
            bundle.writestr(
                entry, (root / name).read_bytes(), compresslevel=COMPRESSION_LEVEL
            )
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    _checksum_path(archive).write_text(f"{digest}  {archive.name}\n")
    return archive


def check_package(zip_path: Path, root: Path) -> None:
    """Raise PackageError unless the archive is exactly what build_package wrote."""
    _check_contents(zip_path, root)
    _check_checksum(zip_path)


def _checksum_path(archive: Path) -> Path:
    return archive.with_name(f"{archive.name}.sha256")


def _check_contents(zip_path: Path, root: Path) -> None:
    try:
        with zipfile.ZipFile(zip_path) as bundle:
            entries = bundle.infolist()
            names = [entry.filename for entry in entries]
            if names != sorted(PACKAGE_FILES):
                difference = sorted(set(names) ^ set(PACKAGE_FILES))
                raise PackageError(f"unexpected package contents: {difference}")
            for entry in entries:
                if entry.date_time != ZIP_EPOCH or entry.external_attr != FILE_MODE:
                    raise PackageError(f"unexpected metadata on {entry.filename}")
                if bundle.read(entry) != (root / entry.filename).read_bytes():
                    raise PackageError(f"content mismatch for {entry.filename}")
    except zipfile.BadZipFile as error:
        raise PackageError(f"{zip_path.name} is not a ZIP archive") from error


def _check_checksum(zip_path: Path) -> None:
    checksum_file = _checksum_path(zip_path)
    if not checksum_file.is_file():
        raise PackageError(f"missing checksum file for {zip_path.name}")
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    if checksum_file.read_text() != f"{digest}  {zip_path.name}\n":
        raise PackageError(f"checksum mismatch for {zip_path.name}")


def main() -> None:
    """Build or check the package in dist/."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "check"))
    command = parser.parse_args().command

    root = Path(__file__).resolve().parents[1]
    if command == "build":
        build_package(root, root / "dist")
    else:
        check_package(root / "dist" / package_name(root), root)


if __name__ == "__main__":
    main()
