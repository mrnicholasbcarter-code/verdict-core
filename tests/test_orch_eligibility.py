"""Tests for verdict.orchestration.eligibility.EligibilityLadder."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from verdict.orchestration.contracts import (
    CapacityClass,
    EligibilityStage,
    FailureClassification,
    NodeKind,
    TaskRequirements,
    WorkNode,
)
from verdict.orchestration.eligibility import EligibilityLadder
from verdict.subagent_selection import HealthResult

NOW = datetime(2026, 2, 1, 12, 0, 0, tzinfo=timezone.utc)


def row(
    route_id: str,
    owned_by: str | None = None,
    *,
    context: int = 200_000,
    tools: bool = True,
    reasoning: bool = True,
    pricing: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    return {
        "id": route_id,
        "owned_by": owned_by or route_id.split("/", 1)[0],
        "context_length": context,
        "max_input_tokens": context,
        "max_output_tokens": 32_000,
        "capabilities": {"tool_calling": tools, "reasoning": reasoning},
        "pricing": dict(pricing) if pricing is not None else {"input": 1.0, "output": 2.0},
    }


def conn(
    provider: str,
    *,
    auth: str = "oauth",
    active: bool = True,
    plan: str = "max",
    free_only: bool = False,
    rate_limited_until: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "provider": provider,
        "authType": auth,
        "isActive": active,
        "testStatus": "ok",
        "backoffLevel": 0,
        "plan_label": plan,
        "rate_limited_until": rate_limited_until,
        "import_free_only": free_only,
    }


class FakeProbe:
    def __init__(self, results: Mapping[str, HealthResult] | None = None) -> None:
        self.results = dict(results or {})
        self.calls: list[str] = []

    def __call__(self, route_id: str) -> HealthResult:
        self.calls.append(route_id)
        return self.results.get(route_id, HealthResult(healthy=True, category=""))


def make_ladder(
    tmp_path: Path,
    rows: list[dict[str, Any]],
    connections: list[dict[str, Any]],
    probe: FakeProbe | None = None,
    **kwargs: Any,
) -> tuple[EligibilityLadder, FakeProbe]:
    probe = probe or FakeProbe()
    ladder = EligibilityLadder(rows, connections, probe, tmp_path / "state.json", **kwargs)
    return ladder, probe


def by_route(verdicts: tuple[Any, ...]) -> dict[str, Any]:
    return {v.route_id: v for v in verdicts}


REQ = TaskRequirements()


class TestDiscovered:
    def test_opaque_routers_skipped(self, tmp_path: Path) -> None:
        rows = [
            row("auto/best-coding"),
            row("combo/mix"),
            row("router/x"),
            row("virtual/y"),
            row("magic/blend", owned_by="combo"),
            row("cc/claude-sonnet-5", owned_by="claude"),
        ]
        ladder, _ = make_ladder(tmp_path, rows, [conn("claude")])
        verdicts = ladder.evaluate(REQ, now=NOW)
        assert [v.route_id for v in verdicts] == ["cc/claude-sonnet-5"]


class TestEntitled:
    def test_missing_connection_fails(self, tmp_path: Path) -> None:
        ladder, _ = make_ladder(tmp_path, [row("cc/claude-sonnet-5", owned_by="claude")], [])
        v = ladder.evaluate(REQ, now=NOW)[0]
        assert v.failed_stage is EligibilityStage.ENTITLED
        assert v.reason == "no_active_account"
        assert v.reached is EligibilityStage.DISCOVERED

    def test_inactive_connection_fails(self, tmp_path: Path) -> None:
        ladder, _ = make_ladder(
            tmp_path, [row("cc/claude-sonnet-5", owned_by="claude")], [conn("claude", active=False)]
        )
        v = ladder.evaluate(REQ, now=NOW)[0]
        assert v.failed_stage is EligibilityStage.ENTITLED
        assert v.reason == "no_active_account"

    def test_free_suffix_without_connection_stays_unknown(self, tmp_path: Path) -> None:
        ladder, _ = make_ladder(tmp_path, [row("openrouter/model:free")], [])
        v = ladder.evaluate(REQ, now=NOW)[0]
        assert v.failed_stage is EligibilityStage.ENTITLED
        assert v.reason == "no_active_account"
        assert v.capacity_class is CapacityClass.UNKNOWN
        assert v.capacity_evidence == "no_connection"

    def test_harness_visibility_gate(self, tmp_path: Path) -> None:
        visible: Callable[[str], bool] = lambda r: r != "cc/claude-sonnet-5"  # noqa: E731
        ladder, _ = make_ladder(
            tmp_path,
            [row("cc/claude-sonnet-5", owned_by="claude")],
            [conn("claude")],
            harness_visible=visible,
        )
        v = ladder.evaluate(REQ, now=NOW)[0]
        assert v.failed_stage is EligibilityStage.ENTITLED
        assert v.reason == "not_harness_visible"


class TestHealthy:
    def test_evaluate_never_probes(self, tmp_path: Path) -> None:
        ladder, probe = make_ladder(
            tmp_path, [row("cc/claude-sonnet-5", owned_by="claude")], [conn("claude")]
        )
        v = ladder.evaluate(REQ, now=NOW)[0]
        assert probe.calls == []
        assert v.failed_stage is None
        assert v.reason == "unprobed"
        assert v.reached is EligibilityStage.ENTITLED  # not yet HEALTHY, not failed

    def test_cached_unhealthy_fails_with_category(self, tmp_path: Path) -> None:
        probe = FakeProbe(
            {"cc/claude-sonnet-5": HealthResult(healthy=False, category="authentication")}
        )
        ladder, _ = make_ladder(
            tmp_path, [row("cc/claude-sonnet-5", owned_by="claude")], [conn("claude")], probe
        )
        selected, _ = ladder.select(REQ, now=NOW)
        assert selected is None
        # a later evaluate() must see the cached probe failure, without probing
        v = ladder.evaluate(REQ, now=NOW)[0]
        assert v.failed_stage is EligibilityStage.HEALTHY
        assert v.reason == "authentication"
        assert probe.calls == ["cc/claude-sonnet-5"]

    def test_stale_health_is_reprobed_on_select(self, tmp_path: Path) -> None:
        ladder, probe = make_ladder(
            tmp_path,
            [row("cc/claude-sonnet-5", owned_by="claude")],
            [conn("claude")],
            healthy_ttl_seconds=300,
        )
        selected, _ = ladder.select(REQ, now=NOW)
        assert selected is not None and probe.calls == ["cc/claude-sonnet-5"]
        later = NOW + timedelta(seconds=301)
        selected, _ = ladder.select(REQ, now=later)
        assert selected is not None
        assert probe.calls == ["cc/claude-sonnet-5", "cc/claude-sonnet-5"]


class TestAvailable:
    def test_cooldown_from_retry_after(self, tmp_path: Path) -> None:
        ladder, _ = make_ladder(
            tmp_path, [row("cc/claude-sonnet-5", owned_by="claude")], [conn("claude")]
        )
        failure = FailureClassification(
            category="rate_limited", action="REROUTE", cooldown_seconds=90.0, scope="route"
        )
        ladder.record_failure("cc/claude-sonnet-5", failure, now=NOW)
        v = by_route(ladder.evaluate(REQ, now=NOW + timedelta(seconds=89)))["cc/claude-sonnet-5"]
        assert v.failed_stage is EligibilityStage.AVAILABLE
        assert v.cooldown_until == (NOW + timedelta(seconds=90)).isoformat()

    def test_category_default_cooldown_when_no_retry_after(self, tmp_path: Path) -> None:
        ladder, _ = make_ladder(
            tmp_path, [row("cc/claude-sonnet-5", owned_by="claude")], [conn("claude")]
        )
        failure = FailureClassification(
            category="quota_exhausted", action="REROUTE", cooldown_seconds=0.0, scope="route"
        )
        ladder.record_failure("cc/claude-sonnet-5", failure, now=NOW)
        v = ladder.evaluate(REQ, now=NOW + timedelta(seconds=3599))[0]
        assert v.failed_stage is EligibilityStage.AVAILABLE
        assert v.cooldown_until == (NOW + timedelta(seconds=3600)).isoformat()

    def test_provider_scope_cooldown_hits_sibling_routes(self, tmp_path: Path) -> None:
        rows = [
            row("cc/claude-sonnet-5", owned_by="claude"),
            row("cc/claude-haiku-5", owned_by="claude"),
            row("cx/gpt-6-codex", owned_by="codex"),
        ]
        ladder, _ = make_ladder(tmp_path, rows, [conn("claude"), conn("codex")])
        failure = FailureClassification(
            category="quota_exhausted", action="REROUTE", cooldown_seconds=0.0, scope="provider"
        )
        ladder.record_failure("cc/claude-sonnet-5", failure, now=NOW)
        verdicts = by_route(ladder.evaluate(REQ, now=NOW + timedelta(seconds=10)))
        # sibling route of the same provider is also cooled down
        assert verdicts["cc/claude-haiku-5"].failed_stage is EligibilityStage.AVAILABLE
        assert verdicts["cc/claude-haiku-5"].reason == "cooldown:provider"
        # a different provider is unaffected
        assert verdicts["cx/gpt-6-codex"].failed_stage is None

    def test_cooldown_expiry_readmits_route(self, tmp_path: Path) -> None:
        ladder, _probe = make_ladder(
            tmp_path, [row("cc/claude-sonnet-5", owned_by="claude")], [conn("claude")]
        )
        failure = FailureClassification(
            category="rate_limited", action="REROUTE", cooldown_seconds=60.0, scope="route"
        )
        ladder.record_failure("cc/claude-sonnet-5", failure, now=NOW)
        assert ladder.select(REQ, now=NOW + timedelta(seconds=30))[0] is None
        selected, _ = ladder.select(REQ, now=NOW + timedelta(seconds=61))
        assert selected is not None and selected.route_id == "cc/claude-sonnet-5"

    def test_connection_rate_limited_until_blocks(self, tmp_path: Path) -> None:
        until = (NOW + timedelta(seconds=120)).isoformat()
        ladder, _ = make_ladder(
            tmp_path,
            [row("cc/claude-sonnet-5", owned_by="claude")],
            [conn("claude", rate_limited_until={"default": until})],
        )
        v = ladder.evaluate(REQ, now=NOW)[0]
        assert v.failed_stage is EligibilityStage.AVAILABLE
        assert v.reason == "provider_rate_limited"
        assert v.cooldown_until == until


class TestTaskEligible:
    def test_missing_tool_capability(self, tmp_path: Path) -> None:
        ladder, _ = make_ladder(
            tmp_path, [row("cc/claude-sonnet-5", owned_by="claude", tools=False)], [conn("claude")]
        )
        v = ladder.evaluate(REQ, now=NOW)[0]
        assert v.failed_stage is EligibilityStage.TASK_ELIGIBLE
        assert v.reason == "missing_capability:tools"

    def test_reasoning_capability_required(self, tmp_path: Path) -> None:
        req = TaskRequirements(required_capabilities=frozenset({"tools", "reasoning"}))
        ladder, _ = make_ladder(
            tmp_path,
            [row("cc/claude-sonnet-5", owned_by="claude", reasoning=False)],
            [conn("claude")],
        )
        v = ladder.evaluate(req, now=NOW)[0]
        assert v.failed_stage is EligibilityStage.TASK_ELIGIBLE
        assert v.reason == "missing_capability:reasoning"

    def test_insufficient_context(self, tmp_path: Path) -> None:
        ladder, _ = make_ladder(
            tmp_path,
            [row("cc/claude-sonnet-5", owned_by="claude", context=8_000)],
            [conn("claude")],
        )
        v = ladder.evaluate(REQ, now=NOW)[0]
        assert v.failed_stage is EligibilityStage.TASK_ELIGIBLE
        assert v.reason == "insufficient_context"

    def test_frontier_restricted_unless_worthy(self, tmp_path: Path) -> None:
        # Metered frontier: kept off non-frontier-worthy work.
        rows = [row("cx/gpt-6-sol", owned_by="codex")]
        ladder, _ = make_ladder(tmp_path, rows, [conn("codex", auth="apikey", plan="pay")])
        v = ladder.evaluate(REQ, now=NOW)[0]
        assert v.failed_stage is EligibilityStage.TASK_ELIGIBLE
        assert v.reason == "frontier_restricted"
        worthy = TaskRequirements(frontier_worthy=True)
        assert ladder.evaluate(worthy, now=NOW)[0].failed_stage is None

    def test_prepaid_frontier_is_an_allowed_fallback(self, tmp_path: Path) -> None:
        # Subscription frontier costs nothing extra per call: not restricted.
        rows = [row("kr/claude-opus-5.5", owned_by="kiro")]
        ladder, _ = make_ladder(tmp_path, rows, [conn("kiro")])
        assert ladder.evaluate(REQ, now=NOW)[0].failed_stage is None

    def test_prepaid_frontier_never_beats_a_cheaper_sufficient_route(self, tmp_path: Path) -> None:
        rows = [
            row("kr/claude-opus-5.5", owned_by="kiro"),
            row("kr/claude-haiku-4.5", owned_by="kiro"),
        ]
        ladder, _ = make_ladder(tmp_path, rows, [conn("kiro")])
        chosen, _ = ladder.select(REQ, now=NOW)
        assert chosen is not None and chosen.route_id == "kr/claude-haiku-4.5"

    def test_frontier_guard_uses_tier_not_name_list(self, tmp_path: Path) -> None:
        # A frontier model whose name is on no list is still guarded by tier.
        rows = [{**row("cx/new-frontier", owned_by="codex"), "capability_tier": 0}]
        ladder, _ = make_ladder(tmp_path, rows, [conn("codex", auth="apikey", plan="pay")])
        assert ladder.evaluate(REQ, now=NOW)[0].reason == "frontier_restricted"

    def test_exclude_families_for_reviewer_independence(self, tmp_path: Path) -> None:
        rows = [
            row("cc/claude-sonnet-5", owned_by="claude"),
            row("cx/gpt-6-codex", owned_by="codex"),
        ]
        ladder, _ = make_ladder(tmp_path, rows, [conn("claude"), conn("codex")])
        req = TaskRequirements(exclude_families=frozenset({"claude"}))
        verdicts = by_route(ladder.evaluate(req, now=NOW))
        assert verdicts["cc/claude-sonnet-5"].reason == "excluded_family"
        assert verdicts["cc/claude-sonnet-5"].failed_stage is EligibilityStage.TASK_ELIGIBLE
        assert verdicts["cx/gpt-6-codex"].failed_stage is None

    def test_kc_free_worker_excludes_openrouter_free_reviewer(self, tmp_path: Path) -> None:
        rows = [
            row("kc/cohere/north-mini-code:free", owned_by="kilocode"),
            row("openrouter/cohere/north-mini-code:free", owned_by="openrouter"),
        ]
        ladder, _ = make_ladder(tmp_path, rows, [conn("kilocode"), conn("openrouter")])
        req = TaskRequirements(exclude_routes=frozenset({"kc/cohere/north-mini-code:free"}))
        verdicts = by_route(ladder.evaluate(req, now=NOW))
        assert verdicts["kc/cohere/north-mini-code:free"].reason == "excluded_route"
        assert verdicts["openrouter/cohere/north-mini-code:free"].reason == "excluded_family"

    def test_exclude_routes(self, tmp_path: Path) -> None:
        ladder, _ = make_ladder(
            tmp_path, [row("cc/claude-sonnet-5", owned_by="claude")], [conn("claude")]
        )
        req = TaskRequirements(exclude_routes=frozenset({"cc/claude-sonnet-5"}))
        assert ladder.evaluate(req, now=NOW)[0].reason == "excluded_route"

    def test_effort_suffix_duplicate_skipped_only_with_base(self, tmp_path: Path) -> None:
        rows = [
            row("cx/gpt-6-codex", owned_by="codex"),
            row("cx/gpt-6-codex-high", owned_by="codex"),
            row("zz/solo-model-max", owned_by="zz"),
        ]
        ladder, _ = make_ladder(tmp_path, rows, [conn("codex"), conn("zz")])
        verdicts = by_route(ladder.evaluate(REQ, now=NOW))
        assert verdicts["cx/gpt-6-codex-high"].reason == "effort_duplicate"
        assert verdicts["cx/gpt-6-codex"].failed_stage is None
        assert verdicts["zz/solo-model-max"].failed_stage is None  # no base id exists


class TestCapacityAndRanking:
    def test_capacity_class_from_evidence(self, tmp_path: Path) -> None:
        rows = [
            row("cc/claude-sonnet-5", owned_by="claude"),
            row("gl/glm-5", owned_by="glm", pricing={"input": 0.0, "output": 0.0}),
            row("op/qwen3-coder", owned_by="openrouter"),
            row("mm/minimax-m2", owned_by="minimax"),
        ]
        connections = [
            conn("claude", auth="oauth", plan="claude_max"),
            conn("glm", auth="apikey", plan="", free_only=True),
            conn("openrouter", auth="apikey", plan="payg"),
            conn("minimax", auth="apikey", plan="", free_only=False),
        ]
        rows[3]["pricing"] = {}
        ladder, _ = make_ladder(tmp_path, rows, connections)
        verdicts = by_route(ladder.evaluate(REQ, now=NOW))
        assert verdicts["cc/claude-sonnet-5"].capacity_class is CapacityClass.SUBSCRIPTION
        assert verdicts["gl/glm-5"].capacity_class is CapacityClass.FREE
        assert verdicts["op/qwen3-coder"].capacity_class is CapacityClass.METERED
        assert verdicts["mm/minimax-m2"].capacity_class is CapacityClass.UNKNOWN

    def test_free_before_subscription_for_workers(self, tmp_path: Path) -> None:
        """Free-first: implementation workers rank FREE before SUBSCRIPTION.

        A health cache with a fresh agentic PASS is required for the FREE
        route to be implementation-eligible. Without it, FREE routes are
        rejected with ``no_health_cache``.
        """
        from verdict.orchestration.health_cache import CATEGORY_OK, HealthCache, ProbeResult

        rows = [
            row("op/qwen3-coder", owned_by="openrouter"),
            row("gl/glm-5", owned_by="glm", pricing={"input": 0.0, "output": 0.0}),
            row("cc/claude-sonnet-5", owned_by="claude"),
        ]
        connections = [
            conn("openrouter", auth="apikey", plan="payg"),
            conn("glm", auth="apikey", plan="free", free_only=True),
            conn("claude", auth="oauth", plan="claude_max"),
        ]
        # Provide a cache with a fresh agentic PASS for the free route.
        cache = HealthCache(tmp_path / "health-cache.json")
        checked = NOW - timedelta(seconds=60)
        cache.record(
            "gl/glm-5",
            ProbeResult(
                category=CATEGORY_OK,
                chat_ok=True,
                tool_ok=True,
                probe_class="agentic",
                agentic_ok=True,
            ),
            checked,
        )
        cache.save()
        ladder, _ = make_ladder(tmp_path, rows, connections, health_cache=cache)
        verdicts = ladder.evaluate(REQ, now=NOW)
        ranked = sorted((v for v in verdicts if v.rank is not None), key=lambda v: v.rank or 0)
        # Free-first: gl/glm-5 (FREE) before cc/claude-sonnet-5 (SUBSCRIPTION)
        assert [v.route_id for v in ranked] == ["gl/glm-5", "cc/claude-sonnet-5", "op/qwen3-coder"]

    def test_free_first_for_frontier_worthy(self, tmp_path: Path) -> None:
        """Planning/controller/review ranks sufficient FREE capacity first."""
        rows = [
            row("op/qwen3-coder", owned_by="openrouter"),
            row("gl/glm-5", owned_by="glm", pricing={"input": 0.0, "output": 0.0}),
            row("cc/claude-sonnet-5", owned_by="claude"),
        ]
        connections = [
            conn("openrouter", auth="apikey", plan="payg"),
            conn("glm", auth="apikey", plan="free", free_only=True),
            conn("claude", auth="oauth", plan="claude_max"),
        ]
        ladder, _ = make_ladder(tmp_path, rows, connections)
        frontier_req = TaskRequirements(frontier_worthy=True, max_capability_tier=3)
        verdicts = ladder.evaluate(frontier_req, now=NOW)
        ranked = sorted((v for v in verdicts if v.rank is not None), key=lambda v: v.rank or 0)
        # Free first for frontier work
        assert [v.route_id for v in ranked] == ["gl/glm-5", "cc/claude-sonnet-5", "op/qwen3-coder"]

    def test_prefer_providers_is_configurable(self, tmp_path: Path) -> None:
        # Preference orders routes of equal capability; declare both tiers.
        rows = [
            {**row("cc/claude-sonnet-5", owned_by="claude"), "capability_tier": 1},
            {**row("cx/gpt-6-codex", owned_by="codex"), "capability_tier": 1},
        ]
        connections = [conn("claude", plan="max"), conn("codex", plan="pro")]
        default, _ = make_ladder(tmp_path, rows, connections)
        selected, _ = default.select(REQ, now=NOW)
        assert selected is not None and selected.route_id == "cc/claude-sonnet-5"
        flipped, _ = make_ladder(tmp_path / "flip", rows, connections, prefer_providers=("codex",))
        selected, _ = flipped.select(REQ, now=NOW)
        assert selected is not None and selected.route_id == "cx/gpt-6-codex"

    def test_at_capacity_spreads_load(self, tmp_path: Path) -> None:
        rows = [
            row("cc/claude-sonnet-5", owned_by="claude"),
            row("cc/claude-haiku-5", owned_by="claude"),
        ]
        loads = {"cc/claude-sonnet-5": 2}
        ladder, _ = make_ladder(
            tmp_path, rows, [conn("claude")], load=lambda r: loads.get(r, 0), max_per_route=2
        )
        selected, verdicts = ladder.select(REQ, now=NOW)
        assert selected is not None and selected.route_id == "cc/claude-haiku-5"
        assert by_route(verdicts)["cc/claude-sonnet-5"].reason == "at_capacity"

    def test_lower_load_ranks_first_within_class(self, tmp_path: Path) -> None:
        rows = [
            row("cc/claude-sonnet-5", owned_by="claude"),
            row("cc/claude-haiku-5", owned_by="claude"),
        ]
        loads = {"cc/claude-sonnet-5": 1}
        ladder, _ = make_ladder(tmp_path, rows, [conn("claude")], load=lambda r: loads.get(r, 0))
        req = TaskRequirements(coding=False)
        selected, _ = ladder.select(req, now=NOW)
        assert selected is not None and selected.route_id == "cc/claude-haiku-5"


class TestSelect:
    def test_max_probes_bound(self, tmp_path: Path) -> None:
        rows = [row(f"pp/model-{i:02d}", owned_by="pp") for i in range(6)]
        probe = FakeProbe({r["id"]: HealthResult(healthy=False, category="timeout") for r in rows})
        ladder, _ = make_ladder(tmp_path, rows, [conn("pp")], probe, max_probes_per_select=3)
        selected, verdicts = ladder.select(REQ, now=NOW)
        assert selected is None
        assert len(probe.calls) == 3
        reasons = {v.reason for v in verdicts}
        assert "probe_budget_exhausted" in reasons

    def test_select_skips_unhealthy_and_picks_next(self, tmp_path: Path) -> None:
        rows = [
            row("cc/claude-sonnet-5", owned_by="claude"),
            row("cx/gpt-6-codex", owned_by="codex"),
        ]
        probe = FakeProbe(
            {
                "cc/claude-sonnet-5": HealthResult(
                    healthy=False, category="rate_limited", retry_after_seconds=42.0
                )
            }
        )
        ladder, _ = make_ladder(tmp_path, rows, [conn("claude"), conn("codex")], probe)
        selected, verdicts = ladder.select(REQ, now=NOW)
        assert selected is not None and selected.route_id == "cx/gpt-6-codex"
        assert selected.reached is EligibilityStage.SELECTED
        failed = by_route(verdicts)["cc/claude-sonnet-5"]
        assert failed.failed_stage is EligibilityStage.HEALTHY
        assert failed.reason == "rate_limited"
        # retry_after drove the persisted cooldown
        until = (NOW + timedelta(seconds=42)).isoformat()
        v = by_route(ladder.evaluate(REQ, now=NOW + timedelta(seconds=10)))["cc/claude-sonnet-5"]
        assert v.cooldown_until == until


class TestPersistence:
    def test_state_round_trip(self, tmp_path: Path) -> None:
        rows = [row("cc/claude-sonnet-5", owned_by="claude")]
        ladder, _probe = make_ladder(tmp_path, rows, [conn("claude")])
        selected, _ = ladder.select(REQ, now=NOW)
        assert selected is not None
        failure = FailureClassification(
            category="rate_limited", action="REROUTE", cooldown_seconds=300.0, scope="route"
        )
        ladder.record_failure("cc/claude-sonnet-5", failure, now=NOW)
        # a brand-new ladder instance sees the persisted cooldown and health
        reloaded, probe2 = make_ladder(tmp_path, rows, [conn("claude")])
        v = reloaded.evaluate(REQ, now=NOW + timedelta(seconds=10))[0]
        assert v.failed_stage is not None
        assert v.cooldown_until == (NOW + timedelta(seconds=300)).isoformat()
        assert probe2.calls == []
        assert not (tmp_path / "state.json.tmp").exists()

    def test_record_success_clears_cooldown(self, tmp_path: Path) -> None:
        rows = [row("cc/claude-sonnet-5", owned_by="claude")]
        ladder, _ = make_ladder(tmp_path, rows, [conn("claude")])
        failure = FailureClassification(
            category="rate_limited", action="REROUTE", cooldown_seconds=300.0, scope="route"
        )
        ladder.record_failure("cc/claude-sonnet-5", failure, now=NOW)
        ladder.record_success("cc/claude-sonnet-5", now=NOW + timedelta(seconds=5))
        selected, _ = ladder.select(REQ, now=NOW + timedelta(seconds=6))
        assert selected is not None and selected.route_id == "cc/claude-sonnet-5"


class TestSummary:
    def test_summary_counts_per_stage(self, tmp_path: Path) -> None:
        rows = [
            row("cc/claude-sonnet-5", owned_by="claude"),
            row("cx/gpt-6-codex", owned_by="codex"),
            row("zz/orphan-model", owned_by="zz"),
            row("cc/claude-opus-5", owned_by="claude"),
        ]
        ladder, _ = make_ladder(tmp_path, rows, [conn("claude"), conn("codex")])
        selected, _ = ladder.select(REQ, now=NOW)
        assert selected is not None
        summary = ladder.summary()
        assert summary["discovered"] == 4
        assert summary["entitled"] == 3  # zz has no account
        assert summary["healthy"] >= 1  # the selected route was probed healthy
        assert summary["eligible"] >= 1
        assert set(summary) == {"discovered", "entitled", "healthy", "available", "eligible"}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


def test_dispatch_blocker_reads_cooldowns_written_after_selection(tmp_path: Path) -> None:
    """BOD-223: a cooldown persisted by another writer after load blocks dispatch."""
    import json

    ladder, _ = make_ladder(tmp_path, [row("cc/a"), row("kr/b")], [])
    assert ladder.dispatch_blocker("cc/a", now=NOW) is None
    until = (NOW + timedelta(minutes=10)).isoformat()
    (tmp_path / "state.json").write_text(
        json.dumps(
            {
                "health": {},
                "cooldowns": {"provider:cc": {"until": until, "category": "rate_limited"}},
            }
        )
    )
    assert ladder.dispatch_blocker("cc/a", now=NOW) == "provider:cc"
    assert ladder.dispatch_blocker("kr/b", now=NOW) is None
    # Expired evidence does not block.
    assert ladder.dispatch_blocker("cc/a", now=NOW + timedelta(hours=1)) is None


class TestDynamicAssignment:
    """BOD-271: the cheapest SUFFICIENT route wins; insufficient routes are dropped."""

    ROWS = (
        row("cc/claude-opus-5", owned_by="claude"),  # tier 0 (frontier)
        {**row("cc/claude-sonnet-5", owned_by="claude"), "capability_tier": 2},  # declared
        row("cc/claude-haiku-5", owned_by="claude"),  # tier 3 (small)
    )

    def _select(self, tmp_path: Path, req: TaskRequirements) -> tuple[str | None, dict[str, Any]]:
        ladder, _ = make_ladder(tmp_path, list(self.ROWS), [conn("claude")])
        chosen, verdicts = ladder.select(req, now=NOW)
        return (chosen.route_id if chosen else None), by_route(verdicts)

    def test_cheaper_sufficient_beats_unnecessary_premium(self, tmp_path: Path) -> None:
        low_risk = TaskRequirements.for_node(
            WorkNode(
                "a",
                "rename a helper",
                owned_files=("x.py",),
                verification_command=("true",),
                risk="low",
            )
        )
        chosen, _ = self._select(tmp_path, replace(low_risk, frontier_worthy=True))
        # Even when frontier routes are allowed, bounded work takes the small model.
        assert chosen == "cc/claude-haiku-5"

    def test_insufficient_cheap_route_is_dropped_before_ranking(self, tmp_path: Path) -> None:
        review = TaskRequirements.for_node(WorkNode("r", "review", kind=NodeKind.REVIEW))
        chosen, verdicts = self._select(tmp_path, review)
        assert verdicts["cc/claude-haiku-5"].failed_stage is EligibilityStage.TASK_ELIGIBLE
        assert verdicts["cc/claude-haiku-5"].reason == "insufficient_capability"
        assert verdicts["cc/claude-sonnet-5"].reason == "insufficient_capability"
        assert chosen == "cc/claude-opus-5"

    def test_different_work_units_get_different_models_from_same_inventory(
        self, tmp_path: Path
    ) -> None:
        bounded = TaskRequirements.for_node(
            WorkNode(
                "a", "fix typo", owned_files=("x.py",), verification_command=("true",), risk="low"
            )
        )
        medium = TaskRequirements.for_node(
            WorkNode(
                "b",
                "refactor module",
                owned_files=("x.py",),
                verification_command=("true",),
                risk="medium",
            )
        )
        high = TaskRequirements.for_node(
            WorkNode(
                "c",
                "security change",
                owned_files=("x.py",),
                verification_command=("true",),
                risk="high",
            )
        )
        picks = {
            name: self._select(tmp_path / name, replace(req, frontier_worthy=True))[0]
            for name, req in (("a", bounded), ("b", medium), ("c", high))
        }
        assert picks == {
            "a": "cc/claude-haiku-5",
            "b": "cc/claude-sonnet-5",
            "c": "cc/claude-opus-5",
        }

    def test_declared_tier_outranks_name_heuristics(self, tmp_path: Path) -> None:
        rows = [
            {**row("kr/mystery-large", owned_by="kiro"), "capability_tier": 1},
            row("kr/claude-haiku-5", owned_by="kiro"),
        ]
        ladder, _ = make_ladder(tmp_path, rows, [conn("kiro")])
        req = TaskRequirements.for_node(
            WorkNode("c", "x", owned_files=("x.py",), verification_command=("true",), risk="high")
        )
        chosen, verdicts = ladder.select(req, now=NOW)
        assert chosen is not None and chosen.route_id == "kr/mystery-large"
        assert by_route(verdicts)["kr/claude-haiku-5"].reason == "insufficient_capability"

    def test_equal_tier_prefers_lower_metered_price(self, tmp_path: Path) -> None:
        rows = [
            row("cx/gpt-5.4", owned_by="codex", pricing={"input": 5.0, "output": 15.0}),
            row("cx/gpt-5.4-alt", owned_by="codex", pricing={"input": 1.0, "output": 3.0}),
        ]
        ladder, _ = make_ladder(tmp_path, rows, [conn("codex", auth="apikey", plan="pay")])
        chosen, _ = ladder.select(TaskRequirements(), now=NOW)
        assert chosen is not None and chosen.route_id == "cx/gpt-5.4-alt"


def test_unknown_capability_is_never_promoted_to_sufficient(tmp_path: Path) -> None:
    """BOD-271: an unrecognized model is not silently medium tier."""
    rows = [row("kr/mystery-model", owned_by="kiro")]
    ladder, _ = make_ladder(tmp_path, rows, [conn("kiro")])
    medium = TaskRequirements(max_capability_tier=2)
    chosen, verdicts = ladder.select(medium, now=NOW)
    assert chosen is None
    assert by_route(verdicts)["kr/mystery-model"].reason == "unknown_capability"
    # Bounded work that accepts any tier may still use it.
    chosen, _ = ladder.select(TaskRequirements(max_capability_tier=3), now=NOW)
    assert chosen is not None and chosen.route_id == "kr/mystery-model"


@pytest.mark.parametrize(
    ("route_id", "tier"),
    [
        ("cc/claude-opus-5-5", 0),
        ("kr/claude-opus-5.5", 0),
        ("cc/claude-fable-5-1", 0),
        ("cx/gpt-6-sol", 0),
        ("cx/gpt-6-astra", 0),
        ("cx/gpt-5.6-sol", 0),
        ("kr/claude-sonnet-5", 1),
        ("kiro/claude-sonnet-5-thinking", 1),
        ("cc/claude-haiku-4-5-20251001", 3),
    ],
)
def test_current_live_families_have_known_tiers(route_id: str, tier: int) -> None:
    """Real OmniRoute route ids in use must not fall into unknown capability."""
    from verdict.classifier import classify_known

    assert classify_known(route_id) == tier


def test_unknown_capability_ranks_after_known_sufficient(tmp_path: Path) -> None:
    """For bounded work an unknown route is usable, but never beats a known one."""
    rows = [row("kr/mystery-model", owned_by="kiro"), row("kr/claude-sonnet-5", owned_by="kiro")]
    ladder, _ = make_ladder(tmp_path, rows, [conn("kiro")])
    chosen, _ = ladder.select(TaskRequirements(max_capability_tier=3), now=NOW)
    assert chosen is not None and chosen.route_id == "kr/claude-sonnet-5"


# ---------------------------------------------------------------------------
# BOD-292: selection-before-dispatch refresh hook
# ---------------------------------------------------------------------------


class TestSelectionRefreshHook:
    def test_hook_receives_candidate_ids_before_probing(self, tmp_path: Path) -> None:
        rows = [
            row("cc/claude-sonnet-5", owned_by="claude"),
            row("cc/claude-opus-5", owned_by="claude"),
        ]
        seen: list[tuple[list[str], datetime]] = []

        def hook(ids, now):  # type: ignore[no-untyped-def]
            seen.append((list(ids), now))

        ladder, _probe = make_ladder(tmp_path, rows, [conn("claude")], refresh_hook=hook)
        selected, _verdicts = ladder.select(REQ, now=NOW)
        # The hook fired once with the candidate ids and the select clock.
        assert len(seen) == 1
        assert set(seen[0][0]) <= {"cc/claude-sonnet-5", "cc/claude-opus-5"}
        assert seen[0][1] == NOW
        # Selection still works and still confirms a route.
        assert selected is not None

    def test_default_behaviour_unchanged_without_hook(self, tmp_path: Path) -> None:
        rows = [row("cc/claude-sonnet-5", owned_by="claude")]
        ladder_no_hook, _ = make_ladder(tmp_path, rows, [conn("claude")])
        selected_a, _verdicts_a = ladder_no_hook.select(REQ, now=NOW)

        ladder_hook, _ = make_ladder(
            tmp_path, rows, [conn("claude")], refresh_hook=lambda ids, now: None
        )
        selected_b, _verdicts_b = ladder_hook.select(REQ, now=NOW)
        # The hook is advisory; the selected route is identical.
        assert (selected_a is None) == (selected_b is None)
        if selected_a is not None and selected_b is not None:
            assert selected_a.route_id == selected_b.route_id

    def test_hook_failure_never_breaks_selection(self, tmp_path: Path) -> None:
        rows = [row("cc/claude-sonnet-5", owned_by="claude")]

        def boom(ids, now):  # type: ignore[no-untyped-def]
            raise RuntimeError("refresh unavailable")

        ladder, _ = make_ladder(tmp_path, rows, [conn("claude")], refresh_hook=boom)
        selected, _verdicts = ladder.select(REQ, now=NOW)
        # A hook exception is swallowed; selection still proceeds.
        assert selected is not None

    def test_refresh_failed_unavailable_ids_are_not_confirmed(self, tmp_path: Path) -> None:
        ids = ("cc/claude-sonnet-5", "cc/claude-opus-5", "cc/claude-haiku-4-5")
        rows = [row(rid, owned_by="claude") for rid in ids]

        def hook(_ids: list[str], _now: datetime) -> dict[str, str]:
            return {ids[0]: "FAILED", ids[1]: "unavailable"}

        ladder, probe = make_ladder(tmp_path, rows, [conn("claude")], refresh_hook=hook)
        chosen, verdicts = ladder.select(REQ, now=NOW)
        assert chosen is not None and chosen.route_id == ids[2]
        assert set(probe.calls).isdisjoint(ids[:2])
        by_id = by_route(verdicts)
        assert by_id[ids[0]].reason == "failed"
        assert by_id[ids[1]].reason == "unavailable"


@pytest.mark.parametrize("frontier", [False, True])
@pytest.mark.parametrize("scope", ["route", "provider"])
def test_pool_cooldown_blocks_aliases_until_expiry(tmp_path, frontier, scope):
    routes = ["cc/claude-haiku", "claude/claude-haiku", "no-think/cc/claude-haiku"]
    ladder, probe = make_ladder(
        tmp_path, [row(r, owned_by="claude") for r in routes], [conn("claude")]
    )
    ladder.record_failure(
        routes[0], FailureClassification("rate_limited", "REASSIGN", 60, scope), now=NOW
    )
    req = TaskRequirements(frontier_worthy=frontier)
    for alias in routes[1:]:
        assert ladder.dispatch_blocker(alias, now=NOW) == "pool:claude"
        verdict = by_route(ladder.evaluate(req, now=NOW))[alias]
        assert verdict.reason == "cooldown:pool"
        assert verdict.cooldown_scope == "pool:claude"
        assert ladder.dispatch_blocker(alias, now=NOW + timedelta(seconds=61)) is None
    assert not probe.calls


def test_pool_exclusion_drops_aliases_not_other_backend(tmp_path):
    routes = ["cc/claude-haiku", "claude/claude-haiku", "no-think/cc/claude-haiku", "kr/claude-haiku"]
    ladder, _ = make_ladder(
        tmp_path, [row(r, owned_by="claude") for r in routes], [conn("claude")]
    )
    req = TaskRequirements(exclude_pools=frozenset({"claude"}))
    verdicts = by_route(ladder.evaluate(req, now=NOW))
    assert all(verdicts[r].reason == "excluded_pool" for r in routes[:3])
    assert verdicts[routes[3]].failed_stage is None


@pytest.mark.parametrize("free_tier, expected", [(2, "gl/glm-5"), (3, "cc/claude-sonnet-5")])
def test_planner_free_first_respects_capability_floor(tmp_path, free_tier, expected):
    ladder, _ = make_ladder(
        tmp_path,
        [
            {**row("gl/glm-5", owned_by="glm"), "capability_tier": free_tier},
            {**row("cc/claude-sonnet-5", owned_by="claude"), "capability_tier": 1},
        ],
        [conn("glm", plan="free"), conn("claude")],
    )
    choice, _ = ladder.select(
        TaskRequirements(frontier_worthy=True, max_capability_tier=2), now=NOW
    )
    assert choice is not None and choice.route_id == expected
