"""Exercise real HA startup, Marketplace installs and HACS handover offline.

Run only through run.py: its Docker network is internal and /config is disposable.
No pytest plugin, production configuration, real Sonos or Last.fm is used.
"""

import asyncio
import hashlib
import json
import logging
import sys
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit

ROOT = Path("/workspace")
FIXTURES = Path("/fixtures")
CONFIG = Path("/config")
DOMAIN = "personal_sonos_scrobbler"
REPOSITORY = "yo-han/home-assistant-sonos-scrobbler"
REPOSITORY_ID = "1442063425"
PHASE = sys.argv[1]


def read(path):
    return json.loads(path.read_text())


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def require(condition, explanation):
    if not condition:
        raise AssertionError(explanation)


async def run():
    # Import HA before HTTP libraries: 2026.11 initializes their aliases itself.
    from aiohttp import ClientSession, web
    from homeassistant import bootstrap, loader
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.const import __version__
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers import entity_registry as er

    requests = []
    misses = []
    cache = read(FIXTURES / "responses.json")

    async def respond(request):
        target = request.query["url"]
        parts = urlsplit(target)
        key = parts.netloc + parts.path
        requests.append({"method": request.method, "url": target})
        if parts.netloc == "data-v2.hacs.xyz":
            section = parts.path.split("/")[1]
            return web.json_response([] if section in ("removed", "critical") else {})
        if key in cache:
            item = cache[key]
            return web.Response(
                body=(FIXTURES / item["file"]).read_bytes(),
                content_type=item["content_type"],
            )
        # Non-GitHub core startup requests are deliberately offline.
        if parts.netloc in (
            "api.github.com",
            "github.com",
            "raw.githubusercontent.com",
        ):
            misses.append(target)
        return web.Response(status=503, text="Offline test: URL not recorded")

    app = web.Application()
    app.router.add_route("*", "/", respond)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 18124)
    await site.start()
    original_request = ClientSession._request

    async def replay(session, method, url, **kwargs):
        target = str(url)
        if urlsplit(target).hostname not in ("localhost", "127.0.0.1"):
            from yarl import URL

            target = str(URL("http://127.0.0.1:18124/").with_query(url=target))
            kwargs.pop("params", None)
            kwargs.pop("auth", None)
            kwargs.pop("ssl", None)
        return await original_request(session, method, target, **kwargs)

    ClientSession._request = replay
    hass = HomeAssistant(str(CONFIG))
    hass.config.skip_pip = True
    loader.async_setup(hass)
    try:
        result = await bootstrap.async_from_config_dict(
            {
                "homeassistant": {
                    "name": "Marketplace isolated test",
                    "latitude": 0,
                    "longitude": 0,
                    "elevation": 0,
                    "unit_system": "metric",
                    "time_zone": "UTC",
                    "country": "NL",
                },
                "http": {"server_host": "127.0.0.1", "server_port": 18123},
                "frontend": {},
            },
            hass,
        )
        require(result is hass, "HA bootstrap failed")
        await hass.async_start()
        await hass.async_block_till_done()
        report = {"phase": PHASE, "homeassistant": __version__, "checks": []}
        registry = er.async_get(hass)

        if PHASE in ("clean", "migrated"):
            from homeassistant.components.marketplace.enums import RepositoryCategory

            entries = hass.config_entries.async_entries("marketplace")
            require(len(entries) == 1, "Expected exactly one Marketplace entry")
            marketplace_entry = entries[0]
            require(
                marketplace_entry.state.value == "loaded", "Marketplace did not load"
            )
            manager = marketplace_entry.runtime_data
            require(manager.system.disabled_reason is None, "Marketplace is disabled")
            report["checks"].append("Marketplace loaded through real bootstrap")
            if PHASE == "clean":
                require(
                    not (CONFIG / "custom_components" / DOMAIN).exists(),
                    "Clean install already has the component",
                )
                # Model an already connected test account; device OAuth is not tested.
                manager.configuration.token = "synthetic-github-connection"
                errors = await manager.async_register_repository(
                    REPOSITORY, RepositoryCategory.INTEGRATION
                )
                require(not errors, f"Repository registration failed: {errors}")
                repository = manager.repositories.get_by_full_name(REPOSITORY)
                require(repository is not None, "Custom repository missing")
                manager.common.custom_repositories.add(repository.data.id)
                await repository.async_install_repository(ref="v0.8.1")
                require(
                    repository.data.installed,
                    "Marketplace did not mark install complete",
                )
                require(
                    not repository.pending_restart,
                    "Clean first install requested restart",
                )
                installed = CONFIG / "custom_components" / DOMAIN
                require(
                    (installed / "manifest.json").is_file(),
                    "Installer wrote no manifest",
                )
                expected = read(FIXTURES / "component-hashes.json")
                actual = {
                    str(path.relative_to(installed)): hashlib.sha256(
                        path.read_bytes()
                    ).hexdigest()
                    for path in installed.rglob("*")
                    if path.is_file() and "__pycache__" not in path.parts
                }
                require(
                    actual == expected,
                    "Installed component differs from candidate archive",
                )
                report["checks"].extend(
                    [
                        "Real custom-repository registration and installer",
                        "Installed files match candidate hashes",
                        "First install needs no restart",
                    ]
                )
            else:
                before = read(CONFIG / "before.json")
                require(
                    marketplace_entry.entry_id == before["hacs_entry_id"],
                    "HACS entry identity changed",
                )
                require(
                    not hass.config_entries.async_entries("hacs"),
                    "Legacy HACS entry remains",
                )
                repository = manager.repositories.get_by_full_name(REPOSITORY)
                require(
                    repository is not None and repository.data.installed,
                    "Installed repository lost",
                )
                require(
                    repository.data.installed_version == "v0.8.1",
                    "Installed version lost",
                )
                require(
                    REPOSITORY_ID in manager.common.custom_repositories,
                    "Custom repository lost",
                )
                update = registry.async_get(before["update_entity_id"])
                require(
                    update is not None and update.platform == "marketplace",
                    "Update entity not adopted",
                )
                require(update.unique_id == REPOSITORY_ID, "Update identity changed")
                entry = hass.config_entries.async_entries(DOMAIN)[0]
                require(
                    entry.entry_id == before["entry_id"],
                    "Integration entry identity changed",
                )
                require(
                    dict(entry.data) == before["data"],
                    "Account, flags or speaker choices changed",
                )
                require(
                    entry.title == before["title"], "Custom integration title changed"
                )
                require(
                    entry.state.value == "loaded", "Migrated integration failed to load"
                )
                for entity_id, expected in before["entities"].items():
                    entity = registry.async_get(entity_id)
                    require(
                        entity is not None
                        and entity.unique_id == expected["unique_id"],
                        "Entity identity lost",
                    )
                    require(entity.name == expected["name"], "User entity name lost")
                    require(
                        entity.device_id == expected["device_id"],
                        "Device identity changed",
                    )
                report["checks"].extend(
                    [
                        "HACS config entry handed over by core loader",
                        "Custom repository and installed version retained",
                        "Update entity adopted without ID change",
                        "Synthetic credentials, selections and both flags unchanged",
                        "Entry, custom name, entity and device IDs retained",
                    ]
                )
                await hass.async_add_executor_job(verify_delivery_storage, hass, entry)
                report["checks"].append(
                    "SQLite receipt and sent total survive the upgrade"
                )
                await verify_local_display(hass, entry, registry)
                from custom_components.personal_sonos_scrobbler.delivery import (
                    delivery_service,
                )

                service = delivery_service(hass, entry.entry_id)
                await service.async_settle()
                require(
                    service.status.status.value == "ready",
                    "Delivery did not resume healthy",
                )
                report["checks"].append(
                    "Local track and speaker sensors follow included room only"
                )

        if PHASE in ("clean", "seed"):
            # An ignored synthetic entry never connects to hardware. Its
            # enabled registry entities publish fixture states below.
            sonos = ConfigEntry(
                domain="sonos",
                title="Synthetic speakers",
                data={},
                options={},
                version=1,
                minor_version=1,
                source="ignore",
                unique_id="synthetic",
                discovery_keys=MappingProxyType({}),
                subentries_data=[],
            )
            await hass.config_entries.async_add(sonos)
            speakers = [
                registry.async_get_or_create(
                    "media_player",
                    "sonos",
                    f"RINCON_SYNTHETIC_{name.upper()}",
                    config_entry=sonos,
                    suggested_object_id=name,
                    original_name=name.title(),
                )
                for name in ("kitchen", "kids")
            ]
            for speaker in speakers:
                hass.states.async_set(speaker.entity_id, "idle")
            flow = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": "user"}
            )
            require(flow["type"] == "form", f"Setup flow unavailable: {flow}")
            created = await hass.config_entries.flow.async_configure(
                flow["flow_id"], {"entity_id": [speakers[0].entity_id]}
            )
            require(created["type"] == "create_entry", f"Setup failed: {created}")
            entry = created["result"]
            await hass.async_block_till_done()
            require(
                entry.state.value == "loaded", "Configured integration did not load"
            )
            require(
                entry.data["now_playing_enabled"] is False,
                "New install enabled Now Playing",
            )
            require(
                entry.data["scrobbling_enabled"] is False,
                "New install enabled scrobbling",
            )
            await verify_local_display(hass, entry, registry)
            report["checks"].extend(
                [
                    "Real config flow and entity platforms load",
                    "New install leaves both delivery flags off",
                    "Local track and speaker sensors follow included room only",
                ]
            )
            if PHASE == "seed":
                # Synthetic only, chosen to match the owner's policy, not their data.
                hass.config_entries.async_update_entry(
                    entry,
                    title="My music",
                    data={
                        **entry.data,
                        "lastfm": {
                            "api_key": "a" * 32,
                            "shared_secret": "b" * 32,
                            "session_key": "c" * 32,
                            "account_name": "synthetic-test-user",
                        },
                    },
                )
                now_playing = registry.async_get_entity_id(
                    "switch", DOMAIN, f"{entry.entry_id}_now_playing"
                )
                await hass.services.async_call(
                    "switch", "turn_on", {"entity_id": now_playing}, blocking=True
                )
                await hass.async_block_till_done()
                require(entry.data["outbox_created"], "Consent did not create ledger")
                require(entry.data["now_playing_enabled"], "Now Playing consent lost")
                await hass.async_add_executor_job(seed_receipt, hass, entry)
                entities = er.async_entries_for_config_entry(registry, entry.entry_id)
                registry.async_update_entity(
                    entities[0].entity_id, name="My custom entity name"
                )
                entities = er.async_entries_for_config_entry(registry, entry.entry_id)
                report["before"] = {
                    "entry_id": entry.entry_id,
                    "title": entry.title,
                    "data": dict(entry.data),
                    "entities": {
                        entity.entity_id: {
                            "unique_id": entity.unique_id,
                            "name": entity.name,
                            "device_id": entity.device_id,
                        }
                        for entity in entities
                    },
                }
        require(
            not any(
                "audioscrobbler" in item["url"] or "last.fm" in item["url"]
                for item in requests
            ),
            "Unexpected Last.fm request",
        )
        report["checks"].append("No Last.fm HTTP requests")
        require(not misses, f"Unrecorded GitHub request(s): {misses}")
        write(CONFIG / f"{PHASE}-report.json", report)
    finally:
        await hass.async_block_till_done()
        await hass.async_stop()
        await runner.cleanup()
        ClientSession._request = original_request
        write(CONFIG / f"{PHASE}-requests.json", requests)


async def verify_local_display(hass, entry, registry):
    """Observe synthetic playback without enabling either external sender."""
    from homeassistant.util import dt as dt_util

    included = registry.async_get(entry.data["entity_registry_ids"][0]).entity_id
    kids = registry.async_get_entity_id(
        "media_player", "sonos", "RINCON_SYNTHETIC_KIDS"
    )
    # Never publish playing state while retained Now Playing is on.
    restore_notice = entry.data["now_playing_enabled"]
    notice = registry.async_get_entity_id(
        "switch", DOMAIN, f"{entry.entry_id}_now_playing"
    )
    if restore_notice:
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": notice}, blocking=True
        )
    try:
        for entity_id, title in ((included, "Included song"), (kids, "Excluded song")):
            hass.states.async_set(
                entity_id,
                "playing",
                {
                    "source": "Spotify Connect",
                    "media_artist": "Synthetic Artist",
                    "media_title": title,
                    "media_duration": 200,
                    "media_position": 0,
                    "media_position_updated_at": dt_util.utcnow(),
                    "group_members": [entity_id],
                },
            )
        # Wait for the real topology-settle timer, not a patched outcome.
        async with asyncio.timeout(15):
            while True:
                await hass.async_block_till_done()
                states = [
                    hass.states.get(entity.entity_id)
                    for entity in er_entries(registry, entry.entry_id)
                ]
                if any(
                    state and state.attributes.get("title") == "Included song"
                    for state in states
                ):
                    break
                await asyncio.sleep(0.1)
        current_speaker = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_current_speaker"
        )
        speaker_state = hass.states.get(current_speaker)
        require(
            speaker_state is not None
            and speaker_state.attributes.get("entity_id") == included,
            "Current speaker does not match the included room",
        )
        require(
            not any(
                state and "Excluded song" in str(state.as_dict()) for state in states
            ),
            "Excluded room reached an integration sensor",
        )
        hass.states.async_set(included, "paused")
        hass.states.async_set(kids, "paused")
        await hass.async_block_till_done()
    finally:
        if restore_notice:
            await hass.services.async_call(
                "switch", "turn_on", {"entity_id": notice}, blocking=True
            )


def seed_receipt(hass, entry):
    """Create one synthetic tombstone through the real ledger API, no HTTP."""
    from datetime import timedelta

    from homeassistant.util import dt as dt_util

    from custom_components.personal_sonos_scrobbler.delivery import ledger_path
    from custom_components.personal_sonos_scrobbler.outbox import (
        DeliveryState,
        EnqueueResult,
        OutboxStore,
    )
    from custom_components.personal_sonos_scrobbler.playback import (
        QualifiedPlay,
        TrackMetadata,
    )

    now = dt_util.utcnow()
    store = OutboxStore.open(ledger_path(hass, entry.entry_id))
    result = store.enqueue(
        QualifiedPlay(
            occurrence_id="migration-test-receipt",
            track_key=b"x" * 16,
            metadata=TrackMetadata(
                artist="Synthetic artist", title="Synthetic listen", album=""
            ),
            started_at=now - timedelta(seconds=200),
            qualified_at=now,
            duration_seconds=200,
        ),
        b"y" * 16,
        entry.data["delivery_epoch"],
        now,
        now,
    )
    require(result == EnqueueResult.ENQUEUED, "Could not seed receipt")
    store.finalize("migration-test-receipt", DeliveryState.SENT, "accepted")


def verify_delivery_storage(hass, entry):
    import sqlite3

    from custom_components.personal_sonos_scrobbler.delivery import ledger_path

    path = ledger_path(hass, entry.entry_id)
    require(path.is_file(), "Upgrade lost the ledger")
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        require(
            connection.execute("PRAGMA integrity_check").fetchone() == ("ok",),
            "Ledger integrity failed",
        )
        require(
            connection.execute(
                "SELECT state FROM plays WHERE id = ?", ("migration-test-receipt",)
            ).fetchone()
            == ("sent",),
            "Receipt lost",
        )
        require(
            connection.execute(
                "SELECT value FROM meta WHERE key = 'sent_total'"
            ).fetchone()
            == ("1",),
            "Sent total changed",
        )


def er_entries(registry, entry_id):
    from homeassistant.helpers.entity_registry import async_entries_for_config_entry

    return async_entries_for_config_entry(registry, entry_id)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(run())
