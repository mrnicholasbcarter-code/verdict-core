"""Defects that kept live acceptance red (OpenSpec change ``acceptance-red-fixes``).

Every test uses stub transports and temporary state; nothing calls a provider.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

import verdict.api as api
from tests import test_serve_failure_feedback as feedback
from tests.test_inband_credit_refusal import _Response, answer_body, refusal_body
from verdict import acceptance, acceptance_toolcheck
from verdict.orchestration import selection
from verdict.orchestration.contracts import EligibilityStage, RouteVerdict, TaskRequirements
from verdict.orchestration.eligibility import EligibilityLadder
from verdict.orchestration.health_cache import (
    CATEGORY_OK,
    CATEGORY_PAYMENT,
    CATEGORY_TIMEOUT,
    HealthCache,
    ProbeResult,
)
from verdict.prove_at_rest import AdmittedRoute, CycleStats, Prober, live_agentic_transport
from verdict.relay import relay_failure_feedback
from verdict.subagent_selection import HealthResult, LaunchCandidate, openai_health_probe

NOW = datetime(2026, 11, 18, 17, 0, tzinfo=timezone.utc)
REQ = TaskRequirements(required_capabilities=frozenset({"tools"}), coding=True)
REFUSER = "freeprov/paid-model"
SIBLING = "freeprov/sibling-model"


# -- D1: the ladder's health probe ---------------------------------------------


def _probe_result(body: dict[str, Any]) -> HealthResult:
    response = _Response(body)
    response.headers = Message()
    probe = openai_health_probe("http://gw/v1", opener=lambda request, *, timeout: response)
    candidate = LaunchCandidate(
        route_id=REFUSER,
        selector=f"omniroute/{REFUSER}",
        capabilities=frozenset({"tools"}),
        context_tokens=128_000,
        input_cost=0.0,
        output_cost=0.0,
        is_free=True,
        is_frontier=False,
        coding_score=1,
        reasoning_score=1,
    )
    return probe(candidate)


def test_health_probe_reports_the_captured_refusal_as_payment_required() -> None:
    """Catches: any non-empty text counted as healthy, so a refusing route was selected."""
    result = _probe_result(refusal_body(REFUSER))
    assert not result.healthy
    assert result.category == "payment_required"
    assert result.status_code == 200  # the 200 is what marks it route-scoped


def test_health_probe_still_passes_a_real_answer() -> None:
    assert _probe_result(answer_body(REFUSER)).healthy


def _ladder_rows() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = [
        {
            "id": route,
            "owned_by": "freeprov",
            "capabilities": {"tool_calling": True, "chat": True},
            "context_length": 128000,
        }
        for route in (REFUSER, SIBLING)
    ]
    conn = {
        "provider": "freeprov",
        "authType": "apikey",
        "isActive": True,
        "testStatus": "ok",
        "backoffLevel": 0,
        "import_free_only": True,
    }
    return rows, [conn]


def test_ladder_does_not_select_a_refusing_route_and_keeps_its_sibling(tmp_path: Path) -> None:
    """Catches: payment_required from one route cooling the whole pool and provider."""
    cache = HealthCache(tmp_path / "health-cache.json")
    for route in (REFUSER, SIBLING):
        cache.record(
            route,
            ProbeResult(
                category=CATEGORY_OK,
                chat_ok=True,
                tool_ok=True,
                probe_class="agentic",
                agentic_ok=True,
            ),
            NOW,
        )
    results = {
        REFUSER: HealthResult(False, "payment_required", 200, error_message="inband_refusal"),
        SIBLING: HealthResult(True, "healthy", 200),
    }
    calls: list[str] = []

    def probe(route_id: str) -> HealthResult:
        calls.append(route_id)
        return results[route_id]

    rows, connections = _ladder_rows()
    ladder = EligibilityLadder(
        rows, connections, probe, tmp_path / "state.json", health_cache=cache
    )
    chosen, verdicts = ladder.select(REQ, now=NOW)

    assert REFUSER in calls, "the test must exercise the refusing route"
    assert chosen is not None and chosen.route_id == SIBLING
    refused = next(v for v in verdicts if v.route_id == REFUSER)
    assert refused.reason == "payment_required"
    state = json.loads((tmp_path / "state.json").read_text())
    assert f"route:{REFUSER}" in state["cooldowns"]
    assert not [key for key in state["cooldowns"] if key.startswith(("pool:", "provider:"))]
    assert not [key for key in state.get("not_free_overrides", {}) if key.startswith("pool:")]


# -- D2: forced reprobe and proof invalidation ----------------------------------


def _fresh_proof_cache(tmp_path: Path, *routes: str) -> HealthCache:
    cache = HealthCache(tmp_path / "health.json")
    for route in routes:
        cache.record(
            route,
            ProbeResult(
                category=CATEGORY_OK,
                chat_ok=True,
                tool_ok=True,
                probe_class="agentic",
                agentic_ok=True,
            ),
            NOW - timedelta(minutes=5),
        )
    cache.save()
    return cache


def _reprobe_prober(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    routes: list[AdmittedRoute],
    *,
    unproven: frozenset[str] = frozenset(),
    **kwargs: Any,
) -> tuple[Prober, list[str]]:
    sent: list[str] = []

    def fake_open(request: Any, *, timeout: float) -> _Response:
        sent.append(json.loads(request.data)["model"])
        return _Response(refusal_body())

    monkeypatch.setattr("verdict.prove_at_rest.open_no_redirect", fake_open)

    def no_chat(route_id: str, phase: str, timeout: float) -> Any:
        raise AssertionError("the agentic phase must not use the chat transport")

    proven = [r.route_id for r in routes if r.route_id not in unproven]
    cache = _fresh_proof_cache(tmp_path, *proven)
    prober = Prober(
        cache,
        lambda: routes,
        no_chat,
        agentic_transport=live_agentic_transport("http://gw", api_key="k"),
        clock=lambda: NOW,
        **kwargs,
    )
    return prober, sent


def test_fresh_proof_is_not_reprobed_without_the_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prober, sent = _reprobe_prober(tmp_path, monkeypatch, [AdmittedRoute("pa/m", "pa", "free")])
    stats = CycleStats()
    prober.run_agentic_probes(stats)
    assert sent == [] and stats.reprobe is None


def test_reprobe_forces_a_route_with_fresh_proof_into_the_agentic_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches: no command could re-test a route whose 24 h proof was still fresh."""
    prober, sent = _reprobe_prober(
        tmp_path,
        monkeypatch,
        [AdmittedRoute("pa/m", "pa", "free")],
        reprobe_routes=frozenset({"pa/m"}),
    )
    stats = CycleStats()
    prober.run_agentic_probes(stats)
    assert sent == ["pa/m"]
    assert stats.to_dict()["reprobe"] == {"queued": ["pa/m"], "skipped": {}}
    entry = HealthCache(prober.cache.path).entry("pa/m")
    assert entry is not None and not entry.agentic_ok and entry.category == CATEGORY_PAYMENT


def test_named_reprobe_routes_do_not_count_against_agentic_max_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches: an explicit reprobe list cut to the default route limit (seen live: CAPPED)."""
    routes = [AdmittedRoute(f"p{i}/m", f"p{i}", "free") for i in range(5)]
    named = frozenset({"p0/m", "p1/m", "p2/m"})
    prober, sent = _reprobe_prober(
        tmp_path,
        monkeypatch,
        routes,
        unproven=frozenset({"p3/m", "p4/m"}),  # the cycle's own candidates
        agentic_max_routes=1,
        reprobe_routes=named,
    )
    stats = CycleStats()
    prober.run_agentic_probes(stats)
    assert named <= set(sent), "every named route is probed past agentic_max_routes"
    # The limit still holds for the routes the cycle picked itself: one of two.
    assert len(set(sent) - named) == 1 and len(sent) == 4
    assert stats.reprobe == {"queued": sorted(named), "skipped": {}}


def test_reprobe_only_forces_admitted_free_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches: a forced reprobe probing a paid or non-admitted route."""
    routes = [AdmittedRoute("pa/m", "pa", "free"), AdmittedRoute("pc/paid", "pc", "metered")]
    prober, sent = _reprobe_prober(
        tmp_path, monkeypatch, routes, reprobe_routes=frozenset({"pa/m", "pc/paid", "zz/unknown"})
    )
    stats = CycleStats()
    prober.run_agentic_probes(stats)
    assert sent == ["pa/m"]
    assert stats.reprobe == {
        "queued": ["pa/m"],
        "skipped": {"pc/paid": "capacity:metered", "zz/unknown": "not_admitted"},
    }


def test_reprobe_respects_the_request_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Catches: named routes running past --max-requests (three requests per route)."""
    routes = [AdmittedRoute("pa/m", "pa", "free"), AdmittedRoute("pb/m", "pb", "free")]
    prober, sent = _reprobe_prober(
        tmp_path,
        monkeypatch,
        routes,
        max_requests=3,  # room for exactly one three-turn conversation
        reprobe_routes=frozenset({"pa/m", "pb/m"}),
    )
    stats = CycleStats()
    prober.run_agentic_probes(stats)
    assert sent == ["pa/m"]
    assert stats.reprobe == {"queued": ["pa/m"], "skipped": {"pb/m": "request_cap"}}


def test_prove_at_rest_once_passes_repeatable_reprobe_to_the_prober(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Catches: the flag parsed but never reaching the prober."""
    import argparse

    from verdict.commands import dispatch as dispatch_module
    from verdict.commands.parsers_runtime import register

    parser = argparse.ArgumentParser()
    register(parser.add_subparsers(dest="command"))
    argv = ["prove-at-rest", "once", "--allow-live-probe", "--json"]
    args = parser.parse_args([*argv, "--reprobe", "pa/m", "--reprobe", "pb/m"])
    assert args.reprobe == ["pa/m", "pb/m"]
    seen: dict[str, Any] = {}

    class Daemon:
        def run_once(self) -> CycleStats:
            return CycleStats(reprobe={"queued": ["pa/m", "pb/m"], "skipped": {}})

    def build(**kwargs: Any) -> Daemon:
        seen.update(kwargs)
        return Daemon()

    monkeypatch.setattr("verdict.prove_at_rest.build_live_daemon", build)
    dispatch_module.dispatch(parser, args)
    assert seen["reprobe_routes"] == ("pa/m", "pb/m")
    assert json.loads(capsys.readouterr().out)["reprobe"]["queued"] == ["pa/m", "pb/m"]


def test_relay_billing_negative_invalidates_the_agentic_proof(tmp_path: Path) -> None:
    """Catches: a refused route kept its fresh proof, so the ladder and identify skipped it."""
    cache = _fresh_proof_cache(tmp_path, "pa/m")
    feedback_result = relay_failure_feedback(cache, route_id="pa/m", status_code=402, now=NOW)
    assert feedback_result.recorded
    entry = HealthCache(cache.path).entry("pa/m")
    assert entry is not None and not entry.healthy
    assert not entry.agentic_ok and entry.agentic_checked_at is None
    assert entry.capacity_evidence == "billing_402_metered"


def test_a_timeout_still_keeps_the_agentic_proof(tmp_path: Path) -> None:
    """No answer is not a refusal: the invalidation must not widen to transient failures."""
    cache = _fresh_proof_cache(tmp_path, "pa/m")
    cache.record(
        "pa/m",
        ProbeResult(category=CATEGORY_TIMEOUT, chat_ok=False, tool_ok=False, failure_scope="route"),
        NOW,
    )
    entry = cache.entry("pa/m")
    assert entry is not None and entry.agentic_ok and entry.agentic_checked_at is not None


# -- D3: lease_held is its own reason -------------------------------------------


def _verdict(route_id: str, reason: str) -> RouteVerdict:
    return RouteVerdict(
        route_id=route_id,
        provider=route_id.split("/", 1)[0],
        reached=EligibilityStage.AVAILABLE,
        failed_stage=EligibilityStage.AVAILABLE,
        reason=reason,
    )


class _Selector:
    def __init__(self, verdicts: tuple[RouteVerdict, ...]) -> None:
        self._verdicts = verdicts

    def select(self, requirements: Any, *, now: datetime) -> tuple[None, tuple[RouteVerdict, ...]]:
        return None, self._verdicts


@pytest.mark.parametrize(
    ("reasons", "expected"),
    [
        (("lease_held", "admission:payment_required"), ("lease_held", selection.LEASE_HELD_EXIT)),
        (("no_agentic_probe", "admission:payment_required"), ("no_eligible_route", 1)),
    ],
)
def test_selection_names_lease_held_apart_from_no_eligible_route(
    monkeypatch: pytest.MonkeyPatch, reasons: tuple[str, ...], expected: tuple[str, int]
) -> None:
    """Catches: a held refresh lease reported as no_eligible_route (exit 1)."""
    verdicts = tuple(_verdict(f"p{i}/m", reason) for i, reason in enumerate(reasons))
    monkeypatch.setattr(selection, "build_selector", lambda *a, **k: _Selector(verdicts))
    result = selection.select_route_for_task(REQ, gateway="http://gw", now=NOW)
    assert result.route is None
    assert selection.no_route_outcome(result, explicit_model=False) == expected
    assert selection.LEASE_HELD_EXIT not in {0, 1}


def test_acceptance_and_selection_agree_on_the_lease_contract() -> None:
    assert acceptance.LEASE_HELD_EXIT == selection.LEASE_HELD_EXIT
    assert acceptance.LEASE_HELD_REASON == selection.LEASE_HELD_REASON
    assert acceptance.LEASE_WAIT_SECONDS <= 600


LEASE_OUT = json.dumps({"model": None, "reason": "lease_held", "reasons": ["lease_held"]})
NO_ROUTE_OUT = json.dumps({"model": None, "reason": "no_eligible_route", "reasons": ["lease_held"]})


def _scripted(*replies: tuple[int, str, str]) -> tuple[Any, list[int]]:
    calls: list[int] = []

    def run(args: list[str], timeout: float, env: Any) -> tuple[int, str, str]:
        calls.append(len(calls))
        return replies[len(calls) - 1]

    return run, calls


def test_acceptance_waits_for_the_lease_and_retries_once() -> None:
    """Catches: an hourly run going red because a screen held the lease at that minute."""
    run, calls = _scripted((75, LEASE_OUT, ""), (0, "kr/model-a\n", ""))
    waits: list[float] = []
    check = acceptance._cli_check(
        "route",
        ["route", "--terse", "x"],
        5,
        acceptance.judge_route,
        "http://gw",
        _run_cli=run,
        _wait_for_lease=lambda: waits.append(1) or 12.5,
    )
    assert check.passed and len(calls) == 2 and len(waits) == 1
    first, second = check.detail["attempts"]
    assert first["exit_code"] == 75 and first["lease_wait_seconds"] == 12.5
    assert "lease_held" in first["reason"]
    assert second["attempt"] == 2 and second["exit_code"] == 0
    assert "refresh lease was held" in check.reason


def test_acceptance_fails_when_the_lease_is_still_held_after_one_retry() -> None:
    run, calls = _scripted((75, LEASE_OUT, ""), (75, LEASE_OUT, ""))
    check = acceptance._cli_check(
        "choose",
        ["choose"],
        5,
        acceptance.judge_choose,
        "http://gw",
        _run_cli=run,
        _wait_for_lease=lambda: 600.0,
    )
    assert not check.passed and len(calls) == 2  # bounded: exactly one retry
    assert len(check.detail["attempts"]) == 2


def test_acceptance_does_not_retry_plain_no_eligible_route() -> None:
    """Catches: the lease retry hiding a real no-route failure."""
    run, calls = _scripted((1, NO_ROUTE_OUT, ""))

    def never() -> float:
        raise AssertionError("no_eligible_route must not wait for the lease")

    check = acceptance._cli_check(
        "route",
        ["route"],
        5,
        acceptance.judge_route,
        "http://gw",
        _run_cli=run,
        _wait_for_lease=never,
    )
    assert not check.passed and len(calls) == 1
    assert "no_eligible_route" in check.reason and "attempts" not in check.detail


def test_lease_wait_is_bounded_when_the_lease_never_frees() -> None:
    clock = [0.0]

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    waited = acceptance.wait_for_refresh_lease(
        live=lambda: True, sleep=sleep, monotonic=lambda: clock[0]
    )
    assert waited == acceptance.LEASE_WAIT_SECONDS
    assert acceptance.wait_for_refresh_lease(live=lambda: False, sleep=sleep) < 1


# -- D5: relay failover after an in-band refusal ---------------------------------


def test_relay_fails_over_after_a_refusal_without_an_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches: one upstream call, then 402, although a second admitted free route answers.

    The acceptance serve check sends no idempotency key. A refusal produced no
    output, so the second POST cannot repeat an effect.
    """
    sent: list[str] = []

    async def transport(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        sent.append(model)
        if len(sent) == 1:
            return httpx.Response(200, json=refusal_body(model))
        return httpx.Response(200, json=answer_body(model))

    feedback._prepare(monkeypatch, transport)
    with TestClient(api.app) as client:
        response = client.post("/v1/chat/completions", json=feedback._request_body())
    assert len(sent) == 2 and sent[0] != sent[1]
    assert response.status_code == 200
    assert response.headers["x-verdict-model"] == sent[1]


def test_relay_does_not_retry_a_real_5xx_without_an_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal exception must not relax the idempotency rule for other failures."""
    sent: list[str] = []

    async def transport(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["model"])
        return httpx.Response(503, json={"error": {"message": "server lost reply"}})

    feedback._prepare(monkeypatch, transport)
    with TestClient(api.app) as client:
        response = client.post("/v1/chat/completions", json=feedback._request_body())
    assert len(sent) == 1 and response.status_code == 503


# -- D6: acceptance rescreen hints ------------------------------------------------


def _acceptance_verdict(route: str, eligible: bool = True) -> dict[str, Any]:
    return {
        "route_id": route,
        "provider": route.split("/")[0],
        "capacity_class": "free",
        "reached": "TASK_ELIGIBLE" if eligible else "ENTITLED",
        "reason": "ranked" if eligible else "no_agentic_probe",
        "supports_tools": True,
        "agentic_capability": {"qualified": True},
    }


def test_acceptance_failing_all_listed_produces_rescreen_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug 1a: a listed route failing both tool-check attempts gets a rescreen hint.

    The HealthCache entry must NOT be written (acceptance is a measurement only).
    """
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path))

    route = "fakeprov/failing-route"

    def run_cli(args: list[str], timeout: float, env: Any) -> tuple[int, str, str]:
        return 0, json.dumps({"verdicts": [_acceptance_verdict(route)]}), ""

    def gateway_get(base: str, key: Any, path: str, timeout: float = 60) -> Any:
        if "synced" in path:
            return {"fakeprov": [{"id": "failing-route"}]}
        if path == "/v1/models":
            return {"data": [{"id": route}]}
        return {"connections": []}

    def fail_always(path: str, body: dict[str, Any]) -> tuple[int, Any]:
        return 502, None

    monkeypatch.setattr(acceptance, "gateway_get", gateway_get)
    monkeypatch.setattr(acceptance_toolcheck, "gateway_post", lambda *a, **k: fail_always)

    check = acceptance.eligibility_check("http://gw", None, seed="s", _run_cli=run_cli)

    quality = check.detail["independent_check"]
    assert quality.get("rescreen_requested") == [route], (
        f"route failing both attempts must produce a rescreen hint; got {quality!r}"
    )

    from verdict.orchestration.health_cache import HealthCache, default_cache_path

    cache = HealthCache(default_cache_path())
    assert cache.entry(route) is None, "acceptance must NOT write a health entry"
    assert route in cache.rescreen_requests()


def test_acceptance_failing_one_attempt_then_passing_does_not_produce_rescreen_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug 1a negative: a route that passes on retry must not produce a rescreen hint."""
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path))

    route = "fakeprov/flaky-route"

    def run_cli(args: list[str], timeout: float, env: Any) -> tuple[int, str, str]:
        return 0, json.dumps({"verdicts": [_acceptance_verdict(route)]}), ""

    def gateway_get(base: str, key: Any, path: str, timeout: float = 60) -> Any:
        if "synced" in path:
            return {"fakeprov": [{"id": "flaky-route"}]}
        if path == "/v1/models":
            return {"data": [{"id": route}]}
        return {"connections": []}

    _calls: dict[str, int] = {}

    def fail_once(path: str, body: dict[str, Any]) -> tuple[int, Any]:
        route = body["model"]
        _calls[route] = _calls.get(route, 0) + 1
        if _calls[route] == 1:
            return 502, None
        # second attempt: answer with a passing tool call
        turn2 = any(m.get("role") == "tool" for m in body.get("messages", []))
        name = "write_file" if turn2 else "read_file"
        call_args = (
            {"path": "out.txt", "content": "VERDICT ACCEPTANCE PROBE 7"}
            if turn2
            else {"path": "notes.txt"}
        )
        tool_call = {"id": "c1", "function": {"name": name, "arguments": json.dumps(call_args)}}
        return 200, {"choices": [{"message": {"content": "", "tool_calls": [tool_call]}}]}

    monkeypatch.setattr(acceptance, "gateway_get", gateway_get)
    monkeypatch.setattr(acceptance_toolcheck, "gateway_post", lambda *a, **k: fail_once)

    check = acceptance.eligibility_check("http://gw", None, seed="s", _run_cli=run_cli)

    quality = check.detail["independent_check"]
    assert quality.get("rescreen_requested", []) == [], (
        "a route that passes on retry must not produce a rescreen hint"
    )

    from verdict.orchestration.health_cache import HealthCache, default_cache_path

    assert route not in HealthCache(default_cache_path()).rescreen_requests()
