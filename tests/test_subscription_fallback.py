"""D8: qualified subscription fallback, bounded within the request budget."""

from dataclasses import replace

import pytest

from tests import test_live_candidate_shortlist as live
from tests.test_identify_rank import NOW, _conn, _ladder_row, _record, _score, _snapshot
from tests.test_serve_capacity_order import _context, _service
from verdict.orchestration.contracts import TaskRequirements
from verdict.orchestration.eligibility import EligibilityLadder
from verdict.orchestration.health_cache import HealthCache
from verdict.subagent_selection import HealthResult


def _usage(route, total, n=3, family="code_edit"):
    from verdict.candidate_pool import OutcomeObservation

    return tuple(
        OutcomeObservation(
            route_id=route,
            task_family=family,
            success=True,
            quality_credit=1.0,
            total_tokens=total,
            observation_id=f"{route}:{i}",
        )
        for i in range(n)
    )


def _history(monkeypatch, observations):
    from types import SimpleNamespace

    families = []

    def load(*args, **kwargs):
        families.append(kwargs["task_family"])
        return SimpleNamespace(observations=observations, omissions=())

    monkeypatch.setattr("verdict.outcome_shadow.load_verified_outcome_observations", load)
    monkeypatch.setattr(
        "verdict.intelligence.load_verified_outcome_observations",
        lambda *a, **k: SimpleNamespace(observations=observations, omissions=()),
    )
    return families


@pytest.mark.parametrize("frontier_worthy", [False, True])
def test_subscription_token_usage_precedes_rating_and_unknown(
    tmp_path, monkeypatch, frontier_worthy
):
    ids = ("a/strong", "b/capable", "c/unknown", "d/incapable")
    rows = [_ladder_row(r, pricing={"input": 1}, tier=2) for r in ids]
    rows[-1]["capability_tier"] = 3
    conns = [_conn(r.split("/")[0], auth="oauth") for r in ids]
    _history(monkeypatch, _usage(ids[0], 10000) + _usage(ids[1], 1000) + _usage(ids[3], 100))
    ladder = EligibilityLadder(
        rows,
        conns,
        lambda _: HealthResult(True, "ok"),
        tmp_path / "state.json",
        health_cache=HealthCache(tmp_path / "health.json"),
        metadata=_snapshot(_record("a/strong", ids[0], aa_coding=_score(99))),
    )
    selected, _ = ladder.select(
        TaskRequirements(max_capability_tier=2, frontier_worthy=frontier_worthy), now=NOW
    )
    assert selected is not None and selected.route_id == ids[1]


@pytest.mark.parametrize(
    "goal, family", [("investigate findings", "general"), ("fix code", "code_edit")]
)
async def test_planner_subscription_usage_uses_goal_family(tmp_path, monkeypatch, goal, family):
    from verdict.orchestration.contracts import NodeKind, WorkerTerminal, WorkGraph, WorkNode
    from verdict.orchestration.receipt import EventLog
    from verdict.orchestration.recovery import FailureIntelligence
    from verdict.orchestration.run import plan_with_failover

    ids = ("a/strong", "b/capable")
    other_family = "code_edit" if family == "general" else "general"
    families = _history(
        monkeypatch,
        _usage(ids[0], 10000, family=family)
        + _usage(ids[1], 1000, family=family)
        + _usage(ids[0], 100, family=other_family)
        + _usage(ids[1], 20000, family=other_family),
    )
    rows = [_ladder_row(r, pricing={"input": 1}, tier=2) for r in ids]
    for row in rows:
        row["capabilities"]["reasoning"] = True
    selector = EligibilityLadder(
        rows,
        [_conn(r.split("/")[0], auth="oauth") for r in ids],
        lambda _: HealthResult(True, "ok"),
        tmp_path / "state.json",
        metadata=_snapshot(_record("a/strong", ids[0], aa_coding=_score(99))),
    )

    async def plan(self, goal, **kwargs):
        return WorkGraph(
            goal, (WorkNode("r", "research", kind=NodeKind.RESEARCH),)
        ), WorkerTerminal(ok=True)

    monkeypatch.setattr("verdict.orchestration.planner.FrontierPlanner.plan", plan)
    events = EventLog(tmp_path / "events.jsonl")
    await plan_with_failover(
        goal,
        repo=tmp_path,
        selector=selector,
        executor=None,
        classifier=FailureIntelligence(),
        events=events,
        now=lambda: NOW,
    )
    event = next(e for e in events.read() if e.type == "plan_started")
    assert event.data["route_id"] == ids[1]
    assert families == [family]


@pytest.mark.parametrize(
    "failing_function",
    [
        "verdict.outcome_shadow.load_verified_outcome_observations",
        "verdict.candidate_pool.expected_token_usage",
    ],
)
def test_ladder_history_failure_logs_and_resets_usage(
    tmp_path, monkeypatch, caplog, failing_function
):
    ids = ("a/strong", "b/capable")
    _history(monkeypatch, _usage(ids[0], 10000) + _usage(ids[1], 1000))
    selector = EligibilityLadder(
        [_ladder_row(r, pricing={"input": 1}, tier=2) for r in ids],
        [_conn(r.split("/")[0], auth="oauth") for r in ids],
        lambda _: HealthResult(True, "ok"),
        tmp_path / "state.json",
        metadata=_snapshot(_record("a/strong", ids[0], aa_coding=_score(99))),
    )
    selected, _ = selector.select(TaskRequirements(), now=NOW)
    assert selected.route_id == ids[1]

    def fail(*args, **kwargs):
        raise OSError("history store unavailable")

    monkeypatch.setattr(failing_function, fail)
    selected, verdicts = selector.select(TaskRequirements(), now=NOW)
    assert selected.route_id == ids[0]
    assert all(v.rank_components["expected_tokens"] is None for v in verdicts)
    assert "history store unavailable" in caplog.text


def test_serve_subscription_tokens_before_rating(monkeypatch):
    service = _service()
    _history(
        monkeypatch,
        _usage(live.STRONG, 1000, family="coding") + _usage(live.NEW, 10000, family="coding"),
    )
    request = live._request(live.STRONG, live.NEW, live.NO_TOOLS)
    prepared = service._prepare_execution_path_request(
        "fix code", "low", _context(request), request
    )
    assert prepared.pool_receipt.shortlist[0].route_id == live.STRONG
    assert live.NO_TOOLS in prepared.hard_excluded_ids


def test_subscription_reserved_inside_full_free_shortlist():
    from verdict.candidate_pool import build_candidate_pool
    from verdict.capacity import CapacityClass

    ids = (live.STRONG, live.FAST, live.NEW)
    pool = build_candidate_pool(
        ids,
        task="fix code",
        metadata=live._metadata(),
        top_k=2,
        capacity={
            live.STRONG: CapacityClass.SUBSCRIPTION,
            live.FAST: CapacityClass.FREE,
            live.NEW: CapacityClass.FREE,
        },
    )
    assert [r.route_id for r in pool.shortlist] == [live.NEW, live.STRONG]


def test_paid_is_last_resort_when_no_free_or_subscription_qualifies(tmp_path):
    ladder = EligibilityLadder(
        [_ladder_row("paid/model", pricing={"input": 1})],
        [_conn("paid", auth="apikey")],
        lambda _: HealthResult(True, "ok"),
        tmp_path / "state.json",
        metadata=None,
    )
    selected, _ = ladder.select(TaskRequirements(), now=NOW)
    assert selected is not None and selected.route_id == "paid/model"


def test_relay_all_free_fail_then_subscription(monkeypatch, tmp_path):
    from datetime import datetime, timezone

    from fastapi.testclient import TestClient

    from tests.test_api_relay_context_length import ContextLengthTransport
    from verdict import api
    from verdict.proxy import UpstreamProxy

    monkeypatch.setattr(live, "NOW", datetime.now(timezone.utc))
    service = _service()
    service.candidate_top_k = 3
    service.admit_snapshot = replace(
        service.admit_snapshot,
        connections=tuple(
            replace(c, auth_type="apikey" if c.provider in ("speed", "newco") else "oauth")
            for c in service.admit_snapshot.connections
        ),
    )
    transport = ContextLengthTransport([(503, '{"error":{"message":"unavailable"}}')] * 2)
    request = live._request(live.STRONG, live.FAST, live.NEW, live.NO_TOOLS)
    events = []
    original = api._finish_evidence

    def capture(evidence, outcome, key):
        events.append(outcome.to_dict())
        return original(evidence, outcome, key)

    monkeypatch.setattr(api, "_finish_evidence", capture)
    monkeypatch.setattr(api, "_build_intelligence", lambda: service)
    monkeypatch.setattr(api, "_authority_context", lambda payload, task: _context(request))
    monkeypatch.setattr(
        api, "_build_proxy", lambda: UpstreamProxy("http://upstream.test/v1", transport=transport)
    )
    monkeypatch.setenv("LLMGATE_ALLOW_ANONYMOUS", "true")
    monkeypatch.setenv("LLMGATE_LOG_PATH", "")
    with TestClient(api.app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "fix code"}]},
            headers={"idempotency-key": "s3"},
        )
    assert response.status_code == 200, response.text
    assert [r["body"]["model"] for r in transport.requests] == [
        live.NEW.split("/")[1],
        live.FAST.split("/")[1],
        live.STRONG.split("/")[1],
    ]
    assert any(
        row.get("details", {}).get("fallback") == "subscription"
        and row["details"]["reason"] == "free attempts failed"
        and row["details"]["attempt"] == 2
        for row in events
    )
    from verdict.orchestration.health_cache import HealthCache, default_cache_path

    assert HealthCache(default_cache_path()).rescreen_requests() == {live.FAST, live.NEW}
    assert all(r["body"]["model"] != live.NO_TOOLS.split("/")[1] for r in transport.requests)


def test_capability_rejection_steps_up_not_same_route(monkeypatch):
    from fastapi.testclient import TestClient

    from tests.test_api_relay_context_length import ContextLengthTransport, TwoModelIntelligence
    from verdict import api
    from verdict.proxy import UpstreamProxy

    transport = ContextLengthTransport([(400, '{"error":{"code":"tools_not_supported"}}')])
    monkeypatch.setattr(api, "_build_intelligence", lambda: TwoModelIntelligence())
    monkeypatch.setattr(
        api, "_build_proxy", lambda: UpstreamProxy("http://upstream.test/v1", transport=transport)
    )
    monkeypatch.setenv("LLMGATE_ALLOW_ANONYMOUS", "true")
    monkeypatch.setenv("LLMGATE_LOG_PATH", "")
    with TestClient(api.app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "preserve all fields"}]},
            headers={"idempotency-key": "s3-capability"},
        )
    assert response.status_code == 200
    assert [r["body"]["model"] for r in transport.requests] == ["selected-model", "backup-model"]


def test_queued_free_screen_runs_first_without_extending_caps(tmp_path):
    from tests.test_identify_screen import NOW as SCREEN_NOW
    from tests.test_identify_screen import passing, ratings, snapshot
    from verdict.identify import screen_models
    from verdict.orchestration.health_cache import HealthCache

    cache = HealthCache(tmp_path / "health.json")
    cache.request_rescreen(("p/strong",), now=SCREEN_NOW)
    assert HealthCache(cache.path).rescreen_requests() == {"p/strong"}
    evidence = snapshot()
    evidence = replace(evidence, route_capacities={r: "free" for r in evidence.proven_by_traffic})
    calls = []
    report = screen_models(
        evidence,
        metadata=ratings(),
        cache=cache,
        transport=passing(calls),
        allow_live_probe=True,
        max_per_provider=1,
        max_routes=1,
        clock=lambda: SCREEN_NOW,
    )
    assert [r for r, _ in calls] == ["p/strong"] * 3
    assert report["routes_checked"] == 1
    assert not HealthCache(cache.path).rescreen_requests()


def test_token_metric_requires_three_unique_accepted_same_family_samples():
    from verdict.candidate_pool import expected_token_usage

    a = "a/model"
    assert expected_token_usage(_usage(a, 100, n=2), task_family="code_edit") == {}
    obs = _usage(a, 100) + _usage(a, 999, n=1)
    # The duplicate observation cannot increase sample count.
    assert expected_token_usage(obs, task_family="general") == {}
    assert expected_token_usage(_usage(a, 100), task_family="code_edit") == {a: 100.0}
    assert (
        expected_token_usage(
            tuple(replace(r, quality_credit=0) for r in _usage(a, 100)), task_family="code_edit"
        )
        == {}
    )


def test_no_capable_subscription_uses_capable_paid_last_resort(monkeypatch):
    from datetime import datetime, timezone

    from fastapi.testclient import TestClient

    from tests.test_api_relay_context_length import ContextLengthTransport
    from verdict import api
    from verdict.proxy import UpstreamProxy

    monkeypatch.setattr(live, "NOW", datetime.now(timezone.utc))
    service = _service()
    # Strong is explicitly metered; the other route cannot use the required tools.
    service.admit_snapshot = replace(
        service.admit_snapshot,
        connections=tuple(
            replace(c, auth_type="apikey") for c in service.admit_snapshot.connections
        ),
        catalog=tuple(
            replace(c, price_known=True, input_cost=1.0, output_cost=1.0)
            if c.identity_id == live.STRONG
            else c
            for c in service.admit_snapshot.catalog
        ),
    )
    request = live._request(live.STRONG, live.NO_TOOLS)
    transport = ContextLengthTransport([])
    events = []
    original = api._finish_evidence

    def capture(evidence, outcome, key):
        events.append(outcome.to_dict())
        return original(evidence, outcome, key)

    monkeypatch.setattr(api, "_finish_evidence", capture)
    monkeypatch.setattr(api, "_build_intelligence", lambda: service)
    monkeypatch.setattr(api, "_authority_context", lambda payload, task: _context(request))
    monkeypatch.setattr(
        api, "_build_proxy", lambda: UpstreamProxy("http://upstream.test/v1", transport=transport)
    )
    monkeypatch.setenv("LLMGATE_ALLOW_ANONYMOUS", "true")
    monkeypatch.setenv("LLMGATE_LOG_PATH", "")
    with TestClient(api.app) as client:
        response = client.post(
            "/v1/chat/completions", json={"messages": [{"role": "user", "content": "fix code"}]}
        )
    assert response.status_code == 200, response.text
    assert [row["body"]["model"] for row in transport.requests] == [live.STRONG.split("/")[1]]
    assert any(
        row.get("details", {}).get("fallback") == "metered" and row["details"]["attempt"] == 0
        for row in events
    )


def test_known_subscription_tokens_always_before_unknown(monkeypatch):
    service = _service()
    _history(monkeypatch, _usage(live.STRONG, 10000, family="coding"))
    request = live._request(live.STRONG, live.NEW)
    prepared = service._prepare_execution_path_request(
        "fix code", "low", _context(request), request
    )
    assert prepared.pool_receipt.shortlist[0].route_id == live.STRONG


def test_subscription_token_tie_uses_rating(monkeypatch):
    service = _service()
    _history(
        monkeypatch,
        _usage(live.STRONG, 10000, family="coding") + _usage(live.NEW, 10000, family="coding"),
    )
    request = live._request(live.STRONG, live.NEW)
    prepared = service._prepare_execution_path_request(
        "fix code", "low", _context(request), request
    )
    assert prepared.pool_receipt.shortlist[0].route_id == live.NEW


def test_capable_metered_pool_and_ladder_remain_allowed(tmp_path):
    from verdict.candidate_pool import build_candidate_pool
    from verdict.capacity import CapacityClass

    pool = build_candidate_pool(
        (live.STRONG,),
        task="fix code",
        metadata=live._metadata(),
        capacity={live.STRONG: CapacityClass.METERED},
    )
    assert [row.route_id for row in pool.shortlist] == [live.STRONG]
    ladder = EligibilityLadder(
        [_ladder_row("paid/model", pricing={"input": 1})],
        [_conn("paid", auth="apikey")],
        lambda _: HealthResult(True, "ok"),
        tmp_path / "state.json",
        metadata=None,
    )
    selected, _ = ladder.select(TaskRequirements(), now=NOW)
    assert selected is not None and selected.route_id == "paid/model"


def test_active_payment_negative_still_blocks_metered(tmp_path):
    from verdict.orchestration.health_cache import ProbeResult

    cache = HealthCache(tmp_path / "health.json")
    cache.record(
        "paid/model",
        ProbeResult(category="payment_required", chat_ok=False, tool_ok=False, http_status=402),
        NOW,
    )
    cache.save()
    ladder = EligibilityLadder(
        [_ladder_row("paid/model", pricing={"input": 1})],
        [_conn("paid", auth="apikey")],
        lambda _: HealthResult(True, "ok"),
        tmp_path / "state.json",
        health_cache=cache,
        metadata=None,
    )
    selected, verdicts = ladder.select(TaskRequirements(), now=NOW)
    assert selected is None
    assert verdicts[0].reason == "payment_required"


def test_usage_is_recomputed_from_verified_run_not_ledger_label(tmp_path, monkeypatch):
    import json
    from datetime import datetime, timezone

    from verdict.candidate_pool import fingerprint_task
    from verdict.orchestration.contracts import WorkGraph, WorkNode
    from verdict.orchestration.receipt import EventLog, write_run_receipt
    from verdict.outcome_shadow import load_verified_outcome_observations

    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / "store"))
    run = tmp_path / "run"
    run.mkdir()
    graph = WorkGraph(
        "fix Python code",
        (WorkNode("a", "fix function", owned_files=("a.py",), verification_command=("pytest",)),),
    )
    (run / "graph.json").write_text(json.dumps(graph.to_dict()))
    now = datetime.now(timezone.utc)
    log = EventLog(run / "events.jsonl", clock=lambda: now)
    log.emit("run_started", run_id="usage-run", goal=graph.goal)
    log.emit("dispatch", node_id="a", attempt=1, route_id="sub/model")
    log.emit(
        "terminal",
        node_id="a",
        attempt=1,
        ok=True,
        reported_model="sub/model",
        usage={"input_tokens": 100, "output_tokens": 50},
    )
    log.emit("verify", node_id="a", ok=True, command="pytest", exit_code=0)
    log.emit("node_state", node_id="a", attempt=1, state="VALIDATED")
    log.emit("run_finished", outcome="COMPLETE", reason="")
    write_run_receipt(run)
    family = fingerprint_task(graph.goal).task_family
    history = load_verified_outcome_observations([run], task_family=family, as_of=now)
    assert len(history.observations) == 1
    assert history.observations[0].total_tokens == 150
    assert history.observations[0].quality_credit == 1
    # Altering the event tokens invalidates the bundle; no guessed total remains.
    events = run / "events.jsonl"
    event_rows = [json.loads(line) for line in events.read_text().splitlines()]
    for row in event_rows:
        if row.get("event") == "terminal" or row.get("type") == "terminal":
            row.setdefault("usage", {})["input_tokens"] = 1
    events.write_text("\n".join(json.dumps(row) for row in event_rows) + "\n")
    assert not load_verified_outcome_observations([run], task_family=family, as_of=now).observations


def test_relay_three_tiers_stay_within_three_attempts():
    from verdict.models import RoutingDecision
    from verdict.proxy import UpstreamProxy
    from verdict.relay import build_attempts

    ids = ("free/a", "free/b", "sub/a", "paid/a")
    decision = RoutingDecision(
        model=ids[0],
        provider="free",
        tier=2,
        reason="fixture",
        alternatives=list(ids[1:]),
        safety_flags=["subscription_fallback_policy"],
        candidate_states=[
            {"model_id": model, "admitted": True, "capacity_order": tier}
            for model, tier in zip(ids, (0, 0, 1, 2), strict=True)
        ],
    )
    proxy = UpstreamProxy("http://upstream.test/v1")
    attempts = build_attempts(proxy, decision, protocol="openai.chat.completions")
    assert [attempt.model for attempt in attempts] == [ids[0], ids[2], ids[3]]
