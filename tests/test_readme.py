"""The README and developer guide state consent, privacy, recovery and live limits."""

from pathlib import Path

ROOT = Path(__file__).parents[1]
README = (ROOT / "README.md").read_text()
NORMALIZED = " ".join(README.split())
DEVELOPMENT = " ".join((ROOT / "docs" / "development.md").read_text().split())


def test_readme_leads_with_native_delivery_and_default_off() -> None:
    opening = " ".join(README.splitlines()[:15])
    assert "Home Assistant" in opening
    assert "Delivery is off by default" in opening
    assert "Linking an account alone does not send listening data" in opening
    for heading in (
        "Installation",
        "Account linking",
        "Delivery and overlap",
        "Local storage and status",
        "Removal",
        "Reset behavior",
        "Spotify evidence",
        "Limits",
    ):
        assert f"## {heading}" in README


def test_guide_distinguishes_local_checks_from_live_validation() -> None:
    for text in (
        "turning scrobbling on against the real Last.fm API has not been tested",
        "one selected Sonos player",
        "Other devices and playback",
        "have not been validated",
        "completed Last.fm linking on 0.2.2",
        "this overlap is why 0.4.0 made Now Playing the only sender",
        "that attribution is inferred",
        "No history read is needed with scrobbling off",
        "does not prove that the switches gate anything",
        "passing simulation proves the exercised stand-in scenarios",
    ):
        assert text in DEVELOPMENT, text


def test_readme_states_the_linking_privacy_and_limits() -> None:
    for text in (
        "optional and sends no listening data",
        "The browser link necessarily holds the API key and a one-use token",
        "The shared secret and session key are never in a URL",
        "pending values are dropped",
        "https://www.last.fm/api/account/create",
        "Your Last.fm password is never needed",
        "confirm the returned account",
        "does not revoke",
        "not encrypted",
        "backups",
        "API requests use a form POST",
        "`lastfm_linked` means confirmed credentials are stored locally",
        "does not itself check that the account still works",
    ):
        assert text in NORMALIZED, text


def test_readme_states_delivery_recovery_and_overlap_limits() -> None:
    for text in (
        "existing Spotify → Last.fm connection is untouched",
        "No historical observation candidates are backfilled",
        "never automatically reposted",
        "does **not** provide exactly-once",
        "independent late Spotify write",
        "1,000 items",
        "seven-day deadline",
        "14 days and 10,000 rows",
        "missing or damaged ledger is never silently",
        "returning to 0.2.2 requires the earlier backup",
        "Muted playback",
        "not perfectly distinguishable",
    ):
        assert text in NORMALIZED, text


def test_readme_states_what_now_playing_sends_and_cannot_do() -> None:
    for text in (
        "transmits the current Spotify track to Last.fm immediately",
        "While the Now Playing switch is on",
        "no effect on your history or charts",
        "A failed notice is never retried",
        "cannot instantly clear what Last.fm already shows",
    ):
        assert text in NORMALIZED, text
    assert "no Now Playing" not in NORMALIZED


def test_readme_states_the_switches_and_their_limits() -> None:
    for text in (
        "two ordinary switches, without a confirmation form",
        "That overlap remains possible if you turn scrobbling on while Spotify also",
        "Upgrading never turns scrobbling on",
        "**Scrobbling is future-only.**",
        "The track already playing is not backfilled",
        "off followed by on starts a new period",
        "they cause no `user.getRecentTracks` or `track.scrobble` request",
        "a request already on the wire cannot be recalled",
        "Upgrading from 0.4.0 keeps Now Playing as it was and leaves scrobbling off",
        "A new entry starts with both switches off",
        "relinking turns neither on",
        "the new period starts just after the newest queued play",
    ):
        assert text in NORMALIZED, text


def test_readme_states_what_the_sensor_shows_and_where_it_ends_up() -> None:
    for text in (
        "works with both switches off or without a linked account",
        "255-character state limit",
        "Nothing is restored after a restart",
        "Recorder stores this sensor's state and attributes",
        "does not change your Recorder configuration",
        "Only the current track sensor shows music",
    ):
        assert text in NORMALIZED, text


def test_readme_names_the_current_package(release_version: str) -> None:
    assert f"personal_sonos_scrobbler-{release_version}.zip" in README
    assert "0.1.0.zip" not in README
