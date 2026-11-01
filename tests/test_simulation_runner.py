"""Local simulation tooling stops its own processes and reports malformed output."""

import asyncio
import os
import signal
import sys

import pytest

from scripts import run_lastfm_simulation as runner


async def test_stop_escalates_only_the_process_group_it_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "STOP_TIMEOUT_SECONDS", 0.1)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import signal,time; signal.signal(signal.SIGINT,signal.SIG_IGN); "
        "print('ready',flush=True); time.sleep(60)",
        stdout=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdout is not None
    try:
        async with asyncio.timeout(3):
            assert await process.stdout.readline() == b"ready\n"
            await runner._async_stop(process)
        assert process.returncode == -signal.SIGKILL
    finally:
        if process.returncode is None:
            os.killpg(process.pid, signal.SIGKILL)
            await process.wait()


async def test_malformed_event_output_wakes_startup_and_reports_the_problem() -> None:
    stream = asyncio.StreamReader()
    stream.feed_data(b"not JSON\n")
    stream.feed_eof()
    ready = asyncio.Event()
    with pytest.raises(runner.SimulationError, match="event"):
        await runner._async_read_events(stream, [], ready)
    assert ready.is_set()
