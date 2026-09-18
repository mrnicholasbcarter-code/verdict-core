"""BOD-117: post-execution outcome receipts are the only producer of measured spend."""

from __future__ import annotations

import json
from pathlib import Path

from verdict.outcome_log import (
    OUTCOME_RECORD_KIND,
    build_outcome_record,
    load_outcomes,
    log_outcome,
    measured_spend_for,
    observe_execution,
    outcome_log_path,
)


def test_outcome_log_path_is_a_sibling_of_the_decision_log(tmp_path: Path) -> None:
    assert outcome_log_path(tmp_path / "verdict-decisions.jsonl") == (
        tmp_path / "verdict-outcomes.jsonl"
    )
    assert outcome_log_path(tmp_path / "custom.jsonl") == tmp_path / "custom-outcomes.jsonl"


def test_observe_execution_reads_cost_and_tokens_only_from_gateway_headers() -> None:
    observed = observe_execution(
        headers=[
            ("Content-Type", "application/json"),
            ("X-OmniRoute-Request-Id", "req-9"),
            ("X-OmniRoute-Response-Cost", "0.0042"),
            ("X-OmniRoute-Tokens-In", "800"),
            ("X-OmniRoute-Tokens-Out", "120"),
            ("X-OmniRoute-Cache-Hit", "false"),
        ],
        body=None,
    )
    assert observed["observed_cost_usd"] == 0.0042
    assert observed["cost_source"] == "x-omniroute-response-cost"
    assert observed["observed_tokens_in"] == 800
    assert observed["observed_tokens_out"] == 120
    assert observed["observed_tokens_total"] == 920
    assert observed["tokens_source"] == "x-omniroute-tokens-in/out"
    assert observed["cache_hit"] is False
    assert observed["execution_id"] == "req-9"


def test_observe_execution_without_cost_header_is_unmeasured_not_zero() -> None:
    observed = observe_execution(headers=[("content-type", "application/json")], body=None)
    assert observed["observed_cost_usd"] is None
    assert observed["cost_source"] is None
    assert observed["observed_tokens_total"] is None


def test_observe_execution_takes_tokens_from_body_usage_when_headers_are_absent() -> None:
    body = json.dumps({"usage": {"prompt_tokens": 10, "completion_tokens": 5}}).encode()
    observed = observe_execution(headers=[], body=body)
    assert observed["observed_tokens_in"] == 10
    assert observed["observed_tokens_out"] == 5
    assert observed["observed_tokens_total"] == 15
    assert observed["tokens_source"] == "body.usage"
    assert observed["observed_cost_usd"] is None, "token counts never become a price"


def test_observe_execution_rejects_malformed_header_values() -> None:
    observed = observe_execution(
        headers=[("x-omniroute-response-cost", "free"), ("x-omniroute-tokens-in", "-3")], body=None
    )
    assert observed["observed_cost_usd"] is None
    assert observed["observed_tokens_in"] is None


def test_log_outcome_round_trips_and_last_attempt_wins(tmp_path: Path) -> None:
    decisions = tmp_path / "verdict-decisions.jsonl"
    first = build_outcome_record(
        request_id="req-1",
        model="openrouter/a:free",
        status_code=503,
        attempt=0,
        surface="chat",
        headers=[],
        body=None,
    )
    second = build_outcome_record(
        request_id="req-1",
        model="openrouter/b:free",
        status_code=200,
        attempt=1,
        surface="chat",
        headers=[("x-omniroute-response-cost", "0.001"), ("x-omniroute-request-id", "exec-2")],
        body=None,
    )
    log_outcome(decisions, first)
    log_outcome(decisions, second)

    lines = outcome_log_path(decisions).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert all(json.loads(line)["record"] == OUTCOME_RECORD_KIND for line in lines)

    outcomes = load_outcomes(decisions)
    assert set(outcomes) == {"req-1"}
    assert outcomes["req-1"]["completed_with"] == "openrouter/b:free"
    assert outcomes["req-1"]["observed_cost_usd"] == 0.001
    assert outcomes["req-1"]["execution_id"] == "exec-2"
    assert outcomes["req-1"]["attempt"] == 1


def test_log_outcome_is_a_no_op_without_a_log_path(tmp_path: Path) -> None:
    log_outcome(
        "",
        build_outcome_record(
            request_id="r",
            model="m",
            status_code=200,
            attempt=0,
            surface="chat",
            headers=[],
            body=None,
        ),
    )
    assert not list(tmp_path.iterdir())


def test_measured_spend_joins_decisions_to_outcomes_by_request_id(tmp_path: Path) -> None:
    decisions = tmp_path / "verdict-decisions.jsonl"
    log_outcome(
        decisions,
        build_outcome_record(
            request_id="req-1",
            model="m",
            status_code=200,
            attempt=0,
            surface="chat",
            headers=[("x-omniroute-response-cost", "0.25"), ("x-omniroute-tokens-in", "1")],
            body=None,
        ),
    )
    outcomes = load_outcomes(decisions)
    measured = measured_spend_for({"request_id": "req-1"}, outcomes)
    assert measured == {
        "observed_cost_usd": 0.25,
        "observed_tokens_total": 1,
        "cost_source": "outcome receipt (x-omniroute-response-cost)",
    }
    assert measured_spend_for({"request_id": "req-unknown"}, outcomes) is None
    assert measured_spend_for({}, outcomes) is None
