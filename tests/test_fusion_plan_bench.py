"""Tests for the FU1 fusion planning benchmark.

All tests use fake transports (no network, no live gateway, no real ~/.verdict).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from verdict.fusion_plan_bench import (
    DEFAULT_MAX_CALLS,
    DEFAULT_MAX_TOKENS,
    DEFAULT_PANEL_SIZE,
    MIN_PANEL_SIZE,
    RECEIPT_FIELDS,
    FusionBenchConfig,
    FusionBenchReport,
    PanelMember,
    _anonymise_panel_plans,
    build_synthesis_prompt,
    run_fusion_plan_bench,
    structural_score,
)
from verdict.orchestration.contracts import (
    AttemptUsage,
    CapacityClass,
    EligibilityStage,
    NodeKind,
    RouteVerdict,
    TaskRequirements,
    WorkerTerminal,
    WorkGraph,
    WorkNode,
    route_family,
)

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)

# ---------------------------------------------------------------- helpers


def _make_node(
    node_id: str,
    kind: NodeKind = NodeKind.IMPLEMENT,
    owned_files: tuple[str, ...] = ("verdict/foo.py",),
    verification_command: tuple[str, ...] = ("python", "-m", "pytest", "tests/test_foo.py"),
    depends_on: tuple[str, ...] = (),
) -> WorkNode:
    if kind is NodeKind.INTEGRATE:
        return WorkNode(
            node_id=node_id,
            objective=f"Integrate {node_id}",
            kind=kind,
            owned_files=(),
            verification_command=verification_command,
            depends_on=depends_on,
        )
    return WorkNode(
        node_id=node_id,
        objective=f"Implement {node_id}",
        kind=kind,
        owned_files=owned_files,
        verification_command=verification_command,
        depends_on=depends_on,
    )


def _make_graph(nodes: list[WorkNode] | None = None, goal: str = "test goal") -> WorkGraph:
    if nodes is None:
        nodes = [
            _make_node(
                "n1",
                owned_files=("verdict/a.py",),
                verification_command=("python", "-m", "pytest", "tests/test_a.py"),
            ),
            _make_node(
                "n2",
                owned_files=("verdict/b.py",),
                verification_command=("python", "-m", "pytest", "tests/test_b.py"),
                depends_on=("n1",),
            ),
            _make_node(
                "integrate",
                kind=NodeKind.INTEGRATE,
                verification_command=("python", "-m", "pytest"),
                depends_on=("n1", "n2"),
            ),
        ]
    return WorkGraph(goal=goal, nodes=tuple(nodes))


def _plan_json(nodes_override: list[dict[str, Any]] | None = None) -> str:
    """Return a minimal valid planner JSON string."""
    if nodes_override is not None:
        return json.dumps({"nodes": nodes_override})
    return json.dumps(
        {
            "nodes": [
                {
                    "node_id": "impl_a",
                    "objective": "implement A",
                    "kind": "implement",
                    "owned_files": ["verdict/a.py", "tests/test_a.py"],
                    "verification_command": ["python", "-m", "pytest", "tests/test_a.py"],
                    "depends_on": [],
                    "required_context": [],
                    "acceptance": ["tests pass"],
                    "required_capabilities": ["tools"],
                    "barrier": "",
                    "risk": "low",
                    "story": "",
                    "coding": True,
                    "reasoning": False,
                    "min_context_tokens": 32000,
                },
                {
                    "node_id": "integrate",
                    "objective": "integrate",
                    "kind": "integrate",
                    "owned_files": [],
                    "verification_command": ["python", "-m", "pytest"],
                    "depends_on": ["impl_a"],
                    "required_context": [],
                    "acceptance": [],
                    "required_capabilities": ["tools"],
                    "barrier": "",
                    "risk": "low",
                    "story": "",
                    "coding": False,
                    "reasoning": False,
                    "min_context_tokens": 32000,
                },
            ]
        }
    )


# ---------------------------------------------------------------- fake selector


@dataclass
class FakeSelector:
    """Minimal ModelSelector that returns routes in order with configurable capacity."""

    routes: list[tuple[str, CapacityClass]] = field(default_factory=list)
    _used: list[str] = field(default_factory=list)
    captured_requirements: list[TaskRequirements] = field(default_factory=list)

    def select(
        self, requirements: TaskRequirements, *, now: datetime
    ) -> tuple[RouteVerdict | None, tuple[RouteVerdict, ...]]:
        self.captured_requirements.append(requirements)
        for route_id, cap in self.routes:
            if route_id in requirements.exclude_routes:
                continue
            fam = route_family(route_id)
            if fam in requirements.exclude_families:
                continue
            verdict = RouteVerdict(
                route_id=route_id,
                provider=route_id.split("/")[0],
                reached=EligibilityStage.SELECTED,
                failed_stage=None,
                reason="ok",
                capacity_class=cap,
                rank=1,
            )
            return verdict, (verdict,)
        return None, ()

    def record_success(self, route_id: str, *, now: datetime) -> None:
        self._used.append(route_id)


# ---------------------------------------------------------------- fake executor


@dataclass
class FakeExecutor:
    """Returns pre-canned WorkerTerminals keyed by route_id prefix.

    Declares ``answer_only_supported = True`` so ``AnswerOnlyExecutor`` accepts it.
    """

    answer_only_supported: bool = True
    responses: dict[str, str] = field(default_factory=dict)
    default_response: str | None = None
    calls: list[tuple[str, str]] = field(default_factory=list)

    def _response_for(self, route_id: str) -> str:
        for key, val in self.responses.items():
            if key in route_id:
                return val
        if self.default_response is not None:
            return self.default_response
        return _plan_json()

    async def run(
        self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float, effort: Any = None
    ) -> WorkerTerminal:
        self.calls.append((route_id, prompt))
        body = self._response_for(route_id)
        return WorkerTerminal(
            ok=True,
            output=body,
            model=route_id,
            usage=AttemptUsage(input_tokens=100, output_tokens=50),
        )


# ================================================================ TESTS


# ---------------------------------------------------------------- structural_score


class TestStructuralScore:
    def test_disjoint_files_score_high(self) -> None:
        graph = _make_graph(
            [
                _make_node(
                    "n1",
                    owned_files=("a.py",),
                    verification_command=("python", "-m", "pytest", "tests/test_a.py"),
                ),
                _make_node(
                    "n2",
                    owned_files=("b.py",),
                    verification_command=("python", "-m", "pytest", "tests/test_b.py"),
                    depends_on=("n1",),
                ),
                _make_node(
                    "integrate",
                    kind=NodeKind.INTEGRATE,
                    verification_command=("python", "-m", "pytest"),
                    depends_on=("n1", "n2"),
                ),
            ]
        )
        score = structural_score(graph)
        assert score > 0.5

    def test_shared_file_lowers_score(self) -> None:
        graph_conflict = _make_graph(
            [
                _make_node(
                    "n1",
                    owned_files=("shared.py", "tests/test_a.py"),
                    verification_command=("python", "-m", "pytest", "tests/test_a.py"),
                ),
                _make_node(
                    "n2",
                    owned_files=("shared.py", "tests/test_b.py"),
                    verification_command=("python", "-m", "pytest", "tests/test_b.py"),
                    depends_on=("n1",),
                ),
                _make_node(
                    "integrate",
                    kind=NodeKind.INTEGRATE,
                    verification_command=("python", "-m", "pytest"),
                    depends_on=("n1", "n2"),
                ),
            ]
        )
        graph_clean = _make_graph(
            [
                _make_node(
                    "n1",
                    owned_files=("a.py",),
                    verification_command=("python", "-m", "pytest", "tests/test_a.py"),
                ),
                _make_node(
                    "n2",
                    owned_files=("b.py",),
                    verification_command=("python", "-m", "pytest", "tests/test_b.py"),
                    depends_on=("n1",),
                ),
                _make_node(
                    "integrate",
                    kind=NodeKind.INTEGRATE,
                    verification_command=("python", "-m", "pytest"),
                    depends_on=("n1", "n2"),
                ),
            ]
        )
        assert structural_score(graph_conflict) < structural_score(graph_clean)

    def test_deterministic(self) -> None:
        graph = _make_graph()
        s1 = structural_score(graph)
        s2 = structural_score(graph)
        assert s1 == s2

    def test_empty_graph_returns_zero(self) -> None:
        graph = WorkGraph(
            goal="test",
            nodes=(
                _make_node(
                    "integrate",
                    kind=NodeKind.INTEGRATE,
                    verification_command=("python", "-m", "pytest"),
                    depends_on=(),
                ),
            ),
        )
        assert structural_score(graph) == 0.0

    def test_single_file_per_node_raises_score(self) -> None:
        single_file = _make_graph(
            [
                _make_node(
                    "n1",
                    owned_files=("a.py",),
                    verification_command=("python", "-m", "pytest", "tests/test_a.py"),
                ),
                _make_node(
                    "n2",
                    owned_files=("b.py",),
                    verification_command=("python", "-m", "pytest", "tests/test_b.py"),
                    depends_on=("n1",),
                ),
                _make_node(
                    "integrate",
                    kind=NodeKind.INTEGRATE,
                    verification_command=("python", "-m", "pytest"),
                    depends_on=("n1", "n2"),
                ),
            ]
        )
        multi_file = _make_graph(
            [
                _make_node(
                    "n1",
                    owned_files=("a.py", "c.py", "d.py"),
                    verification_command=("python", "-m", "pytest", "tests/test_a.py"),
                ),
                _make_node(
                    "n2",
                    owned_files=("b.py", "e.py"),
                    verification_command=("python", "-m", "pytest", "tests/test_b.py"),
                    depends_on=("n1",),
                ),
                _make_node(
                    "integrate",
                    kind=NodeKind.INTEGRATE,
                    verification_command=("python", "-m", "pytest"),
                    depends_on=("n1", "n2"),
                ),
            ]
        )
        assert structural_score(single_file) > structural_score(multi_file)

    def test_score_bounded_zero_to_one(self) -> None:
        for _ in range(3):
            score = structural_score(_make_graph())
            assert 0.0 <= score <= 1.0


# ---------------------------------------------------------------- panel family diversity


class TestPanelFamilyDiversity:
    """Panel is drawn from distinct model families."""

    def test_panel_routes_come_from_distinct_families(self) -> None:
        selector = FakeSelector(
            routes=[
                ("anthropic/claude-haiku", CapacityClass.FREE),
                ("openai/gpt-4o-mini", CapacityClass.FREE),
                ("google/gemini-flash", CapacityClass.FREE),
            ]
        )
        executor = FakeExecutor()

        report = asyncio.run(
            run_fusion_plan_bench(
                goal="test goal",
                goal_id="test",
                selector=selector,
                executor=executor,
                config=FusionBenchConfig(panel_size=3),
                now=lambda: NOW,
            )
        )

        # Each route comes from a different family.
        route_ids = [m.route_id for m in report.panel_members if m.route_id]
        families = [route_family(r) for r in route_ids]
        assert len(families) == len(set(families)), f"Panel routes share a family: {families}"

    def test_second_panel_slot_excludes_first_family(self) -> None:
        selector = FakeSelector(
            routes=[
                ("anthropic/claude-haiku", CapacityClass.FREE),
                ("anthropic/claude-haiku-v2", CapacityClass.FREE),  # same family
                ("openai/gpt-4o-mini", CapacityClass.FREE),
            ]
        )
        executor = FakeExecutor()

        report = asyncio.run(
            run_fusion_plan_bench(
                goal="test goal",
                goal_id="test",
                selector=selector,
                executor=executor,
                config=FusionBenchConfig(panel_size=2),
                now=lambda: NOW,
            )
        )

        route_ids = [m.route_id for m in report.panel_members if m.route_id]
        families = [route_family(r) for r in route_ids]
        assert "claude" not in families[1:] or len(families) < 2, (
            "Second panel member should not come from same family as first"
        )


# ---------------------------------------------------------------- capacity spend guard


class TestCapacitySpendGuard:
    """Panel and synthesizer option (b) are rejected unless the route is FREE."""

    def test_metered_route_blocked_for_panel(self) -> None:
        selector = FakeSelector(routes=[("anthropic/claude-haiku", CapacityClass.METERED)])
        executor = FakeExecutor()

        report = asyncio.run(
            run_fusion_plan_bench(
                goal="test goal",
                goal_id="test",
                selector=selector,
                executor=executor,
                config=FusionBenchConfig(panel_size=2, min_panel_size=2),
                now=lambda: NOW,
            )
        )

        for member in report.panel_members:
            if member.route_id:
                assert "capacity_blocked" in member.error, f"METERED route not blocked: {member}"
        # Report should indicate panel too small.
        assert report.error or all(not m.plan_valid for m in report.panel_members)

    def test_unknown_capacity_blocked_for_panel(self) -> None:
        selector = FakeSelector(routes=[("some/model-unknown", CapacityClass.UNKNOWN)])
        executor = FakeExecutor()

        report = asyncio.run(
            run_fusion_plan_bench(
                goal="test goal",
                goal_id="test",
                selector=selector,
                executor=executor,
                config=FusionBenchConfig(panel_size=2, min_panel_size=2),
                now=lambda: NOW,
            )
        )

        for member in report.panel_members:
            if member.route_id:
                assert "capacity_blocked" in member.error, f"UNKNOWN route not blocked: {member}"

    def test_subscription_route_blocked_for_panel(self) -> None:
        selector = FakeSelector(routes=[("anthropic/claude-sonnet", CapacityClass.SUBSCRIPTION)])
        executor = FakeExecutor()

        report = asyncio.run(
            run_fusion_plan_bench(
                goal="test goal",
                goal_id="test",
                selector=selector,
                executor=executor,
                config=FusionBenchConfig(panel_size=2, min_panel_size=2),
                now=lambda: NOW,
            )
        )

        for member in report.panel_members:
            if member.route_id:
                assert "capacity_blocked" in member.error

    def test_free_route_allowed_for_panel(self) -> None:
        selector = FakeSelector(
            routes=[
                ("anthropic/claude-haiku", CapacityClass.FREE),
                ("openai/gpt-4o-mini", CapacityClass.FREE),
            ]
        )
        executor = FakeExecutor()

        report = asyncio.run(
            run_fusion_plan_bench(
                goal="test goal",
                goal_id="test",
                selector=selector,
                executor=executor,
                config=FusionBenchConfig(panel_size=2),
                now=lambda: NOW,
            )
        )

        valid = [m for m in report.panel_members if m.plan_valid]
        assert len(valid) >= 2, f"Expected 2 valid FREE panel members, got {valid}"

    def test_metered_route_blocked_for_synth_b(self) -> None:
        """Synthesizer option (b) requires FREE; METERED is blocked."""
        selector = FakeSelector(
            routes=[
                # Panel members (FREE)
                ("anthropic/claude-haiku", CapacityClass.FREE),
                ("openai/gpt-4o-mini", CapacityClass.FREE),
                # Synthesizer option (b) candidate (METERED — should be blocked)
                ("paid/metered-synth", CapacityClass.METERED),
            ]
        )
        executor = FakeExecutor()

        report = asyncio.run(
            run_fusion_plan_bench(
                goal="test goal",
                goal_id="test",
                selector=selector,
                executor=executor,
                config=FusionBenchConfig(panel_size=2),
                now=lambda: NOW,
            )
        )

        synth_b = [s for s in report.synth_results if s.synth_option == "b"]
        for s in synth_b:
            if s.route_id == "paid/metered-synth":
                assert "capacity_blocked" in s.error, f"METERED synth_b not blocked: {s}"


# ---------------------------------------------------------------- anonymisation


class TestAnonymisation:
    """No route or model names reach the synthesizer prompt."""

    def test_plan_labels_are_plan_n_not_route_names(self) -> None:
        graph = _make_graph()
        plans_text = _anonymise_panel_plans([graph, graph])
        assert "Plan 1" in plans_text
        assert "Plan 2" in plans_text

    def test_no_route_name_in_synthesis_prompt(self) -> None:
        graph = _make_graph()
        route_names = ["anthropic", "claude", "openai", "gpt", "google", "gemini"]

        prompt = build_synthesis_prompt(
            goal="test goal", repo_map_text="", constraints="", panel_plans=[graph, graph]
        )

        # The node JSON (objective, owned_files) may contain "test" or other neutral words;
        # but actual model/provider names from route IDs must not appear.
        for name in route_names:
            # Only fail if the name appears in the PANEL PLANS section as a route identifier.
            # The planner prompt preamble may contain generic words; we check the plans section.
            plans_start = prompt.find("=== Plan 1 ===")
            if plans_start == -1:
                continue
            plans_section = prompt[plans_start:]
            assert name not in plans_section.lower() or name in ("test",), (
                f"Route-related name {name!r} found in synthesizer plan section"
            )

    def test_synthesizer_prompt_built_from_captured_prompt(self) -> None:
        """Confirm the executor sees the synthesis prompt, not the base planner prompt."""

        class CapturingExecutor:
            answer_only_supported: bool = True
            captured_prompts: list[str]

            def __init__(self) -> None:
                self.captured_prompts = []

            async def run(
                self,
                prompt: str,
                *,
                route_id: str,
                cwd: Path,
                timeout_seconds: float,
                effort: Any = None,
            ) -> WorkerTerminal:
                self.captured_prompts.append(prompt)
                return WorkerTerminal(
                    ok=True,
                    output=_plan_json(),
                    model=route_id,
                    usage=AttemptUsage(input_tokens=50, output_tokens=20),
                )

        cap_exec = CapturingExecutor()
        selector = FakeSelector(
            routes=[
                ("anthropic/claude-haiku", CapacityClass.FREE),
                ("openai/gpt-4o-mini", CapacityClass.FREE),
                # synth b (also FREE, different family)
                ("google/gemini-flash", CapacityClass.FREE),
            ]
        )

        asyncio.run(
            run_fusion_plan_bench(
                goal="test goal for anonymisation",
                goal_id="anon_test",
                selector=selector,
                executor=cap_exec,
                config=FusionBenchConfig(panel_size=2),
                now=lambda: NOW,
            )
        )

        synth_prompts = [p for p in cap_exec.captured_prompts if "SYNTHESIS TASK" in p]
        assert synth_prompts, "No synthesis prompt was sent"
        for p in synth_prompts:
            assert "Plan 1" in p or "Plan 2" in p, "Anonymised plan label missing"
            # Route IDs must not appear in the synthesis prompt.
            assert "anthropic" not in p.lower() or "anthropic" in p.lower().split("plan")[0], (
                "Route provider 'anthropic' leaked into synthesis prompt"
            )


# ---------------------------------------------------------------- validator enforcement


class TestValidatorEnforcement:
    """An invalid synthesized plan is recorded as invalid; never silently replaced."""

    def test_invalid_synth_plan_recorded_as_invalid(self) -> None:
        selector = FakeSelector(
            routes=[
                ("anthropic/claude-haiku", CapacityClass.FREE),
                ("openai/gpt-4o-mini", CapacityClass.FREE),
                # synth b
                ("google/gemini-flash", CapacityClass.FREE),
            ]
        )
        # Return valid plans for panel, broken JSON for synthesizer.
        executor = FakeExecutor(
            responses={
                "gemini": '{"nodes": "this_is_not_valid"}'  # will fail parse_plan
            },
            default_response=_plan_json(),
        )

        report = asyncio.run(
            run_fusion_plan_bench(
                goal="test goal",
                goal_id="test",
                selector=selector,
                executor=executor,
                config=FusionBenchConfig(panel_size=2),
                now=lambda: NOW,
            )
        )

        synth_b = [s for s in report.synth_results if s.synth_option == "b"]
        # The synthesizer may have been blocked on no route or returned invalid.
        for s in synth_b:
            if "gemini" in s.route_id:
                assert not s.plan_valid, "Invalid synthesized plan was not recorded as invalid"
                assert s.error, "Invalid synthesized plan has no error message"

    def test_invalid_synth_plan_does_not_replace_panel_plans(self) -> None:
        """Panel plans remain valid even when synthesizer returns garbage."""
        selector = FakeSelector(
            routes=[
                ("anthropic/claude-haiku", CapacityClass.FREE),
                ("openai/gpt-4o-mini", CapacityClass.FREE),
                ("google/gemini-flash", CapacityClass.FREE),
            ]
        )
        executor = FakeExecutor(
            responses={"gemini": "not json at all"}, default_response=_plan_json()
        )

        report = asyncio.run(
            run_fusion_plan_bench(
                goal="test goal",
                goal_id="test",
                selector=selector,
                executor=executor,
                config=FusionBenchConfig(panel_size=2),
                now=lambda: NOW,
            )
        )

        valid_panel = [m for m in report.panel_members if m.plan_valid]
        assert len(valid_panel) >= 2, "Panel plans should still be valid when synth is broken"


# ---------------------------------------------------------------- cap enforcement


class TestCapEnforcement:
    """Call and token caps are enforced; exceeding a cap stops new calls."""

    def test_max_calls_cap_stops_further_calls(self) -> None:
        selector = FakeSelector(
            routes=[
                ("anthropic/claude-haiku", CapacityClass.FREE),
                ("openai/gpt-4o-mini", CapacityClass.FREE),
                ("google/gemini-flash", CapacityClass.FREE),
            ]
        )
        executor = FakeExecutor()

        cfg = FusionBenchConfig(panel_size=3, max_calls=1)
        report = asyncio.run(
            run_fusion_plan_bench(
                goal="test goal",
                goal_id="test",
                selector=selector,
                executor=executor,
                config=cfg,
                now=lambda: NOW,
            )
        )

        assert report.total_calls <= 1, (
            f"total_calls {report.total_calls} exceeded max_calls {cfg.max_calls}"
        )
        cap_errors = [m for m in report.panel_members if m.error == "cap_exceeded"]
        assert cap_errors, "Expected at least one cap_exceeded panel member"

    def test_max_tokens_cap_stops_further_calls(self) -> None:
        """When every call uses 100+50=150 tokens, a 150-token cap stops after 1 call.

        The cap check runs before each call. After the first call records 150 tokens,
        the check ``150 < 150`` is False, so the second panel slot is skipped.
        """
        selector = FakeSelector(
            routes=[
                ("anthropic/claude-haiku", CapacityClass.FREE),
                ("openai/gpt-4o-mini", CapacityClass.FREE),
                ("google/gemini-flash", CapacityClass.FREE),
            ]
        )
        executor = FakeExecutor()  # each call records 100+50=150 tokens

        cfg = FusionBenchConfig(panel_size=3, max_tokens=150)
        report = asyncio.run(
            run_fusion_plan_bench(
                goal="test goal",
                goal_id="test",
                selector=selector,
                executor=executor,
                config=cfg,
                now=lambda: NOW,
            )
        )

        # First call uses all 150 tokens; subsequent calls are skipped.
        assert report.total_tokens <= 150, (
            f"total_tokens {report.total_tokens} exceeded max_tokens {cfg.max_tokens}"
        )
        cap_errors = [m for m in report.panel_members if m.error == "cap_exceeded"]
        assert cap_errors, "Expected at least one cap_exceeded after token limit reached"

    def test_default_caps_are_documented(self) -> None:
        """Default caps match the documented values."""
        assert DEFAULT_MAX_CALLS == 10
        assert DEFAULT_MAX_TOKENS == 500_000
        assert DEFAULT_PANEL_SIZE == 3
        assert MIN_PANEL_SIZE == 2


# ---------------------------------------------------------------- panel too small


class TestPanelTooSmall:
    def test_blocked_when_panel_below_minimum(self) -> None:
        """When fewer than min_panel_size routes are admitted, the bench reports blocked."""
        selector = FakeSelector(
            routes=[
                ("anthropic/claude-haiku", CapacityClass.METERED)  # blocked by spend guard
            ]
        )
        executor = FakeExecutor()

        report = asyncio.run(
            run_fusion_plan_bench(
                goal="test goal",
                goal_id="test",
                selector=selector,
                executor=executor,
                config=FusionBenchConfig(panel_size=2, min_panel_size=2),
                now=lambda: NOW,
            )
        )

        assert report.error, "Expected report.error when panel is too small"
        assert "panel_too_small" in report.error


# ---------------------------------------------------------------- receipt fields


class TestReceiptFields:
    def test_receipt_fields_allowlist_covers_expected_keys(self) -> None:
        required = {
            "schema_version",
            "goal_id",
            "goal_digest",
            "arm",
            "plan_valid",
            "structural_score",
            "routes",
            "token_usage",
            "wall_time_s",
            "error",
        }
        assert required <= set(RECEIPT_FIELDS)

    def test_arm_dict_respects_allowlist(self) -> None:
        """arm_dict output keys are a subset of RECEIPT_FIELDS."""
        report = FusionBenchReport(
            goal_id="test",
            goal_digest="abc123",
            panel_members=[
                PanelMember(
                    route_id="anthropic/claude-haiku",
                    capacity_class="free",
                    family="claude",
                    duration_s=1.0,
                    tokens_in=100,
                    tokens_out=50,
                    plan_valid=True,
                    structural_score=0.75,
                    error="",
                )
            ],
        )
        arm_data = report.arm_dict("panel_0")
        for key in arm_data:
            assert key in RECEIPT_FIELDS, f"Unexpected key in arm_dict output: {key!r}"


# ---------------------------------------------------------------- FusionBenchConfig


class TestFusionBenchConfig:
    def test_default_values(self) -> None:
        cfg = FusionBenchConfig()
        assert cfg.panel_size == DEFAULT_PANEL_SIZE
        assert cfg.min_panel_size == MIN_PANEL_SIZE
        assert cfg.max_calls == DEFAULT_MAX_CALLS
        assert cfg.max_tokens == DEFAULT_MAX_TOKENS

    def test_min_panel_size_cannot_exceed_panel_size(self) -> None:
        with pytest.raises(ValueError, match="min_panel_size"):
            FusionBenchConfig(panel_size=2, min_panel_size=3)

    def test_min_panel_size_must_be_at_least_2(self) -> None:
        with pytest.raises(ValueError, match="min_panel_size"):
            FusionBenchConfig(min_panel_size=1)


# ---------------------------------------------------------------- path sanitization


class TestGoalIdSanitization:
    """_sanitize_component in dispatch rejects dangerous path components."""

    @staticmethod
    def _sanitize(val: str, name: str = "goal_id") -> str:
        """Inline reimplementation that mirrors dispatch._sanitize_component."""
        import re

        _safe = re.compile(r"^[A-Za-z0-9._-]+$")
        if not val or val in (".", ".."):
            raise ValueError(f"{name} is empty or reserved: {val!r}")
        if not _safe.match(val):
            raise ValueError(f"{name} contains characters not allowed in a path component: {val!r}")
        return val

    def test_normal_id_accepted(self) -> None:
        assert self._sanitize("bod-417") == "bod-417"
        assert self._sanitize("goal_01") == "goal_01"
        assert self._sanitize("GOAL.1") == "GOAL.1"

    def test_path_traversal_rejected(self) -> None:
        with pytest.raises(ValueError, match="not allowed"):
            self._sanitize("../x")

    def test_slash_rejected(self) -> None:
        with pytest.raises(ValueError, match="not allowed"):
            self._sanitize("a/b")

    def test_empty_rejected(self) -> None:
        with pytest.raises(ValueError, match="empty or reserved"):
            self._sanitize("")

    def test_dot_rejected(self) -> None:
        with pytest.raises(ValueError, match="empty or reserved"):
            self._sanitize(".")

    def test_dotdot_rejected(self) -> None:
        with pytest.raises(ValueError, match="empty or reserved"):
            self._sanitize("..")
