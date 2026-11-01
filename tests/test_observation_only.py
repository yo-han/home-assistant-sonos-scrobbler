"""Structural proof that the component cannot send or control anything."""

import ast
from pathlib import Path

import pytest

COMPONENT = Path(__file__).parents[1] / "custom_components/personal_sonos_scrobbler"
SOURCES = sorted(COMPONENT.glob("**/*.py"))
TEXT_FILES = sorted(
    path
    for path in COMPONENT.glob("**/*")
    if path.suffix in {".py", ".json"} and "__pycache__" not in path.parts
)
PURE_MODULES = (
    "normalize.py",
    "now_playing.py",
    "observation.py",
    "outbox.py",
    "playback.py",
    "streams.py",
    "topology.py",
    "const.py",
)

FORBIDDEN_ATTRIBUTES = (
    "services",
    "async_call",
    "async_call_from_config",
    "async_remove",
    "async_set",
    "async_set_internal",
    "async_update_entity",
    "open_connection",
)
FORBIDDEN_NAMES = ("__import__", "eval", "exec", "getattr")
ALLOWED_MODULES = {
    "collections.abc",
    "dataclasses",
    "datetime",
    "enum",
    "hashlib",
    "homeassistant.components.sensor",
    "homeassistant.components.switch",
    "homeassistant.config_entries",
    "homeassistant.const",
    "homeassistant.core",
    "homeassistant.helpers.device_registry",
    "homeassistant.helpers.entity_platform",
    "homeassistant.helpers.entity_registry",
    "homeassistant.helpers.event",
    "homeassistant.helpers.selector",
    "homeassistant.loader",
    "homeassistant.util.dt",
    "logging",
    "probatio",
    "re",
    "time",
    "typing",
    "unicodedata",
    "urllib.parse",
    "uuid",
}
EXTRA_ALLOWED_MODULES = {
    "config_flow.py": {"homeassistant.exceptions"},
    "controls.py": {
        "asyncio",
        "homeassistant.exceptions",
        "homeassistant.helpers.dispatcher",
    },
    "delivery.py": {
        "asyncio",
        "collections",
        "homeassistant.helpers.aiohttp_client",
        "homeassistant.helpers.dispatcher",
        "pathlib",
        "random",
    },
    "lastfm.py": {"aiohttp", "asyncio", "json"},
    "lastfm_delivery.py": {"aiohttp", "asyncio", "json"},
    "lastfm_flow.py": {
        "homeassistant.helpers.aiohttp_client",
        "homeassistant.helpers.dispatcher",
    },
    "outbox.py": {
        "asyncio",
        "contextlib",
        "functools",
        "os",
        "pathlib",
        "sqlite3",
        "threading",
    },
    "sensor.py": {"homeassistant.helpers.dispatcher"},
    "speaker_controls.py": {"homeassistant.exceptions"},
    "switch.py": {"contextlib", "homeassistant.helpers.dispatcher"},
}
# The registry calls that keep the integration's own speaker switches current.
OWN_ENTITY_ATTRIBUTES = {"switch.py": {"async_remove", "async_update_entity"}}
HOME_ASSISTANT_PACKAGES = ("homeassistant.helpers", "homeassistant.util")
LASTFM_TERMS = ("last.fm", "lastfm", "audioscrobbler", "api_key", "api_secret")
LASTFM_AWARE_FILES = {
    "config_flow.py",
    "const.py",
    "controls.py",
    "delivery.py",
    "diagnostics.py",
    "en.json",
    "lastfm.py",
    "lastfm_delivery.py",
    "lastfm_flow.py",
    "nl.json",
    "sensor.py",
    "strings.json",
    "switch.py",
}
SENDING_TERMS = (
    "getmobilesession",
    "password=",
)


def relative_imports(tree: ast.AST) -> set[str]:
    """Return the sibling modules imported with a relative import."""
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level:
            if node.module:
                modules.add(node.module.split(".")[0])
            else:
                modules.update(alias.name for alias in node.names)
    return modules


def import_graph() -> dict[str, set[str]]:
    return {
        path.stem: relative_imports(ast.parse(path.read_text())) for path in SOURCES
    }


def reachable_from(graph: dict[str, set[str]], start: str) -> set[str]:
    seen: set[str] = set()
    pending = [start]
    while pending:
        module = pending.pop()
        if module not in seen:
            seen.add(module)
            pending.extend(graph.get(module, set()))
    return seen


def imported_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            if node.module in HOME_ASSISTANT_PACKAGES or node.module == "urllib":
                modules.update(f"{node.module}.{alias.name}" for alias in node.names)
            else:
                modules.add(node.module)
    return modules


def find_violations(source: str, filename: str) -> list[str]:
    tree = ast.parse(source, filename)
    allowed = ALLOWED_MODULES | EXTRA_ALLOWED_MODULES.get(filename, set())
    violations = [
        f"{filename}: imports {module}"
        for module in sorted(imported_modules(tree))
        if module not in allowed
    ]
    violations += [
        f"{filename}:{node.lineno}: uses .{node.attr}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr in FORBIDDEN_ATTRIBUTES
        and node.attr not in OWN_ENTITY_ATTRIBUTES.get(filename, set())
    ]
    violations += [
        f"{filename}:{node.lineno}: calls {node.id}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES
    ]
    return violations


def test_scan_covers_every_component_module() -> None:
    assert len(SOURCES) == 20


def test_the_registry_calls_of_the_switches_reach_only_their_own_entities() -> None:
    source = (COMPONENT / "switch.py").read_text()

    assert source.count(".async_remove(") == 1
    assert "registry.async_remove(entry.entity_id)" in source
    assert "entry in er.async_entries_for_config_entry(" in source
    assert source.count(".async_update_entity(") == 1
    assert "self.entity_id, original_name=" in source


@pytest.mark.parametrize("path", SOURCES, ids=lambda path: path.name)
def test_component_has_no_forbidden_constructs(path: Path) -> None:
    assert find_violations(path.read_text(), path.name) == []


@pytest.mark.parametrize(
    "snippet",
    [
        "import aiohttp",
        "import http.client",
        "import urllib.request",
        "from urllib.request import urlopen",
        "from urllib import request",
        "from homeassistant.helpers.aiohttp_client import async_get_clientsession",
        "from homeassistant.helpers import storage",
        "from homeassistant.components.sonos import media",
        'hass.services.async_call("x", "y")',
        'hass.states.async_set("a.b", "c")',
        'hass.states.async_remove("a.b")',
        'hass.states.async_set_internal("a.b", "c", {}, False)',
        'registry.async_update_entity("a.b", name="c")',
        "import subprocess",
        "import smtplib",
        "import soco",
        "import importlib",
        'asyncio.open_connection("a", 1)',
        '__import__("aiohttp")',
        'getattr(hass, "serv" + "ices")',
    ],
)
def test_guard_flags_forbidden_snippets(snippet: str) -> None:
    assert find_violations(snippet, "snippet.py") != []


def test_urllib_parse_is_allowed() -> None:
    assert find_violations("from urllib.parse import unquote", "snippet.py") == []


def test_extra_modules_stay_with_their_file() -> None:
    assert find_violations("import aiohttp", "lastfm.py") == []
    assert find_violations("import aiohttp", "runtime.py") != []


def test_sqlite_stays_with_the_outbox() -> None:
    assert find_violations("import sqlite3", "outbox.py") == []
    assert find_violations("import sqlite3", "runtime.py") != []
    assert find_violations("import sqlite3", "delivery.py") != []
    assert [
        path.name
        for path in SOURCES
        if "sqlite3" in imported_modules(ast.parse(path.read_text()))
    ] == ["outbox.py"]


def test_outbox_reaches_no_sibling_beyond_constants_and_playback() -> None:
    assert import_graph()["outbox"] == {"const", "playback"}


def test_lastfm_references_stay_in_the_allowlisted_files() -> None:
    assert TEXT_FILES
    for path in TEXT_FILES:
        if path.name in LASTFM_AWARE_FILES:
            continue
        text = path.read_text().casefold()
        for term in LASTFM_TERMS:
            assert term not in text, f"{term} in {path.name}"


def test_no_sending_or_password_path_ships() -> None:
    assert TEXT_FILES
    for path in TEXT_FILES:
        text = path.read_text().casefold()
        for term in SENDING_TERMS:
            assert term not in text, f"{term} in {path.name}"


@pytest.mark.parametrize("method", ["track.scrobble", "track.updatenowplaying"])
def test_write_methods_live_in_the_delivery_client_only(method: str) -> None:
    assert TEXT_FILES
    holders = [
        path.name for path in TEXT_FILES if method in path.read_text().casefold()
    ]

    assert holders == ["lastfm_delivery.py"]


def test_observation_path_cannot_reach_the_lastfm_client() -> None:
    graph = import_graph()

    assert "lastfm" in graph
    for start in (
        "runtime",
        "normalize",
        "observation",
        "topology",
        "streams",
        "playback",
        "outbox",
    ):
        assert "lastfm" not in reachable_from(graph, start), start
        assert "lastfm_delivery" not in reachable_from(graph, start), start


def test_only_the_flow_imports_the_client() -> None:
    importers = {
        module for module, imports in import_graph().items() if "lastfm" in imports
    }

    assert importers == {"lastfm_flow", "lastfm_delivery"}


def test_only_delivery_imports_the_delivery_client() -> None:
    importers = {
        module
        for module, imports in import_graph().items()
        if "lastfm_delivery" in imports
    }

    assert importers == {"delivery"}


def test_only_the_config_flow_imports_the_link_flow() -> None:
    importers = {
        module for module, imports in import_graph().items() if "lastfm_flow" in imports
    }

    assert importers == {"config_flow"}


def test_link_commit_cannot_yield_after_its_fence() -> None:
    tree = ast.parse((COMPONENT / "lastfm_flow.py").read_text())
    commits = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name == "_async_commit"
    ]

    assert len(commits) == 1
    assert isinstance(commits[0], ast.FunctionDef)
    assert not [node for node in ast.walk(commits[0]) if isinstance(node, ast.Await)]


def test_clients_expose_exactly_five_api_methods_and_two_urls() -> None:
    strings = {
        node.value
        for name in ("lastfm.py", "lastfm_delivery.py")
        for node in ast.walk(ast.parse((COMPONENT / name).read_text()))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }

    assert {
        text for text in strings if text.startswith(("auth.", "track.", "user."))
    } == {
        "auth.getToken",
        "auth.getSession",
        "track.scrobble",
        "track.updateNowPlaying",
        "user.getRecentTracks",
    }
    assert {text for text in strings if text.startswith(("http://", "https://"))} == {
        "https://ws.audioscrobbler.com/2.0/",
        "https://www.last.fm/api/auth/",
    }


@pytest.mark.parametrize("name", ["lastfm.py", "lastfm_delivery.py"])
def test_each_client_uses_one_post_and_no_other_session_call(name: str) -> None:
    tree = ast.parse((COMPONENT / name).read_text())
    session_calls = [
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "_session"
    ]

    assert session_calls == ["post"]


def test_no_services_yaml() -> None:
    assert not list(COMPONENT.glob("**/services.y*ml"))
    assert not list(COMPONENT.glob("**/icons.json"))


@pytest.mark.parametrize("name", PURE_MODULES)
def test_pure_modules_do_not_import_homeassistant(name: str) -> None:
    modules = imported_modules(ast.parse((COMPONENT / name).read_text()))

    assert not [module for module in modules if module.startswith("homeassistant")]


def test_the_delivery_step_reaches_no_homeassistant_name() -> None:
    tree = ast.parse((COMPONENT / "delivery.py").read_text())
    from_homeassistant = {
        alias.asname or alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        and (node.module or "").startswith("homeassistant")
        for alias in node.names
    }
    service_only = {"DeliveryService", "delivery_service", "ledger_path"}
    used = {
        name.id
        for node in tree.body
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name not in service_only
        and not node.name.endswith("_ledger")
        for name in ast.walk(node)
        if isinstance(name, ast.Name)
    }

    assert "ConfigEntry" in from_homeassistant
    assert not used & from_homeassistant
    core = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "DeliveryCore"
    )
    assert "DeliveryService" not in {
        name.id for name in ast.walk(core) if isinstance(name, ast.Name)
    }
