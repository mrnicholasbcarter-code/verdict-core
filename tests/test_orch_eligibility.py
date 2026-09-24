"""Tests for verdict.orchestration.eligibility.EligibilityLadder."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from verdict.orchestration.contracts import (
    CapacityClass,
    EligibilityStage,
    FailureClassification,
    TaskRequirements,
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
        rows = [row("cc/claude-opus-5", owned_by="claude")]
        ladder, _ = make_ladder(tmp_path, rows, [conn("claude")])
        v = ladder.evaluate(REQ, now=NOW)[0]
        assert v.failed_stage is EligibilityStage.TASK_ELIGIBLE
        assert v.reason == "frontier_restricted"
        worthy = TaskRequirements(frontier_worthy=True)
        assert ladder.evaluate(worthy, now=NOW)[0].failed_stage is None

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

    def test_subscription_before_free_before_metered(self, tmp_path: Path) -> None:
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
        verdicts = ladder.evaluate(REQ, now=NOW)
        ranked = sorted((v for v in verdicts if v.rank is not None), key=lambda v: v.rank or 0)
        assert [v.route_id for v in ranked] == ["cc/claude-sonnet-5", "gl/glm-5", "op/qwen3-coder"]

    def test_prefer_providers_is_configurable(self, tmp_path: Path) -> None:
        rows = [
            row("cc/claude-sonnet-5", owned_by="claude"),
            row("cx/gpt-6-codex", owned_by="codex"),
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
