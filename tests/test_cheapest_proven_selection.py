"""D6: selection, not just receipt ranks, prefers the cheapest qualified tier."""

from tests.test_identify_rank import NOW, _conn, _ladder_row, _record, _score, _snapshot
from verdict.orchestration.contracts import CapacityClass, TaskRequirements
from verdict.orchestration.eligibility import EligibilityLadder
from verdict.orchestration.health_cache import HealthCache, ProbeResult
from verdict.subagent_selection import HealthResult


def test_unrated_screened_free_selected_before_rated_subscription(tmp_path):
    cache = HealthCache(tmp_path / "health.json")
    cache.record(
        "free/unrated",
        ProbeResult(
            category="ok", chat_ok=True, tool_ok=True, probe_class="agentic", agentic_ok=True
        ),
        NOW,
    )
    cache.save()
    ladder = EligibilityLadder(
        [
            _ladder_row("free/unrated", pricing={"input": 0}),
            _ladder_row("cc/rated", pricing={"input": 1}),
        ],
        [_conn("free", auth="apikey", free=True), _conn("cc", auth="oauth")],
        lambda _: HealthResult(True, "ok"),
        tmp_path / "state.json",
        health_cache=cache,
        metadata=_snapshot(_record("acme/rated", "cc/rated", aa_coding=_score(99))),
    )
    selected, _ = ladder.select(TaskRequirements(), now=NOW)
    assert selected is not None and selected.route_id == "free/unrated"
    assert selected.rank_components["evidence_level"] == "probation"


def test_subscription_selected_when_free_has_no_tool_screen(tmp_path):
    ladder = EligibilityLadder(
        [
            _ladder_row("free/unrated", pricing={"input": 0}),
            _ladder_row("cc/rated", pricing={"input": 1}),
        ],
        [_conn("free", auth="apikey", free=True), _conn("cc", auth="oauth")],
        lambda _: HealthResult(True, "ok"),
        tmp_path / "state.json",
        health_cache=HealthCache(tmp_path / "health.json"),
        metadata=None,
    )
    selected, verdicts = ladder.select(TaskRequirements(), now=NOW)
    assert selected is not None and selected.route_id == "cc/rated"
    assert next(v for v in verdicts if v.route_id == "free/unrated").reason == "no_agentic_probe"


def test_missing_price_is_assumed_free_but_still_requires_screen(tmp_path):
    ladder = EligibilityLadder(
        [_ladder_row("p/unknown", pricing={})],
        [_conn("p", auth="apikey")],
        lambda _: HealthResult(True, "ok"),
        tmp_path / "state.json",
        health_cache=HealthCache(tmp_path / "health.json"),
        metadata=None,
    )
    assert ladder.capacity_class("p/unknown") is CapacityClass.FREE
    selected, verdicts = ladder.select(TaskRequirements(), now=NOW)
    assert selected is None
    assert verdicts[0].reason == "no_agentic_probe"
    assert verdicts[0].capacity_evidence == "assumed_free_no_paid_evidence"


def test_specific_paid_and_subscription_evidence_keeps_its_class(tmp_path):
    rows = [
        _ladder_row(r, pricing=price)
        for r, price in [
            ("anthropic/model", {}),
            ("op/paid", {}),
            ("cc/sub", {}),
            ("paid/model", {"input": 1}),
            ("p/unknown", {}),
        ]
    ]
    rows[1]["owned_by"] = "openrouter"
    ladder = EligibilityLadder(
        rows,
        [
            _conn("anthropic", auth="apikey"),
            _conn("openrouter", auth="apikey"),
            _conn("cc", auth="oauth"),
            _conn("paid", auth="apikey"),
            _conn("p", auth="apikey"),
        ],
        lambda _: HealthResult(True, "ok"),
        tmp_path / "state.json",
        metadata=None,
    )
    assert [ladder.capacity_class(row["id"]) for row in rows] == [
        CapacityClass.METERED,
        CapacityClass.METERED,
        CapacityClass.SUBSCRIPTION,
        CapacityClass.METERED,
        CapacityClass.FREE,
    ]


def test_probation_selection_survives_receipt_and_attempt_checker_rows(tmp_path):
    import json

    from verdict.orchestration.contracts import WorkGraph, WorkNode
    from verdict.orchestration.receipt import EventLog, build_run_receipt, run_attempt_records

    events = EventLog(tmp_path / "events.jsonl")
    events.emit("selection", "n", route_id="free/model", evidence_level="probation")
    events.emit("dispatch", "n", route_id="free/model", attempt=1)
    events.emit("terminal", "n", route_id="free/model", attempt=1, ok=True)
    events.emit("verify", "n", command="pytest", ok=True)
    graph = WorkGraph(
        "g", (WorkNode("n", "tiny edit", owned_files=("n.py",), verification_command=("pytest",)),)
    )
    (tmp_path / "graph.json").write_text(json.dumps(graph.to_dict()))
    receipt = build_run_receipt(tmp_path)
    assert receipt["nodes"][0]["attempts"][0]["evidence_level"] == "probation"
    records = run_attempt_records(tmp_path, receipt)
    assert {r.record_kind for r in records} == {"terminal", "checker_result"}
    assert all(r.evidence_level == "probation" for r in records)


def test_unlabelled_attempt_keeps_existing_checker_serialization():
    from verdict.outcome_records import build_attempt_record

    record = build_attempt_record(
        task_kind="implement",
        harness="fixture",
        model="free/model",
        outcome="accepted",
        failure_class="success",
        source="fixture",
    )
    assert "evidence_level" not in record.to_dict()
