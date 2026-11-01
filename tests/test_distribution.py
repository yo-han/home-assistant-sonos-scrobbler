"""What HACS and Home Assistant read from the repository, on the real files."""

import json
from pathlib import Path
from string import Formatter
from typing import Any

import pytest
from PIL import Image

from scripts.build_package import COMPONENT

ROOT = Path(__file__).parents[1]
BRAND = ROOT / COMPONENT / "brand"


def load(path: Path) -> Any:
    return json.loads(path.read_text())


def key_paths(value: Any, prefix: str = "") -> set[str]:
    if not isinstance(value, dict):
        return {prefix}
    return {
        path
        for key, item in value.items()
        for path in key_paths(item, f"{prefix}.{key}")
    }


def test_hacs_installs_the_component_from_a_release_only() -> None:
    assert load(ROOT / "hacs.json") == {
        "name": "Personal Sonos Scrobbler",
        "homeassistant": "2026.10.4",
        "hide_default_branch": True,
    }


def test_the_hacs_floor_is_the_tested_home_assistant() -> None:
    project = (ROOT / "pyproject.toml").read_text()

    assert f'"homeassistant=={load(ROOT / "hacs.json")["homeassistant"]}"' in project


def test_english_translations_are_the_strings_file() -> None:
    strings = load(ROOT / COMPONENT / "strings.json")

    assert load(ROOT / COMPONENT / "translations" / "en.json") == strings


def test_dutch_translates_every_key() -> None:
    strings = load(ROOT / COMPONENT / "strings.json")
    dutch = load(ROOT / COMPONENT / "translations" / "nl.json")

    assert key_paths(dutch) == key_paths(strings)


@pytest.mark.parametrize(("name", "size"), [("icon.png", 256), ("icon@2x.png", 512)])
def test_brand_icons_are_transparent_pngs_of_the_exact_size(
    name: str, size: int
) -> None:
    with Image.open(BRAND / name) as image:
        assert image.format == "PNG"
        assert image.size == (size, size)
        alpha = image.convert("RGBA").getchannel("A")

    assert alpha.getextrema() == (0, 255)
    assert alpha.getpixel((0, 0)) == 0
    assert (BRAND / name).stat().st_size < 20_000


def test_the_editable_icon_source_stays_out_of_the_component() -> None:
    assert (ROOT / "assets" / "brand" / "icon.svg").is_file()
    assert [path.name for path in BRAND.iterdir() if path.suffix != ".png"] == []


def leaves(value: Any, prefix: str = "") -> dict[str, str]:
    if isinstance(value, str):
        return {prefix: value}
    return {
        path: text
        for key, item in value.items()
        for path, text in leaves(item, f"{prefix}.{key}").items()
    }


def placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in Formatter().parse(text) if name}


def test_both_languages_ask_for_the_same_placeholders() -> None:
    english = leaves(load(ROOT / COMPONENT / "translations" / "en.json"))
    dutch = leaves(load(ROOT / COMPONENT / "translations" / "nl.json"))

    assert all(text.strip() for text in (*english.values(), *dutch.values()))
    assert {path: placeholders(text) for path, text in dutch.items()} == {
        path: placeholders(text) for path, text in english.items()
    }


@pytest.mark.parametrize("language", ["en", "nl"])
def test_no_translation_calls_this_a_pilot(language: str) -> None:
    translations = leaves(load(ROOT / COMPONENT / "translations" / f"{language}.json"))

    assert [
        path for path, text in translations.items() if "pilot" in text.lower()
    ] == []
