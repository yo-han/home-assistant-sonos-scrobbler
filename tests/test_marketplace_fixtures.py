"""Marketplace metadata must remain coherent when later releases are published."""

import json
from pathlib import Path

import pytest

from tests.marketplace import run as runner


@pytest.mark.parametrize("fixture_ref", [runner.METADATA_REF, "v1.2.3"])
def test_replayed_release_has_a_recorded_tree_after_a_newer_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture_ref: str
) -> None:
    base = f"https://api.github.com/repos/{runner.REPOSITORY}"
    selected_release = {
        "tag_name": fixture_ref,
        "draft": False,
        "prerelease": False,
        "assets": [{"name": "package.zip"}],
    }
    newer_release = {**selected_release, "tag_name": "v99.0.0"}
    payloads = {
        base: {"default_branch": "main"},
        f"{base}/releases": [newer_release, selected_release],
        f"{base}/releases/tags/{fixture_ref}": selected_release,
        f"{base}/git/trees/{fixture_ref}?recursive=1": {"tree": []},
        f"{base}/commits/{fixture_ref}": {"sha": "a" * 40},
        f"{base}/branches/main": {"name": "main"},
    }

    def fake_fetch(url: str) -> bytes:
        return json.dumps(payloads[url]).encode()

    monkeypatch.setattr(runner, "fetch", fake_fetch)
    cache = runner.prepare_github_metadata(tmp_path, fixture_ref)
    releases_item = cache[f"api.github.com/repos/{runner.REPOSITORY}/releases"]
    releases = json.loads((tmp_path / releases_item["file"]).read_text())

    assert releases == [selected_release]
    for release in releases:
        tree_key = (
            f"api.github.com/repos/{runner.REPOSITORY}/git/trees/{release['tag_name']}"
        )
        assert tree_key in cache
        assert json.loads((tmp_path / cache[tree_key]["file"]).read_text()) == {
            "tree": []
        }
