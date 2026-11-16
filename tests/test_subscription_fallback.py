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
    """Build distinct accepted attempt observations with a fixed token total."""
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
    """Supply synthetic verified history to the ladder and serving service."""
    from types import SimpleNamespace

    families = []

    def load(*args, **kwargs):
        """Record the requested task family and return synthetic verified observations."""
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
    """Prefer lower measured subscription usage while excluding incapable routes."""
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
    """Rank planning subscriptions using token history for the goal task family."""
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
        """Return a synthetic research plan without invoking an external model."""
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
    """Clear prior token estimates and log a warning when usage history fails."""
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
        """Simulate an unavailable usage history store or token estimator."""
        raise OSError("history store unavailable")

    monkeypatch.setattr(failing_function, fail)
    selected, verdicts = selector.select(TaskRequirements(), now=NOW)
    assert selected.route_id == ids[0]
    assert all(v.rank_components["expected_tokens"] is None for v in verdicts)
    assert "history store unavailable" in caplog.text


def test_serve_subscription_tokens_before_rating(monkeypatch):
    """Rank serving candidates by subscription tokens and preserve tool exclusions."""
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


def test_free_candidates_fill_shortlist_before_subscription():
    """Fill the shortlist with free routes when enough qualify."""
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
    assert [r.route_id for r in pool.shortlist] == [live.NEW, live.FAST]


def test_paid_is_last_resort_when_no_free_or_subscription_qualifies(tmp_path):
    """Allow a capable metered route when no cheaper capacity class qualifies."""
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
    """Fall back after two free failures, recording the reason and rescreen hints."""
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
        """Capture fallback evidence while preserving normal evidence finalization."""
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
    """Retry unsupported tools on a different admitted route within the request."""
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
    """Prioritize a queued free route within screen caps and consume its hint."""
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
    """Omit token estimates for sparse, unaccepted, or mismatched task history."""
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
    """Serve a capable metered route when the alternative lacks required tools."""
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
        """Capture metered fallback evidence while preserving normal finalization."""
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
    """Rank a measured subscription ahead of one with unknown token usage."""
    service = _service()
    _history(monkeypatch, _usage(live.STRONG, 10000, family="coding"))
    request = live._request(live.STRONG, live.NEW)
    prepared = service._prepare_execution_path_request(
        "fix code", "low", _context(request), request
    )
    assert prepared.pool_receipt.shortlist[0].route_id == live.STRONG


def _usage_series(route, totals, family="code_edit"):
    """Distinct accepted attempts with varying token totals for a real median."""
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
        for i, total in enumerate(totals)
    )


def _subscription_order(tmp_path, monkeypatch, quotas, observations):
    """Rank subscription routes once, with the given per-provider quota headroom."""
    _history(monkeypatch, observations)
    ids = ("aaa/low", "bbb/high", "ccc/sparse")
    rows = [_ladder_row(r, pricing={"input": 1}, tier=2) for r in ids]
    conns = []
    for route in ids:
        provider = route.split("/", 1)[0]
        conn = _conn(provider, auth="oauth")
        conn["quota_percent"] = quotas[provider]
        conns.append(conn)
    ladder = EligibilityLadder(
        rows,
        conns,
        lambda _: HealthResult(True, "ok"),
        tmp_path / f"state-{','.join(str(q) for q in quotas.values())}.json",
        metadata=_snapshot(_record("aaa/low", ids[0], aa_coding=_score(90))),
    )
    verdicts = ladder.evaluate(TaskRequirements(), now=NOW)
    ranked = sorted((v for v in verdicts if v.rank is not None), key=lambda v: v.rank or 0)
    return [v.route_id for v in ranked], {v.route_id: v for v in verdicts}


def test_subscription_order_follows_token_median_not_quota_headroom(tmp_path, monkeypatch):
    """Measured subscription usage orders routes; remaining quota never does."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / "verdict-home"))
    ids = ("aaa/low", "bbb/high", "ccc/sparse")
    observations = (
        _usage_series(ids[0], (800, 1000, 1400))  # median 1000
        + _usage_series(ids[1], (2000, 3000, 4000))  # median 3000
        + _usage_series(ids[2], (100, 200))  # below the three-sample minimum
    )
    # Sparse route gets the most headroom; measured routes get the least.
    order_a, verdicts_a = _subscription_order(
        tmp_path, monkeypatch, {"aaa": 10.0, "bbb": 20.0, "ccc": 95.0}, observations
    )
    assert order_a == [ids[0], ids[1], ids[2]]
    assert verdicts_a[ids[0]].rank_components["expected_tokens"] == 1000
    assert verdicts_a[ids[1]].rank_components["expected_tokens"] == 3000
    assert verdicts_a[ids[2]].rank_components["expected_tokens"] is None
    # Flip every headroom value; the order and token estimates must not move.
    order_b, verdicts_b = _subscription_order(
        tmp_path, monkeypatch, {"aaa": 95.0, "bbb": 5.0, "ccc": 50.0}, observations
    )
    assert order_b == order_a
    for route in ids:
        assert (
            verdicts_b[route].rank_components["expected_tokens"]
            == verdicts_a[route].rank_components["expected_tokens"]
        )
    assert verdicts_b[ids[0]].rank_components["quota_percent"] == 95.0
    assert verdicts_b[ids[2]].rank_components["quota_percent"] == 50.0


def test_subscription_token_tie_uses_rating(monkeypatch):
    """Use rating to break ties between equal subscription token estimates."""
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
    """Keep capable metered routes eligible in both the pool and ladder."""
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
    """Reject metered fallback when cached evidence reports payment required."""
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
    """Extract checked token totals from verified events and reject tampered events."""
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
    """Reserve subscription and metered fallback within three total relay attempts."""
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
    assert [attempt.model for attempt in attempts] == [ids[0], ids[1], ids[2]]


def test_real_preparation_fences_outside_shortlist_free_proof(monkeypatch):
    import asyncio
    from datetime import datetime, timezone

    monkeypatch.setattr(live, "NOW", datetime.now(timezone.utc))
    service = _service()
    service.candidate_top_k = 1
    service.admit_snapshot = replace(
        service.admit_snapshot,
        connections=tuple(
            replace(c, auth_type="apikey" if c.provider in ("speed", "newco") else "oauth")
            for c in service.admit_snapshot.connections
        ),
    )
    request = live._request(live.STRONG, live.FAST, live.NEW)
    context = _context(request)
    decision = asyncio.run(service.route("fix code", criticality="low", context=context))
    assert "free_supply_unknown" in decision.safety_flags
    assert decision.model in {live.NEW.split("/")[1], live.FAST.split("/")[1]}


def test_real_pool_marks_omitted_free_routes_as_possible():
    from verdict.candidate_pool import build_candidate_pool
    from verdict.capacity import CapacityClass

    pool = build_candidate_pool(
        (live.STRONG, live.FAST, live.NEW),
        task="fix code",
        metadata=live._metadata(),
        top_k=1,
        capacity={
            live.STRONG: CapacityClass.SUBSCRIPTION,
            live.FAST: CapacityClass.FREE,
            live.NEW: CapacityClass.FREE,
        },
    )
    assert set(pool.possible_free_routes) == {live.FAST, live.NEW}
    assert len(pool.shortlist) == 1


def test_failed_free_qualification_is_not_possible_free_supply():
    from verdict.candidate_pool import (
        HEALTH_OK,
        PROBE_SMALL_QUAL,
        ProbeBudget,
        RouteHealth,
        build_candidate_pool,
    )
    from verdict.capacity import CapacityClass

    ids = (live.FAST, live.STRONG)
    pool = build_candidate_pool(
        ids,
        task="fix code",
        metadata=live._metadata(),
        top_k=2,
        capacity={live.FAST: CapacityClass.FREE, live.STRONG: CapacityClass.SUBSCRIPTION},
        health={route: RouteHealth(state=HEALTH_OK) for route in ids},
        probe_budget=ProbeBudget(max_probes=2, max_level=PROBE_SMALL_QUAL),
        probe_fn=lambda route, _level: {"ok": route != live.FAST, "qualified": route != live.FAST},
        require_min_qualification=True,
    )
    assert any(probe.route_id == live.FAST and not probe.passed for probe in pool.probes)
    assert [row.route_id for row in pool.shortlist] == [live.STRONG]
    assert pool.possible_free_routes == ()


def test_free_probe_without_qualification_keeps_supply_unknown():
    from verdict.candidate_pool import (
        HEALTH_OK,
        PROBE_SMALL_QUAL,
        ProbeBudget,
        RouteHealth,
        build_candidate_pool,
    )
    from verdict.capacity import CapacityClass

    ids = (live.FAST, live.STRONG)
    pool = build_candidate_pool(
        ids,
        task="fix code",
        metadata=live._metadata(),
        top_k=2,
        capacity={live.FAST: CapacityClass.FREE, live.STRONG: CapacityClass.SUBSCRIPTION},
        health={route: RouteHealth(state=HEALTH_OK) for route in ids},
        probe_budget=ProbeBudget(max_probes=2, max_level=PROBE_SMALL_QUAL),
        probe_fn=lambda route, _level: (
            {"ok": False, "detail": "probe unavailable"}
            if route == live.FAST
            else {"ok": True, "qualified": True}
        ),
        require_min_qualification=True,
    )
    assert [row.route_id for row in pool.shortlist] == [live.STRONG]
    assert pool.possible_free_routes == (live.FAST,)


def test_relay_allows_subscription_when_free_passport_is_denied(monkeypatch, tmp_path):
    from datetime import datetime, timezone

    from fastapi.testclient import TestClient

    from tests.test_api_relay_context_length import ContextLengthTransport
    from verdict import api
    from verdict.proxy import UpstreamProxy

    monkeypatch.setattr(live, "NOW", datetime.now(timezone.utc))
    service = _service()
    service.passports[live.FAST] = replace(
        service.passports[live.FAST], availability_state="denied"
    )
    request = live._request(live.STRONG, live.FAST)
    transport = ContextLengthTransport([])
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path))
    monkeypatch.setenv("LLMGATE_ALLOW_ANONYMOUS", "true")
    monkeypatch.setenv("LLMGATE_LOG_PATH", "")
    monkeypatch.setattr(api, "_build_intelligence", lambda: service)
    monkeypatch.setattr(api, "_authority_context", lambda payload, task: _context(request))
    monkeypatch.setattr(
        api, "_build_proxy", lambda: UpstreamProxy("http://upstream.test/v1", transport=transport)
    )
    with TestClient(api.app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "fix code"}]},
            headers={"idempotency-key": "denied-free"},
        )
    assert response.status_code == 200, response.text
    assert [row["body"]["model"] for row in transport.requests] == [live.STRONG.split("/")[1]]


@pytest.mark.parametrize("stale_verification", [False, True])
def test_relay_denies_subscription_when_free_denial_is_stale(
    monkeypatch, tmp_path, stale_verification
):
    from datetime import datetime, timedelta, timezone

    from fastapi.testclient import TestClient

    from tests.test_api_relay_context_length import ContextLengthTransport
    from verdict import api
    from verdict.proxy import UpstreamProxy

    monkeypatch.setattr(live, "NOW", datetime.now(timezone.utc))
    service = _service()
    passport = (
        replace(service.passports[live.FAST], last_verified_timestamp=live.NOW - timedelta(hours=1))
        if stale_verification
        else live._passport(live.FAST, latency_ms=1, stale=True)
    )
    service.passports[live.FAST] = replace(passport, availability_state="denied")
    request = live._request(live.STRONG, live.FAST)
    transport = ContextLengthTransport([])
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path))
    monkeypatch.setenv("LLMGATE_ALLOW_ANONYMOUS", "true")
    monkeypatch.setenv("LLMGATE_LOG_PATH", "")
    monkeypatch.setattr(api, "_build_intelligence", lambda: service)
    monkeypatch.setattr(api, "_authority_context", lambda payload, task: _context(request))
    monkeypatch.setattr(
        api, "_build_proxy", lambda: UpstreamProxy("http://upstream.test/v1", transport=transport)
    )
    with TestClient(api.app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "fix code"}]},
            headers={"idempotency-key": "stale-denied-free"},
        )
    assert response.status_code == 503
    assert transport.requests == []


def test_relay_does_not_escalate_when_free_supply_is_unknown():
    from verdict.models import RoutingDecision
    from verdict.proxy import UpstreamProxy
    from verdict.relay import build_attempts

    decision = RoutingDecision(
        model="free/a",
        provider="free",
        tier=2,
        reason="fixture",
        alternatives=["sub/a"],
        safety_flags=["subscription_fallback_policy", "free_supply_unknown"],
        candidate_states=[
            {"model_id": "free/a", "admitted": True, "capacity_order": 0},
            {"model_id": "sub/a", "admitted": True, "capacity_order": 1},
        ],
    )
    attempts = build_attempts(
        UpstreamProxy("http://upstream.test/v1"), decision, protocol="openai.chat.completions"
    )
    assert [attempt.model for attempt in attempts] == ["free/a"]


def test_relay_three_free_routes_consume_budget_before_higher_tiers():
    from verdict.models import RoutingDecision
    from verdict.proxy import UpstreamProxy
    from verdict.relay import build_attempts

    ids = ("free/a", "free/b", "free/c", "sub/a", "paid/a")
    decision = RoutingDecision(
        model=ids[0],
        provider="free",
        tier=2,
        reason="fixture",
        alternatives=list(ids[1:]),
        safety_flags=["subscription_fallback_policy"],
        candidate_states=[
            {"model_id": model, "admitted": True, "capacity_order": tier}
            for model, tier in zip(ids, (0, 0, 0, 1, 2), strict=True)
        ],
    )
    attempts = build_attempts(
        UpstreamProxy("http://upstream.test/v1"), decision, protocol="openai.chat.completions"
    )
    assert [attempt.model for attempt in attempts] == list(ids[:3])


def test_relay_duplicate_models_do_not_consume_attempt_slots():
    from verdict.models import RoutingDecision
    from verdict.proxy import UpstreamProxy
    from verdict.relay import build_attempts

    ids = ("free/a", "free/b", "sub/a")
    decision = RoutingDecision(
        model=ids[0],
        provider="free",
        tier=2,
        reason="fixture",
        alternatives=[ids[0], ids[1], ids[1], ids[2]],
        safety_flags=["subscription_fallback_policy"],
        candidate_states=[
            {"model_id": model, "admitted": True, "capacity_order": tier}
            for model, tier in zip(ids, (0, 0, 1), strict=True)
        ],
    )
    attempts = build_attempts(
        UpstreamProxy("http://upstream.test/v1"), decision, protocol="openai.chat.completions"
    )
    assert [attempt.model for attempt in attempts] == list(ids)


def test_relay_does_not_escalate_after_free_proof_denial(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from tests.relay_proof_helpers import with_relay_proofs
    from tests.test_api_relay_context_length import ContextLengthTransport
    from tests.test_proxy import FixedIntelligence
    from verdict import api
    from verdict.models import RoutingDecision
    from verdict.proxy import UpstreamProxy

    ids = ("free-a", "free-b", "sub-a")

    class Intelligence(FixedIntelligence):
        async def route(self, task, criticality="medium", context=None, *, request_id=None):
            decision = with_relay_proofs(
                RoutingDecision(
                    model=ids[0],
                    provider="omniroute",
                    tier=2,
                    reason="test",
                    alternatives=[ids[2]],
                    candidate_states=[{"model_id": ids[2], "admitted": True}],
                ),
                context,
            )
            return replace(
                decision,
                alternatives=list(ids[1:]),
                safety_flags=["subscription_fallback_policy"],
                candidate_states=[
                    {"model_id": model, "admitted": True, "capacity_order": tier}
                    for model, tier in zip(ids, (0, 0, 1), strict=True)
                ],
            )

    transport = ContextLengthTransport([(503, '{"error":"unavailable"}')])
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path))
    monkeypatch.setenv("LLMGATE_ALLOW_ANONYMOUS", "true")
    monkeypatch.setenv("LLMGATE_LOG_PATH", "")
    monkeypatch.setattr(api, "_build_intelligence", lambda: Intelligence())
    monkeypatch.setattr(
        api, "_build_proxy", lambda: UpstreamProxy("http://upstream.test/v1", transport=transport)
    )
    with TestClient(api.app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "fix code"}]},
            headers={"idempotency-key": "proof-denied"},
        )
    assert response.status_code == 503
    assert [row["body"]["model"] for row in transport.requests] == [ids[0]]


def test_relay_three_free_failures_report_budget_exhaustion(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from tests.relay_proof_helpers import with_relay_proofs
    from tests.test_api_relay_context_length import ContextLengthTransport
    from tests.test_proxy import FixedIntelligence
    from verdict import api
    from verdict.models import RoutingDecision
    from verdict.proxy import UpstreamProxy

    ids = ("free-a", "free-b", "free-c", "sub-a", "paid-a")

    class Intelligence(FixedIntelligence):
        async def route(self, task, criticality="medium", context=None, *, request_id=None):
            decision = with_relay_proofs(
                RoutingDecision(
                    model=ids[0],
                    provider="omniroute",
                    tier=2,
                    reason="test",
                    alternatives=list(ids[1:3]),
                    safety_flags=["subscription_fallback_policy"],
                    candidate_states=[
                        {
                            "model_id": model,
                            "route_id": model,
                            "admitted": True,
                            "capacity_order": 0,
                        }
                        for model in ids[:3]
                    ],
                ),
                context,
            )
            return replace(
                decision,
                alternatives=list(ids[1:]),
                candidate_states=[
                    {"model_id": model, "route_id": model, "admitted": True, "capacity_order": tier}
                    for model, tier in zip(ids, (0, 0, 0, 1, 2), strict=True)
                ],
            )

    transport = ContextLengthTransport([(503, '{"error":"unavailable"}')] * 3)
    events = []
    original = api._finish_evidence

    def capture(evidence, outcome, key):
        events.append(outcome.to_dict())
        return original(evidence, outcome, key)

    monkeypatch.setenv("VERDICT_HOME", str(tmp_path))
    monkeypatch.setenv("LLMGATE_ALLOW_ANONYMOUS", "true")
    monkeypatch.setenv("LLMGATE_LOG_PATH", "")
    monkeypatch.setattr(api, "_finish_evidence", capture)
    monkeypatch.setattr(api, "_build_intelligence", lambda: Intelligence())
    monkeypatch.setattr(
        api, "_build_proxy", lambda: UpstreamProxy("http://upstream.test/v1", transport=transport)
    )
    with TestClient(api.app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "fix code"}]},
            headers={"idempotency-key": "free-budget"},
        )
    assert response.status_code == 503
    assert [row["body"]["model"] for row in transport.requests] == list(ids[:3])
    terminal = events[-1]["details"]
    assert terminal["termination_reason"] == "budget_exhausted"
    assert [row["model"] for row in terminal["attempted_routes"]] == list(ids[:3])
    assert not any(row.get("details", {}).get("fallback") for row in events)


def test_permitted_metered_sorts_after_capable_free_and_subscription(tmp_path, monkeypatch):
    """Order a mixed-tier ladder free, then subscription, then metered last."""
    from datetime import timedelta

    from verdict.orchestration.contracts import CapacityClass, EligibilityStage
    from verdict.orchestration.health_cache import ProbeResult

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / "verdict-home"))

    free_id, sub_id, paid_id = "freeco/model", "subco/model", "paidco/model"
    rows = [
        _ladder_row(free_id, pricing={"input": 1}, tier=2),
        _ladder_row(sub_id, pricing={"input": 1}, tier=2),
        _ladder_row(paid_id, pricing={"input": 1}, tier=2),
    ]
    conns = [
        _conn("freeco", auth="apikey", free=True),
        _conn("subco", auth="oauth"),
        _conn("paidco", auth="apikey"),
    ]
    cache = HealthCache(tmp_path / "health.json")
    for route in (free_id, sub_id, paid_id):
        cache.record(
            route,
            ProbeResult(
                category="ok", chat_ok=True, tool_ok=True, agentic_ok=True, probe_class="agentic"
            ),
            NOW,
        )
    cache.save()
    ladder = EligibilityLadder(
        rows,
        conns,
        lambda _: HealthResult(True, "ok"),
        tmp_path / "state.json",
        health_cache=cache,
        metadata=None,
    )
    requirements = TaskRequirements(max_capability_tier=3)

    verdicts = {v.route_id: v for v in ladder.evaluate(requirements, now=NOW)}
    ranked = sorted((v for v in verdicts.values() if v.rank is not None), key=lambda v: v.rank)
    assert [v.route_id for v in ranked] == [free_id, sub_id, paid_id]
    assert [v.capacity_class for v in ranked] == [
        CapacityClass.FREE,
        CapacityClass.SUBSCRIPTION,
        CapacityClass.METERED,
    ]
    assert [v.rank_components["capacity_order"] for v in ranked] == [0, 1, 2]

    # A payment-required negative still excludes the metered route outright.
    # The failure is recorded one second later so it wins the cache's
    # strictly-newer checked_at merge over the earlier healthy probe.
    later = NOW + timedelta(seconds=1)
    cache.record(
        paid_id,
        ProbeResult(category="payment_required", chat_ok=False, tool_ok=False, http_status=402),
        later,
    )
    cache.save()
    excluded = {v.route_id: v for v in ladder.evaluate(requirements, now=later)}
    paid = excluded[paid_id]
    assert paid.rank is None
    assert paid.failed_stage is EligibilityStage.HEALTHY
    assert paid.reason == "payment_required"


def test_terminal_reasons_free_supply_unknown_and_budget_exhausted_preserve_names_and_distinction(
    monkeypatch, tmp_path
):
    """Prove free_supply_unknown and budget_exhausted keep current names and remain distinct."""
    from fastapi.testclient import TestClient

    from tests.relay_proof_helpers import with_relay_proofs
    from tests.test_api_relay_context_length import ContextLengthTransport
    from tests.test_proxy import FixedIntelligence
    from verdict import api
    from verdict.models import RoutingDecision
    from verdict.orchestration.eligibility import selection_accounting
    from verdict.proxy import UpstreamProxy

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path))
    monkeypatch.setenv("TMPDIR", "/tmp")
    monkeypatch.setenv("LLMGATE_ALLOW_ANONYMOUS", "true")
    monkeypatch.setenv("LLMGATE_LOG_PATH", "")

    # 1. Assert free_supply_unknown terminal reason appears under its current name
    # when an unverified free supply fails upstream without escalation.
    free_unknown_ids = ("free-unknown-a", "sub-a")

    class FreeUnknownIntelligence(FixedIntelligence):
        async def route(self, task, criticality="medium", context=None, *, request_id=None):
            decision = with_relay_proofs(
                RoutingDecision(
                    model=free_unknown_ids[0],
                    provider="omniroute",
                    tier=2,
                    reason="test",
                    alternatives=[free_unknown_ids[1]],
                    safety_flags=["subscription_fallback_policy", "free_supply_unknown"],
                    candidate_states=[
                        {
                            "model_id": free_unknown_ids[0],
                            "route_id": free_unknown_ids[0],
                            "admitted": True,
                            "capacity_order": 0,
                        }
                    ],
                ),
                context,
            )
            return replace(
                decision,
                alternatives=[free_unknown_ids[1]],
                candidate_states=[
                    {"model_id": model, "route_id": model, "admitted": True, "capacity_order": tier}
                    for model, tier in zip(free_unknown_ids, (0, 1), strict=True)
                ],
            )

    transport_free_unknown = ContextLengthTransport([(503, '{"error":"service_unavailable"}')])
    events_free_unknown = []
    original_finish = api._finish_evidence

    def capture_free_unknown(evidence, outcome, key):
        events_free_unknown.append(outcome.to_dict())
        return original_finish(evidence, outcome, key)

    monkeypatch.setattr(api, "_finish_evidence", capture_free_unknown)
    monkeypatch.setattr(api, "_build_intelligence", lambda: FreeUnknownIntelligence())
    monkeypatch.setattr(
        api,
        "_build_proxy",
        lambda: UpstreamProxy("http://upstream.test/v1", transport=transport_free_unknown),
    )

    with TestClient(api.app) as client:
        res = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "fix code"}]},
            headers={"idempotency-key": "regression-free-supply-unknown"},
        )
    assert res.status_code == 503
    assert len(transport_free_unknown.requests) == 1
    free_unknown_terminal = events_free_unknown[-1]["details"]
    free_reason = free_unknown_terminal["termination_reason"]
    assert free_reason == "free_supply_unknown"

    # 2. Assert budget_exhausted terminal reason appears under its current name
    # when all three free attempts fail.
    budget_ids = ("free-b1", "free-b2", "free-b3", "sub-b1")

    class BudgetExhaustedIntelligence(FixedIntelligence):
        async def route(self, task, criticality="medium", context=None, *, request_id=None):
            decision = with_relay_proofs(
                RoutingDecision(
                    model=budget_ids[0],
                    provider="omniroute",
                    tier=2,
                    reason="test",
                    alternatives=list(budget_ids[1:3]),
                    safety_flags=["subscription_fallback_policy"],
                    candidate_states=[
                        {
                            "model_id": model,
                            "route_id": model,
                            "admitted": True,
                            "capacity_order": 0,
                        }
                        for model in budget_ids[:3]
                    ],
                ),
                context,
            )
            return replace(
                decision,
                alternatives=list(budget_ids[1:]),
                candidate_states=[
                    {"model_id": model, "route_id": model, "admitted": True, "capacity_order": tier}
                    for model, tier in zip(budget_ids, (0, 0, 0, 1), strict=True)
                ],
            )

    transport_budget = ContextLengthTransport([(503, '{"error":"service_unavailable"}')] * 3)
    events_budget = []

    def capture_budget(evidence, outcome, key):
        events_budget.append(outcome.to_dict())
        return original_finish(evidence, outcome, key)

    monkeypatch.setattr(api, "_finish_evidence", capture_budget)
    monkeypatch.setattr(api, "_build_intelligence", lambda: BudgetExhaustedIntelligence())
    monkeypatch.setattr(
        api,
        "_build_proxy",
        lambda: UpstreamProxy("http://upstream.test/v1", transport=transport_budget),
    )

    with TestClient(api.app) as client:
        res = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "fix code"}]},
            headers={"idempotency-key": "regression-budget-exhausted"},
        )
    assert res.status_code == 503
    assert len(transport_budget.requests) == 3
    budget_terminal = events_budget[-1]["details"]
    budget_reason = budget_terminal["termination_reason"]
    assert budget_reason == "budget_exhausted"

    # 3. Both reasons are distinct and not merged into the selection_accounting classes.
    assert free_reason != budget_reason

    accounting_classes = {
        "eligible_free_attempted",
        "known_unavailable",
        "unqualified_supply",
        "eligible_unattempted_authority_expired",
    }
    accounting_reasons_fields = {"known_unavailable_reasons", "unqualified_reasons"}
    expected_accounting_keys = accounting_classes | accounting_reasons_fields

    for terminal_details, reason_name in (
        (free_unknown_terminal, "free_supply_unknown"),
        (budget_terminal, "budget_exhausted"),
    ):
        accounting = terminal_details["selection_accounting"]
        # The reason is recorded as termination_reason, not as a selection_accounting class key
        assert terminal_details["termination_reason"] == reason_name
        assert reason_name not in accounting
        assert set(accounting.keys()) == expected_accounting_keys
        # Neither reason replaces or is merged into category lists as class labels
        for cls_name in accounting_classes:
            assert reason_name not in accounting[cls_name]
            assert cls_name != reason_name

    # Check direct selection_accounting projection behavior:
    # authority_expired=False vs True maps to eligible_unattempted_authority_expired,
    # never introducing budget_exhausted or free_supply_unknown as class names.
    sample_candidates = [
        {"route_id": "free/a", "capacity_order": 0, "admitted": True},
        {"route_id": "sub/a", "capacity_order": 1, "admitted": True},
    ]
    acct_no_expire = selection_accounting(
        sample_candidates, attempted=["free/a"], authority_expired=False
    )
    acct_expired = selection_accounting(
        sample_candidates, attempted=["free/a"], authority_expired=True
    )
    assert "budget_exhausted" not in acct_no_expire
    assert "free_supply_unknown" not in acct_no_expire
    assert "budget_exhausted" not in acct_expired
    assert "free_supply_unknown" not in acct_expired
    assert acct_no_expire["eligible_unattempted_authority_expired"] == []
    assert acct_expired["eligible_unattempted_authority_expired"] == ["sub/a"]
