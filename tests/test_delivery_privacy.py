"""Delivery status exposes no listening data, whatever the provider or storage did."""

import json
import logging
import re
from pathlib import Path

import pytest
from homeassistant.helpers import entity_registry as er

from custom_components.personal_sonos_scrobbler.const import DOMAIN
from custom_components.personal_sonos_scrobbler.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.personal_sonos_scrobbler.lastfm_delivery import ScrobbleOutcome
from custom_components.personal_sonos_scrobbler.outbox import OutboxError
from tests.delivery_harness import RECENT, SCROBBLE, STARTED, Pilot, accepted, history
from tests.lastfm_server import Reply, json_reply
from tests.outbox_support import fail_on

ROOT = Path(__file__).parents[1]
COMPONENT = ROOT / "custom_components" / DOMAIN
CANARIES = ("canary-artist", "canary-title", "canary-album", "canary-provider")
CHANGE_PLAY = "UPDATE plays SET state = 'sent'"


def _ignored() -> Reply:
    return json_reply(
        {
            "scrobbles": {
                "@attr": {"accepted": 0, "ignored": 1},
                "scrobble": {
                    "timestamp": str(STARTED),
                    "ignoredMessage": {"code": "1", "#text": "canary-provider-text"},
                },
            }
        }
    )


# Every ScrobbleOutcome, each with the provider text carrying a canary.
OUTCOME_REPLIES: dict[ScrobbleOutcome, Reply] = {
    ScrobbleOutcome.ACCEPTED: accepted(),
    ScrobbleOutcome.IGNORED: _ignored(),
    ScrobbleOutcome.SAFE_RETRY: json_reply({"error": 16, "message": "canary-provider"}),
    ScrobbleOutcome.AUTH_REQUIRED: json_reply(
        {"error": 9, "message": "canary-provider"}
    ),
    ScrobbleOutcome.PERMANENT_REJECT: json_reply(
        {"error": 6, "message": "canary-provider canary-title"}
    ),
    ScrobbleOutcome.UNCERTAIN: Reply(status=200, body=b"{}"),
}
MALFORMED_REPLIES = {
    "not_json": Reply(status=200, body=b"<<canary-provider canary-title"),
    "wrong_shape": json_reply({"scrobbles": "canary-artist"}),
    "error_shape": json_reply({"error": "canary-album"}),
    "server_error": Reply(status=500, body=b"canary-provider"),
}


def test_every_outcome_has_a_reply() -> None:
    assert set(OUTCOME_REPLIES) == set(ScrobbleOutcome)


async def _outputs(pilot: Pilot, caplog: pytest.LogCaptureFixture) -> dict[str, str]:
    registry = er.async_get(pilot.hass)
    outputs: dict[str, str] = {"logs": caplog.text}
    for suffix in ("status", "delivery"):
        entity_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{pilot.entry.entry_id}_{suffix}"
        )
        assert entity_id is not None, suffix
        state = pilot.hass.states.get(entity_id)
        assert state is not None
        outputs[f"{suffix}_sensor"] = state.state + json.dumps(
            dict(state.attributes), default=str
        )
    outputs["diagnostics"] = json.dumps(
        await async_get_config_entry_diagnostics(pilot.hass, pilot.entry)
    )
    outputs["service"] = repr(pilot.service) + repr(pilot.service.status)
    outputs["service_vars"] = repr(vars(pilot.service).get("_activity"))
    await pilot.pause()
    outputs["clear_queue"] = str(await pilot.option("clear_queue"))
    return outputs


def _assert_clean(outputs: dict[str, str]) -> None:
    for name, output in outputs.items():
        for canary in CANARIES:
            assert canary not in output, f"{canary} leaked through {name}"


async def _deliver_one(
    pilot: Pilot, scrobble: Reply, hist: Reply, *, posts: int = 1
) -> None:
    await pilot.linked()
    await pilot.accept_now()
    pilot.server.queue(RECENT, hist)
    pilot.server.queue(SCROBBLE, scrobble)
    await pilot.past_grace()
    await pilot.reach(lambda: pilot.posts() >= posts, tick=1)
    await pilot.quiet()


@pytest.mark.parametrize("outcome", list(ScrobbleOutcome))
async def test_canaries_absent_for_every_outcome(
    pilot: Pilot, caplog: pytest.LogCaptureFixture, outcome: ScrobbleOutcome
) -> None:
    caplog.set_level(logging.DEBUG)

    await _deliver_one(pilot, OUTCOME_REPLIES[outcome], history())

    assert pilot.posts() >= 1
    _assert_clean(await _outputs(pilot, caplog))


@pytest.mark.parametrize("name", list(MALFORMED_REPLIES))
async def test_canaries_absent_for_malformed_bodies(
    pilot: Pilot, caplog: pytest.LogCaptureFixture, name: str
) -> None:
    caplog.set_level(logging.DEBUG)

    await _deliver_one(pilot, MALFORMED_REPLIES[name], history())

    _assert_clean(await _outputs(pilot, caplog))


async def test_canaries_absent_for_a_malformed_history(
    pilot: Pilot, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    await _deliver_one(pilot, accepted(), MALFORMED_REPLIES["not_json"], posts=0)

    _assert_clean(await _outputs(pilot, caplog))


async def test_canaries_absent_for_an_outbox_error(
    pilot: Pilot,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caplog.set_level(logging.DEBUG)
    await pilot.linked()
    await pilot.accept_now()
    pilot.server.queue(RECENT, history())
    pilot.server.queue(SCROBBLE, accepted())
    with monkeypatch.context() as patch:
        fail_on(patch, "UPDATE plays SET state = 'in_flight'", message="canary-title")
        await pilot.past_grace()
        await pilot.reach(lambda: pilot.service.status.error_code is not None, tick=1)
        assert pilot.service.status.error_code == "storage_error"
        outputs = await _outputs(pilot, caplog)
    error = OutboxError("storage_error")

    outputs["error"] = repr(error) + str(error)
    _assert_clean(outputs)


def _files_holding(root: str, needle: bytes) -> list[str]:
    return sorted(
        found.name
        for found in Path(root).rglob("*")
        if found.is_file() and needle in found.read_bytes()
    )


async def test_queue_payload_only_in_private_storage(
    pilot: Pilot, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    await pilot.linked()
    await pilot.accept_now()

    assert b"canary-title" in pilot.path.read_bytes()
    assert pilot.path.stat().st_mode & 0o077 == 0
    assert "canary" not in json.dumps(dict(pilot.entry.data))
    assert _files_holding(pilot.hass.config.config_dir, b"canary-title") == [
        pilot.path.name
    ]
    _assert_clean(await _outputs(pilot, caplog))


def _texts() -> dict[str, str]:
    texts = {"README": (ROOT / "README.md").read_text()}
    for name in ("strings.json", "translations/en.json", "translations/nl.json"):
        texts[name] = (COMPONENT / name).read_text()
    return texts


def test_docs_never_claim_encryption() -> None:
    claim = re.compile(r"encrypt|versleutel|beveiligd", re.IGNORECASE)
    negated = re.compile(r"(not|niet|geen)\s+(encrypted|versleuteld)", re.IGNORECASE)
    for name, text in _texts().items():
        for line in text.splitlines():
            if claim.search(line):
                assert negated.search(line), f"{name} claims protection: {line}"


def test_no_text_claims_that_delivery_never_sends() -> None:
    for name in ("strings.json", "translations/en.json"):
        text = (COMPONENT / name).read_text().lower()
        assert "never sends" not in text, name
        assert "stays read-only" not in text, name
        assert "linking an account sends nothing" not in text, name
    dutch = (COMPONENT / "translations/nl.json").read_text().lower()
    assert "verstuurt nooit" not in dutch
    assert "blijft alleen-lezen" not in dutch


@pytest.mark.parametrize("name", ["translations/en.json", "translations/nl.json"])
def test_option_texts_state_the_limits(name: str) -> None:
    steps = json.loads((COMPONENT / name).read_text())["options"]["step"]
    errors = json.loads((COMPONENT / name).read_text())["options"]["abort"]
    menu = steps["init"]["description"].lower()
    clear = steps["clear_queue"]["description"].lower()
    disconnect = steps["disconnect"]["description"].lower()
    english = name.endswith("en.json")

    def has(text: str, english_word: str, dutch_word: str) -> bool:
        return (english_word if english else dutch_word) in text

    assert has(menu, "switches", "schakelaars")
    assert has(menu, "both start off", "beide uit")
    assert has(menu, "spotify", "spotify")
    assert has(menu, "duplicate", "dubbele")
    assert has(clear, "backup", "back-up")
    assert has(disconnect, "revoke", "ingetrokken")
    failure = errors["storage_error"].lower()
    assert has(failure, "queue", "wachtrij")
    assert has(failure, "not confirmed", "niet bevestigd")
    assert has(failure, "disconnect", "ontkoppeling")
    assert has(failure, "already applied", "al toegepast")
