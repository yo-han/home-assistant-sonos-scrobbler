"""The delivery simulation fails on the evidence, not on a flag a scenario set."""

from typing import Any

import pytest

from scripts import run_delivery_simulation as runner

REQUESTS = [{"routeKey": "lastfm", "method": "POST", "path": "/", "statusCode": 200}]
LISTENERS = ["127.0.0.1:50001", "127.0.0.1:50002"]


def passing_results() -> list[dict[str, Any]]:
    return [
        {"scenario": scenario.name, **scenario.expected()}
        for scenario in runner.SCENARIOS
    ]


def test_check_fails_on_second_post_in_ambiguity() -> None:
    results = passing_results()
    runner._check(results, REQUESTS, LISTENERS, True)
    ambiguous = next(
        result for result in results if result["scenario"] == "timeout_after_write"
    )
    ambiguous["posts"] = 2

    with pytest.raises(runner.SimulationError, match="second POST"):
        runner._check(results, REQUESTS, LISTENERS, True)


def test_check_fails_when_started_mock_saw_no_requests() -> None:
    results = passing_results()
    runner._check(results, REQUESTS, LISTENERS, True)

    with pytest.raises(runner.SimulationError, match="no requests"):
        runner._check(results, [], LISTENERS, True)


def test_check_fails_on_open_port_after_stop() -> None:
    results = passing_results()
    runner._check(results, REQUESTS, LISTENERS, True)

    with pytest.raises(runner.SimulationError, match="still open"):
        runner._check(results, REQUESTS, LISTENERS, False)


def passing_notices() -> list[dict[str, Any]]:
    return [
        {"scenario": scenario.name, **scenario.expected()}
        for scenario in runner.NOTICE_SCENARIOS
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [("notices", 2), ("stored", 1), ("posts", 1), ("ledger_rows", 1), ("gate", "x")],
)
def test_notice_check_fails_on_a_retry_or_a_history_effect(
    field: str, value: object
) -> None:
    results = passing_notices()
    runner._check_notices(results)
    failed = next(
        result for result in results if result["scenario"] == "notice_timeout"
    )
    failed[field] = value

    with pytest.raises(runner.SimulationError, match="notice outcomes differ"):
        runner._check_notices(results)
