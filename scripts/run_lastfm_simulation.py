"""Run the real Last.fm client against the local simulation and check every outcome.

This shows how the client behaves against a synthetic mock, never that the real API
agrees: see postman/simulations/lastfm-auth/lastfm-auth.context.yaml.
"""

import asyncio
import json
import os
import signal
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

import aiohttp

from custom_components.personal_sonos_scrobbler import lastfm
from custom_components.personal_sonos_scrobbler.lastfm import (
    LastFmClient,
    LastFmCredentials,
    LastFmError,
)

ROOT = Path(__file__).resolve().parents[1]
SIMULATION = "postman/simulations/lastfm-auth/lastfm-auth.sim.yaml"
SHARED_SECRET = "synthetic-simulation-secret"
ACCOUNT_NAME = "synthetic_listener"
MOCK_MEMBER = "lastfm"
FAULT_MEMBER = "lastfm-rate-limited"
START_TIMEOUT_SECONDS = 60.0
STOP_TIMEOUT_SECONDS = 15.0
EVENT_LINE_LIMIT = 4 * 1024 * 1024


class SimulationError(RuntimeError):
    """The simulation did not produce the expected evidence."""


@dataclass(frozen=True, slots=True)
class Case:
    """One client walk through a member, with the outcome of each step."""

    name: str
    member: str
    api_key: str
    expected: tuple[str, ...]
    shared_secret: str = SHARED_SECRET


CASES = (
    Case("success", MOCK_MEMBER, "1" * 32, ("token_created", ACCOUNT_NAME)),
    Case(
        "unapproved_then_approved",
        MOCK_MEMBER,
        "2" * 32,
        ("token_created", "token_not_authorized", ACCOUNT_NAME),
    ),
    Case("http_200_rate_limit", MOCK_MEMBER, "3" * 32, ("rate_limited",)),
    Case("malformed_body", MOCK_MEMBER, "4" * 32, ("unexpected_response",)),
    Case("oversized_body", MOCK_MEMBER, "5" * 32, ("unexpected_response",)),
    Case(
        "wrong_signature",
        MOCK_MEMBER,
        "1" * 32,
        ("invalid_credentials",),
        shared_secret="not-the-simulation-secret",
    ),
    Case("injected_http_429", FAULT_MEMBER, "1" * 32, ("rate_limited",)),
)


CASE_MEMBERS = frozenset(case.member for case in CASES)


async def _async_run_case(session: aiohttp.ClientSession, case: Case) -> list[str]:
    client = LastFmClient(session, LastFmCredentials(case.api_key, case.shared_secret))
    outcomes: list[str] = []
    token: str | None = None
    for position in range(len(case.expected)):
        try:
            if position == 0:
                token = await client.async_create_token()
                outcomes.append("token_created")
            else:
                assert token is not None
                created = await client.async_create_session(token)
                outcomes.append(created.account_name)
        except LastFmError as error:
            outcomes.append(error.code.value)
    return outcomes


async def _async_run_cases(addresses: dict[str, str]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    async with aiohttp.ClientSession() as session:
        for case in CASES:
            lastfm.API_ROOT = f"{addresses[case.member]}/"
            outcomes = await _async_run_case(session, case)
            results.append(
                {
                    "case": case.name,
                    "member": case.member,
                    "outcomes": outcomes,
                    "passed": outcomes == list(case.expected),
                }
            )
    return results


async def _async_read_events(
    stream: asyncio.StreamReader, events: list[dict[str, Any]], ready: asyncio.Event
) -> None:
    try:
        while line := await stream.readline():
            try:
                event = json.loads(line)
            except ValueError:
                raise SimulationError("invalid simulation event output") from None
            if not isinstance(event, dict):
                raise SimulationError("invalid simulation event shape")
            events.append(event)
            if event.get("event") == "listening":
                ready.set()
    finally:
        ready.set()


async def _async_listeners(process_group: int) -> list[str]:
    """Return the addresses this simulation really listens on, as lsof sees them."""
    lsof = await asyncio.create_subprocess_exec(
        "lsof",
        "-nP",
        "-a",
        "-g",
        str(process_group),
        "-iTCP",
        "-sTCP:LISTEN",
        "-Fn",
        stdout=asyncio.subprocess.PIPE,
    )
    output, _ = await lsof.communicate()
    return sorted(
        {line[1:] for line in output.decode().splitlines() if line.startswith("n")}
    )


async def _async_stop(process: asyncio.subprocess.Process) -> None:
    """Stop the process group this script created, and nothing else."""
    if process.returncode is not None:
        await process.wait()
        return
    for stop_signal, deadline in (
        (signal.SIGINT, STOP_TIMEOUT_SECONDS),
        (signal.SIGKILL, 5.0),
    ):
        try:
            os.killpg(process.pid, stop_signal)
        except ProcessLookupError:
            await process.wait()
            return
        try:
            async with asyncio.timeout(deadline):
                await process.wait()
            return
        except TimeoutError:
            if stop_signal is signal.SIGKILL:
                raise SimulationError("simulation did not exit after SIGKILL") from None


async def _async_port_is_closed(port: int) -> bool:
    try:
        _, writer = await asyncio.open_connection("127.0.0.1", port)
    except OSError:
        return True
    writer.close()
    await writer.wait_closed()
    return False


def _request_evidence(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the routing fields; raw events hold headers and bodies."""
    return [
        {key: event.get(key) for key in ("routeKey", "method", "path", "statusCode")}
        for event in events
        if event.get("event") == "request"
    ]


def _expected_mock_requests() -> int:
    return sum(len(case.expected) for case in CASES if case.member == MOCK_MEMBER)


def _check(
    results: list[dict[str, Any]],
    requests: list[dict[str, Any]],
    listeners: list[str],
    port_closed: bool,
) -> None:
    failed = [result["case"] for result in results if not result["passed"]]
    if failed:
        raise SimulationError(f"client outcomes differ: {failed}")
    if len(requests) != _expected_mock_requests():
        raise SimulationError(f"expected mock requests, got {len(requests)}")
    if {(item["routeKey"], item["method"], item["path"]) for item in requests} != {
        (MOCK_MEMBER, "POST", "/")
    }:
        raise SimulationError("a request took an unexpected route")
    if len(listeners) != len(CASE_MEMBERS) + 1 or not all(
        listener.startswith("127.0.0.1:") for listener in listeners
    ):
        raise SimulationError(f"expected loopback listeners only, got {listeners}")
    if not port_closed:
        raise SimulationError("the simulation port is still open after stopping")


async def _async_simulate(stderr: IO[bytes]) -> dict[str, Any]:
    process = await asyncio.create_subprocess_exec(
        "postman",
        "simulation",
        "run",
        SIMULATION,
        "--port",
        "auto",
        "--no-history",
        "--output",
        "ndjson",
        cwd=ROOT,
        stdout=asyncio.subprocess.PIPE,
        stderr=stderr,
        limit=EVENT_LINE_LIMIT,
        start_new_session=True,
    )
    assert process.stdout is not None
    events: list[dict[str, Any]] = []
    ready = asyncio.Event()
    reader = asyncio.create_task(_async_read_events(process.stdout, events, ready))
    try:
        try:
            async with asyncio.timeout(START_TIMEOUT_SECONDS):
                await ready.wait()
        except TimeoutError:
            raise SimulationError("simulation startup deadline exceeded") from None
        listening = next(
            (event for event in events if event.get("event") == "listening"), None
        )
        if listening is None:
            raise SimulationError("the simulation exited before listening")
        addresses = {
            member["routeKey"]: member["address"] for member in listening["mocks"]
        }
        listeners = await _async_listeners(process.pid)
        results = await _async_run_cases(addresses)
    finally:
        try:
            await _async_stop(process)
        finally:
            try:
                async with asyncio.timeout(5.0):
                    await reader
            except TimeoutError:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
                raise SimulationError("simulation event reader did not stop") from None

    port = listening["port"]
    requests = _request_evidence(events)
    _check(results, requests, listeners, await _async_port_is_closed(port))
    return {
        "simulation": listening["simulation"],
        "cases": results,
        "mock_requests": len(requests),
        "route_status_counts": _count_statuses(requests),
        "summary": next(
            (event for event in events if event.get("event") == "summary"), None
        ),
        "listeners": listeners,
        "port_closed_after_stop": True,
    }


def _count_statuses(requests: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in requests:
        key = f"{item['routeKey']} {item['method']} {item['path']} {item['statusCode']}"
        counts[key] = counts.get(key, 0) + 1
    return counts


def main() -> None:
    """Print the evidence, or fail with what is missing."""
    with tempfile.TemporaryFile() as stderr:
        try:
            evidence = asyncio.run(_async_simulate(stderr))
        except SimulationError:
            stderr.seek(0)
            sys.stderr.buffer.write(stderr.read())
            raise
    sys.stdout.write(f"{json.dumps(evidence, indent=2)}\n")


if __name__ == "__main__":
    main()
