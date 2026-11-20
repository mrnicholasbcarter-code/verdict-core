"""Fusion planning benchmark: panel of lesser models + synthesis.

Benchmark definition (FU1 from docs/plan/20261120-learning-loop/plan.md):
- **Panel**: N admitted FREE routes from different model families each produce
  a plan via the existing planner prompt and validator.
- **Synthesis**: a synthesizer route receives the panel plans, anonymised as
  "Plan 1", "Plan 2", etc., and writes one plan from agreements, gaps, and
  resolved conflicts. The planner validator must accept the result.
- **Synthesizer options benched**: (a) highest-rated admitted planner route
  (FREE or SUBSCRIPTION); (b) a strong FREE route.
- **Baselines**: single highest-rated planner; best single panel plan by
  structural score.
- **Output**: JSON result per arm under ``$VERDICT_HOME/fusion-bench/``.

Spend rules (enforced; fail closed):
- Panel members and synthesizer option (b): FREE capacity only.
- Synthesizer option (a) and top-planner baseline: FREE or SUBSCRIPTION.
- Never METERED, UNKNOWN, or pay-per-token.
"""

from __future__ import annotations

import json
import textwrap
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from verdict.orchestration.contracts import (
    CapacityClass,
    NodeKind,
    TaskRequirements,
    WorkerTerminal,
    WorkGraph,
    WorkNode,
    route_family,
)

# ------------------------------------------------------------------ constants

SCHEMA_VERSION = "1"

#: FREE capacity only — panel members, synthesizer option (b).
_FREE_ONLY: frozenset[CapacityClass] = frozenset({CapacityClass.FREE})
#: FREE or SUBSCRIPTION — synthesizer option (a), top-planner baseline.
_FREE_OR_SUB: frozenset[CapacityClass] = frozenset({CapacityClass.FREE, CapacityClass.SUBSCRIPTION})

#: Default number of panel members.
DEFAULT_PANEL_SIZE = 3
#: Minimum panel members; fewer than this is BLOCKED.
MIN_PANEL_SIZE = 2
#: Default cap on total LLM calls for the whole bench run.
DEFAULT_MAX_CALLS = 10
#: Default cap on total input + output tokens for the whole bench run.
DEFAULT_MAX_TOKENS = 500_000
#: Timeout per individual planner or synthesizer call (seconds).
DEFAULT_CALL_TIMEOUT_S = 180.0
# Fields written to disk — explicit allowlist keeps internal state off-disk.
RECEIPT_FIELDS: tuple[str, ...] = (
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
)


# ---------------------------------------------------------------- config


@dataclass(frozen=True)
class FusionBenchConfig:
    """Tunable caps and defaults for one bench run."""

    panel_size: int = DEFAULT_PANEL_SIZE
    min_panel_size: int = MIN_PANEL_SIZE
    max_calls: int = DEFAULT_MAX_CALLS
    max_tokens: int = DEFAULT_MAX_TOKENS
    call_timeout_s: float = DEFAULT_CALL_TIMEOUT_S

    def __post_init__(self) -> None:
        if self.panel_size < 1:
            raise ValueError(f"panel_size must be >= 1, got {self.panel_size}")
        if self.min_panel_size < 2:
            raise ValueError(f"min_panel_size must be >= 2, got {self.min_panel_size}")
        if self.min_panel_size > self.panel_size:
            raise ValueError(
                f"min_panel_size ({self.min_panel_size}) > panel_size ({self.panel_size})"
            )
        if self.max_calls < 1:
            raise ValueError(f"max_calls must be >= 1, got {self.max_calls}")
        if self.max_tokens < 1:
            raise ValueError(f"max_tokens must be >= 1, got {self.max_tokens}")


# ---------------------------------------------------------------- result types


@dataclass(frozen=True)
class PanelMember:
    """One panel member's planning result."""

    route_id: str
    capacity_class: str  # CapacityClass.value
    family: str
    duration_s: float
    tokens_in: int | None
    tokens_out: int | None
    plan_valid: bool
    structural_score: float | None
    error: str


@dataclass(frozen=True)
class SynthResult:
    """One synthesizer arm's result."""

    route_id: str
    capacity_class: str
    synth_option: str  # "a" or "b"
    duration_s: float
    tokens_in: int | None
    tokens_out: int | None
    plan_valid: bool
    structural_score: float | None
    error: str


@dataclass(frozen=True)
class BaselineResult:
    """One baseline arm's result."""

    arm: str  # "top_planner" or "best_panel"
    route_id: str | None
    capacity_class: str | None
    plan_valid: bool
    structural_score: float | None
    error: str


@dataclass
class FusionBenchReport:
    """Complete report for one goal across all bench arms."""

    goal_id: str
    goal_digest: str
    panel_members: list[PanelMember] = field(default_factory=list)
    synth_results: list[SynthResult] = field(default_factory=list)
    baselines: list[BaselineResult] = field(default_factory=list)
    total_calls: int = 0
    total_tokens: int = 0
    wall_time_s: float = 0.0
    error: str = ""

    def arm_dict(self, arm: str) -> dict[str, Any]:
        """Serialise one arm row for the JSON output file."""
        if arm.startswith("panel_"):
            idx = int(arm.split("_", 1)[1])
            m = self.panel_members[idx]
            return {
                "schema_version": SCHEMA_VERSION,
                "goal_id": self.goal_id,
                "goal_digest": self.goal_digest,
                "arm": arm,
                "plan_valid": m.plan_valid,
                "structural_score": m.structural_score,
                "routes": [
                    {
                        "route_id": m.route_id,
                        "capacity_class": m.capacity_class,
                        "family": m.family,
                        "tokens_in": m.tokens_in,
                        "tokens_out": m.tokens_out,
                    }
                ],
                "token_usage": {"input": m.tokens_in, "output": m.tokens_out},
                "wall_time_s": m.duration_s,
                "error": m.error,
            }
        if arm.startswith("synth_"):
            opt = arm.split("_", 1)[1]
            matches = [s for s in self.synth_results if s.synth_option == opt]
            if not matches:
                return {
                    "schema_version": SCHEMA_VERSION,
                    "goal_id": self.goal_id,
                    "goal_digest": self.goal_digest,
                    "arm": arm,
                    "plan_valid": False,
                    "structural_score": None,
                    "routes": [],
                    "token_usage": {},
                    "wall_time_s": 0.0,
                    "error": "not_run",
                }
            s = matches[0]
            return {
                "schema_version": SCHEMA_VERSION,
                "goal_id": self.goal_id,
                "goal_digest": self.goal_digest,
                "arm": arm,
                "plan_valid": s.plan_valid,
                "structural_score": s.structural_score,
                "routes": [
                    {
                        "route_id": s.route_id,
                        "capacity_class": s.capacity_class,
                        "tokens_in": s.tokens_in,
                        "tokens_out": s.tokens_out,
                    }
                ],
                "token_usage": {"input": s.tokens_in, "output": s.tokens_out},
                "wall_time_s": s.duration_s,
                "error": s.error,
            }
        # baselines
        matches_b = [b for b in self.baselines if b.arm == arm]
        if not matches_b:
            return {
                "schema_version": SCHEMA_VERSION,
                "goal_id": self.goal_id,
                "goal_digest": self.goal_digest,
                "arm": arm,
                "plan_valid": False,
                "structural_score": None,
                "routes": [],
                "token_usage": {},
                "wall_time_s": 0.0,
                "error": "not_run",
            }
        b = matches_b[0]
        return {
            "schema_version": SCHEMA_VERSION,
            "goal_id": self.goal_id,
            "goal_digest": self.goal_digest,
            "arm": arm,
            "plan_valid": b.plan_valid,
            "structural_score": b.structural_score,
            "routes": [{"route_id": b.route_id, "capacity_class": b.capacity_class}]
            if b.route_id
            else [],
            "token_usage": {},
            "wall_time_s": 0.0,
            "error": b.error,
        }


# ------------------------------------------------------------ structural score


def structural_score(graph: WorkGraph) -> float:
    """Deterministic structural quality score for a WorkGraph (0.0-1.0).

    Four components, weighted sum:
    - ``disjoint`` (0.35): fraction of implement nodes whose owned files
      do not overlap any other implement node's files.
    - ``single_file`` (0.25): fraction of implement nodes with exactly one
      owned file (one owned file per node reduces worker scope).
    - ``verify_quality`` (0.25): fraction of implement nodes with a non-empty
      verification command that references a test file.
    - ``count`` (0.15): ideal 2-6 total nodes; penalised outside that range.
    """
    nodes = graph.nodes
    impl = [n for n in nodes if n.kind is NodeKind.IMPLEMENT]
    if not impl:
        return 0.0

    # 1. File-disjoint: each node may own files no other implement node owns.
    all_owned: list[set[str]] = [set(n.owned_files) for n in impl]
    all_files_flat: list[str] = [f for s in all_owned for f in s]
    duplicates: set[str] = {f for f in all_files_flat if all_files_flat.count(f) > 1}
    disjoint = sum(1 for s in all_owned if not (s & duplicates)) / len(impl)

    # 2. Single-file: nodes with exactly one owned file are well-scoped.
    single_file = sum(1 for n in impl if len(n.owned_files) == 1) / len(impl)

    # 3. Verify quality: verification_command exists and names a test file.
    def _vq(node: WorkNode) -> float:
        if not node.verification_command:
            return 0.0
        cmd_str = " ".join(node.verification_command)
        # Looks like a pytest invocation referencing a test file
        if any("test_" in tok or ".py" in tok for tok in node.verification_command):
            return 1.0
        if cmd_str:
            return 0.5
        return 0.0

    verify_quality = sum(_vq(n) for n in impl) / len(impl)

    # 4. Count: 2-6 total nodes is ideal.
    total = len(nodes)
    count_score = 1.0 if 2 <= total <= 6 else max(0.0, 1.0 - abs(total - 4) / 8.0)

    return round(
        0.35 * disjoint + 0.25 * single_file + 0.25 * verify_quality + 0.15 * count_score, 4
    )


# -------------------------------------------------------- synthesis prompt


def _anonymise_panel_plans(panel_plans: list[WorkGraph]) -> str:
    """Render each panel plan as "Plan N" with its node JSON — no route/model names."""
    parts: list[str] = []
    for i, graph in enumerate(panel_plans, 1):
        nodes_json = json.dumps({"nodes": [n.to_dict() for n in graph.nodes]}, indent=2)
        parts.append(f"=== Plan {i} ===\n{nodes_json}")
    return "\n\n".join(parts)


def build_synthesis_prompt(
    goal: str, repo_map_text: str, constraints: str, panel_plans: list[WorkGraph]
) -> str:
    """Build the synthesis prompt.

    Extends the standard planner prompt with a PANEL PLANS section.
    The panel plans are rendered as "Plan 1", "Plan 2", etc. with no
    route or model names — the synthesizer sees only structure and content.
    """
    from verdict.orchestration.planner import build_planning_prompt

    base = build_planning_prompt(goal, repo_map_text, constraints)
    plans_text = _anonymise_panel_plans(panel_plans)
    panel_section = textwrap.dedent(f"""\
        PANEL PLANS:
        {len(panel_plans)} planning proposals from other models are shown below,
        anonymised as Plan 1, Plan 2, etc. — do not refer to any model by name.

        {plans_text}

        SYNTHESIS TASK:
        Write ONE plan that:
        1. Keeps every agreement found across all panel plans.
        2. Fills in gaps that any single plan caught but others missed.
        3. Resolves conflicts by choosing the more conservative, correct approach.
        4. Satisfies all RULES and the OUTPUT CONTRACT above.

        """)
    # Insert the panel section just before "Return ONLY the JSON object."
    marker = "Return ONLY the JSON object described above."
    if marker in base:
        return base.replace(marker, panel_section + marker)
    # Fallback: append (should not happen with the current planner prompt).
    return base + "\n\n" + panel_section


# ---------------------------------------------------------- token accounting


def _terminal_tokens(terminal: WorkerTerminal) -> tuple[int, int]:
    """Extract (input_tokens, output_tokens) from a terminal; 0 when absent."""
    if terminal.usage is None:
        return 0, 0
    inp = terminal.usage.input_tokens or 0
    out = terminal.usage.output_tokens or 0
    return inp, out


# -------------------------------------------------------- capacity guard


def _capacity_ok(capacity_class: CapacityClass, allowed: frozenset[CapacityClass]) -> bool:
    """Return True when the route's capacity class is in the allowed set."""
    return capacity_class in allowed


# ---------------------------------------------------------- panel selection


async def _run_one_plan(
    selector: Any,
    executor: Any,
    *,
    goal: str,
    repo_map_text: str,
    constraints: str,
    exclude_routes: frozenset[str],
    exclude_families: frozenset[str],
    allowed_capacity: frozenset[CapacityClass],
    now: Callable[[], datetime],
    timeout_s: float,
    run_dir: Path | None,
    phase: str,
    attempt_id: str,
    max_capability_tier: int = 2,
) -> tuple[Any, WorkerTerminal | None, str]:
    """Select one route, verify its capacity class, and run a planning call.

    Returns ``(route_verdict, terminal, error_reason)`` where ``error_reason``
    is empty on success. ``terminal`` is None when selection fails.
    """
    from verdict.orchestration.contracts import AnswerOnlyExecutor
    from verdict.orchestration.controller_operations import bounded_executor_call
    from verdict.orchestration.planner import build_planning_prompt

    requirements = TaskRequirements(
        required_capabilities=frozenset({"tools"}),
        min_context_tokens=100_000,
        coding=True,
        reasoning=False,
        frontier_worthy=False,
        role_ordering="strongest_first",
        exclude_routes=exclude_routes,
        exclude_families=exclude_families,
        max_capability_tier=max_capability_tier,
    )
    choice, _considered = selector.select(requirements, now=now())
    if choice is None:
        return None, None, "no_admitted_route"

    cap = getattr(choice, "capacity_class", CapacityClass.UNKNOWN)
    if not _capacity_ok(cap, allowed_capacity):
        return choice, None, f"capacity_blocked:{cap.value}"

    prompt = build_planning_prompt(goal, repo_map_text, constraints)
    terminal = await bounded_executor_call(
        AnswerOnlyExecutor(executor),
        prompt,
        route_id=choice.route_id,
        cwd=Path("."),
        timeout_seconds=timeout_s,
        run_dir=run_dir,
        phase=phase,
        attempt_id=attempt_id,
    )
    return choice, terminal, ""


# ------------------------------------------------------ main bench function


async def run_fusion_plan_bench(
    *,
    goal: str,
    goal_id: str,
    constraints: str = "",
    repo_map_text: str = "",
    selector: Any,
    executor: Any,
    config: FusionBenchConfig | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    run_dir: Path | None = None,
) -> FusionBenchReport:
    """Run the full fusion planning benchmark for one goal.

    Args:
        goal: The raw goal text.
        goal_id: Short stable identifier for the goal (used in output filenames).
        constraints: Optional constraints text (passed to the planner prompt).
        repo_map_text: Repo map text (may be empty for the bench fixture).
        selector: A ``ModelSelector``-protocol object (``select``, ``record_success``).
        executor: A ``WorkerExecutor``-protocol object.
        config: Benchmark configuration; uses defaults when None.
        now: Clock function for selection timestamps.
        run_dir: Optional directory for diagnostic files.
    """
    import hashlib

    from verdict.orchestration.contracts import OrchestrationError
    from verdict.orchestration.planner import parse_plan

    cfg = config or FusionBenchConfig()

    goal_digest = hashlib.sha256(goal.encode("utf-8")).hexdigest()[:12]
    report = FusionBenchReport(goal_id=goal_id, goal_digest=goal_digest)
    t_start = time.monotonic()

    call_count = 0
    token_count = 0

    def _cap_check() -> bool:
        """Return True when the call + token caps still have headroom."""
        return call_count < cfg.max_calls and token_count < cfg.max_tokens

    # -------------------------------------------------- panel
    tried: set[str] = set()
    families_seen: set[str] = set()
    panel_valid_plans: list[WorkGraph] = []

    for panel_slot in range(cfg.panel_size):
        if not _cap_check():
            report.panel_members.append(
                PanelMember(
                    route_id="",
                    capacity_class="",
                    family="",
                    duration_s=0.0,
                    tokens_in=None,
                    tokens_out=None,
                    plan_valid=False,
                    structural_score=None,
                    error="cap_exceeded",
                )
            )
            continue

        t0 = time.monotonic()
        choice, terminal, err = await _run_one_plan(
            selector,
            executor,
            goal=goal,
            repo_map_text=repo_map_text,
            constraints=constraints,
            exclude_routes=frozenset(tried),
            exclude_families=frozenset(families_seen),
            allowed_capacity=_FREE_ONLY,
            now=now,
            timeout_s=cfg.call_timeout_s,
            run_dir=run_dir,
            phase=f"panel_{panel_slot}",
            attempt_id=f"panel-{panel_slot}",
        )
        dur = time.monotonic() - t0

        if choice is None or err or terminal is None:
            report.panel_members.append(
                PanelMember(
                    route_id=getattr(choice, "route_id", "") or "",
                    capacity_class=getattr(getattr(choice, "capacity_class", None), "value", "")
                    or "",
                    family="",
                    duration_s=dur,
                    tokens_in=None,
                    tokens_out=None,
                    plan_valid=False,
                    structural_score=None,
                    error=err or "no_route",
                )
            )
            continue

        call_count += 1
        ti, to = _terminal_tokens(terminal)
        token_count += ti + to

        route_id = choice.route_id
        cap_val = getattr(choice.capacity_class, "value", str(choice.capacity_class))
        family = route_family(route_id)
        tried.add(route_id)
        families_seen.add(family)

        if not terminal.ok:
            report.panel_members.append(
                PanelMember(
                    route_id=route_id,
                    capacity_class=cap_val,
                    family=family,
                    duration_s=dur,
                    tokens_in=ti,
                    tokens_out=to,
                    plan_valid=False,
                    structural_score=None,
                    error=terminal.error or "executor_failed",
                )
            )
            continue

        try:
            graph = parse_plan(terminal.output, goal)
            score = structural_score(graph)
            panel_valid_plans.append(graph)
            report.panel_members.append(
                PanelMember(
                    route_id=route_id,
                    capacity_class=cap_val,
                    family=family,
                    duration_s=dur,
                    tokens_in=ti,
                    tokens_out=to,
                    plan_valid=True,
                    structural_score=score,
                    error="",
                )
            )
        except OrchestrationError as exc:
            report.panel_members.append(
                PanelMember(
                    route_id=route_id,
                    capacity_class=cap_val,
                    family=family,
                    duration_s=dur,
                    tokens_in=ti,
                    tokens_out=to,
                    plan_valid=False,
                    structural_score=None,
                    error=f"parse_failed:{exc}",
                )
            )

    # Abort if below minimum panel size.
    valid_panel_count = sum(1 for m in report.panel_members if m.plan_valid)
    if valid_panel_count < cfg.min_panel_size:
        report.error = f"panel_too_small:{valid_panel_count}<{cfg.min_panel_size}"
        report.total_calls = call_count
        report.total_tokens = token_count
        report.wall_time_s = time.monotonic() - t_start
        return report

    # -------------------------------------------------- synthesis (option b: FREE)
    if _cap_check() and panel_valid_plans:
        t0 = time.monotonic()
        synth_tried: set[str] = set(tried)  # exclude panel routes

        synth_requirements = TaskRequirements(
            required_capabilities=frozenset({"tools"}),
            min_context_tokens=100_000,
            coding=True,
            reasoning=False,
            frontier_worthy=False,
            role_ordering="strongest_first",
            exclude_routes=frozenset(synth_tried),
            max_capability_tier=1,
        )
        synth_b_choice, _c = selector.select(synth_requirements, now=now())
        dur = time.monotonic() - t0

        if synth_b_choice is None:
            report.synth_results.append(
                SynthResult(
                    route_id="",
                    capacity_class="",
                    synth_option="b",
                    duration_s=dur,
                    tokens_in=None,
                    tokens_out=None,
                    plan_valid=False,
                    structural_score=None,
                    error="no_route",
                )
            )
        else:
            cap = getattr(synth_b_choice, "capacity_class", CapacityClass.UNKNOWN)
            cap_val = getattr(cap, "value", str(cap))
            route_id = synth_b_choice.route_id

            if not _capacity_ok(cap, _FREE_ONLY):
                report.synth_results.append(
                    SynthResult(
                        route_id=route_id,
                        capacity_class=cap_val,
                        synth_option="b",
                        duration_s=dur,
                        tokens_in=None,
                        tokens_out=None,
                        plan_valid=False,
                        structural_score=None,
                        error=f"capacity_blocked:{cap_val}",
                    )
                )
            elif _cap_check():
                from verdict.orchestration.contracts import AnswerOnlyExecutor, OrchestrationError
                from verdict.orchestration.controller_operations import bounded_executor_call
                from verdict.orchestration.planner import parse_plan

                t0 = time.monotonic()
                synth_prompt = build_synthesis_prompt(
                    goal, repo_map_text, constraints, panel_valid_plans
                )
                synth_terminal = await bounded_executor_call(
                    AnswerOnlyExecutor(executor),
                    synth_prompt,
                    route_id=route_id,
                    cwd=Path("."),
                    timeout_seconds=cfg.call_timeout_s,
                    run_dir=run_dir,
                    phase="synth_b",
                    attempt_id="synth-b",
                )
                dur = time.monotonic() - t0
                call_count += 1
                ti, to = _terminal_tokens(synth_terminal)
                token_count += ti + to

                if not synth_terminal.ok:
                    report.synth_results.append(
                        SynthResult(
                            route_id=route_id,
                            capacity_class=cap_val,
                            synth_option="b",
                            duration_s=dur,
                            tokens_in=ti,
                            tokens_out=to,
                            plan_valid=False,
                            structural_score=None,
                            error=synth_terminal.error or "executor_failed",
                        )
                    )
                else:
                    try:
                        synth_graph = parse_plan(synth_terminal.output, goal)
                        score = structural_score(synth_graph)
                        report.synth_results.append(
                            SynthResult(
                                route_id=route_id,
                                capacity_class=cap_val,
                                synth_option="b",
                                duration_s=dur,
                                tokens_in=ti,
                                tokens_out=to,
                                plan_valid=True,
                                structural_score=score,
                                error="",
                            )
                        )
                    except OrchestrationError as exc:
                        # Invalid synthesized plan is recorded as invalid; never silently replaced.
                        report.synth_results.append(
                            SynthResult(
                                route_id=route_id,
                                capacity_class=cap_val,
                                synth_option="b",
                                duration_s=dur,
                                tokens_in=ti,
                                tokens_out=to,
                                plan_valid=False,
                                structural_score=None,
                                error=f"parse_failed:{exc}",
                            )
                        )
            else:
                report.synth_results.append(
                    SynthResult(
                        route_id=route_id,
                        capacity_class=cap_val,
                        synth_option="b",
                        duration_s=dur,
                        tokens_in=None,
                        tokens_out=None,
                        plan_valid=False,
                        structural_score=None,
                        error="cap_exceeded",
                    )
                )

    # -------------------------------------------------- synthesis (option a: highest-rated planner)
    # Option (a) may use FREE or SUBSCRIPTION. It is benched using the same
    # synthesizer prompt as option (b). The panel routes are excluded.
    if _cap_check() and panel_valid_plans:
        from verdict.orchestration.contracts import AnswerOnlyExecutor, OrchestrationError
        from verdict.orchestration.controller_operations import bounded_executor_call
        from verdict.orchestration.planner import parse_plan

        synth_a_requirements = TaskRequirements(
            required_capabilities=frozenset({"tools"}),
            min_context_tokens=100_000,
            coding=True,
            reasoning=True,
            frontier_worthy=True,
            role_ordering="strongest_first",
            exclude_routes=frozenset(tried),
            max_capability_tier=1,
        )
        synth_a_choice, _c = selector.select(synth_a_requirements, now=now())

        if synth_a_choice is None:
            report.synth_results.append(
                SynthResult(
                    route_id="",
                    capacity_class="",
                    synth_option="a",
                    duration_s=0.0,
                    tokens_in=None,
                    tokens_out=None,
                    plan_valid=False,
                    structural_score=None,
                    error="no_route",
                )
            )
        else:
            cap = getattr(synth_a_choice, "capacity_class", CapacityClass.UNKNOWN)
            cap_val = getattr(cap, "value", str(cap))
            route_id = synth_a_choice.route_id

            if not _capacity_ok(cap, _FREE_OR_SUB):
                report.synth_results.append(
                    SynthResult(
                        route_id=route_id,
                        capacity_class=cap_val,
                        synth_option="a",
                        duration_s=0.0,
                        tokens_in=None,
                        tokens_out=None,
                        plan_valid=False,
                        structural_score=None,
                        error=f"capacity_blocked:{cap_val}",
                    )
                )
            else:
                t0 = time.monotonic()
                synth_prompt = build_synthesis_prompt(
                    goal, repo_map_text, constraints, panel_valid_plans
                )
                synth_terminal = await bounded_executor_call(
                    AnswerOnlyExecutor(executor),
                    synth_prompt,
                    route_id=route_id,
                    cwd=Path("."),
                    timeout_seconds=cfg.call_timeout_s,
                    run_dir=run_dir,
                    phase="synth_a",
                    attempt_id="synth-a",
                )
                dur = time.monotonic() - t0
                call_count += 1
                ti, to = _terminal_tokens(synth_terminal)
                token_count += ti + to

                if not synth_terminal.ok:
                    report.synth_results.append(
                        SynthResult(
                            route_id=route_id,
                            capacity_class=cap_val,
                            synth_option="a",
                            duration_s=dur,
                            tokens_in=ti,
                            tokens_out=to,
                            plan_valid=False,
                            structural_score=None,
                            error=synth_terminal.error or "executor_failed",
                        )
                    )
                else:
                    try:
                        synth_graph = parse_plan(synth_terminal.output, goal)
                        score = structural_score(synth_graph)
                        report.synth_results.append(
                            SynthResult(
                                route_id=route_id,
                                capacity_class=cap_val,
                                synth_option="a",
                                duration_s=dur,
                                tokens_in=ti,
                                tokens_out=to,
                                plan_valid=True,
                                structural_score=score,
                                error="",
                            )
                        )
                    except OrchestrationError as exc:
                        report.synth_results.append(
                            SynthResult(
                                route_id=route_id,
                                capacity_class=cap_val,
                                synth_option="a",
                                duration_s=dur,
                                tokens_in=ti,
                                tokens_out=to,
                                plan_valid=False,
                                structural_score=None,
                                error=f"parse_failed:{exc}",
                            )
                        )

    # -------------------------------------------------- baseline: top planner
    # Top planner uses strongest_first with FREE|SUBSCRIPTION.
    if _cap_check():
        from verdict.orchestration.contracts import AnswerOnlyExecutor, OrchestrationError
        from verdict.orchestration.controller_operations import bounded_executor_call
        from verdict.orchestration.planner import build_planning_prompt, parse_plan

        top_requirements = TaskRequirements(
            required_capabilities=frozenset({"tools"}),
            min_context_tokens=100_000,
            coding=True,
            reasoning=True,
            frontier_worthy=True,
            role_ordering="strongest_first",
            max_capability_tier=1,
        )
        top_choice, _c = selector.select(top_requirements, now=now())

        if top_choice is None:
            report.baselines.append(
                BaselineResult(
                    arm="top_planner",
                    route_id=None,
                    capacity_class=None,
                    plan_valid=False,
                    structural_score=None,
                    error="no_route",
                )
            )
        else:
            cap = getattr(top_choice, "capacity_class", CapacityClass.UNKNOWN)
            cap_val = getattr(cap, "value", str(cap))
            route_id = top_choice.route_id

            if not _capacity_ok(cap, _FREE_OR_SUB):
                report.baselines.append(
                    BaselineResult(
                        arm="top_planner",
                        route_id=route_id,
                        capacity_class=cap_val,
                        plan_valid=False,
                        structural_score=None,
                        error=f"capacity_blocked:{cap_val}",
                    )
                )
            else:
                t0 = time.monotonic()
                prompt = build_planning_prompt(goal, repo_map_text, constraints)
                top_terminal = await bounded_executor_call(
                    AnswerOnlyExecutor(executor),
                    prompt,
                    route_id=route_id,
                    cwd=Path("."),
                    timeout_seconds=cfg.call_timeout_s,
                    run_dir=run_dir,
                    phase="baseline_top",
                    attempt_id="baseline-top",
                )
                call_count += 1
                ti, to = _terminal_tokens(top_terminal)
                token_count += ti + to

                if not top_terminal.ok:
                    report.baselines.append(
                        BaselineResult(
                            arm="top_planner",
                            route_id=route_id,
                            capacity_class=cap_val,
                            plan_valid=False,
                            structural_score=None,
                            error=top_terminal.error or "executor_failed",
                        )
                    )
                else:
                    try:
                        top_graph = parse_plan(top_terminal.output, goal)
                        score = structural_score(top_graph)
                        report.baselines.append(
                            BaselineResult(
                                arm="top_planner",
                                route_id=route_id,
                                capacity_class=cap_val,
                                plan_valid=True,
                                structural_score=score,
                                error="",
                            )
                        )
                    except OrchestrationError as exc:
                        report.baselines.append(
                            BaselineResult(
                                arm="top_planner",
                                route_id=route_id,
                                capacity_class=cap_val,
                                plan_valid=False,
                                structural_score=None,
                                error=f"parse_failed:{exc}",
                            )
                        )

    # -------------------------------------------------- baseline: best panel plan
    valid_members = [
        m for m in report.panel_members if m.plan_valid and m.structural_score is not None
    ]
    if valid_members:
        best = max(valid_members, key=lambda m: m.structural_score or 0.0)
        report.baselines.append(
            BaselineResult(
                arm="best_panel",
                route_id=best.route_id,
                capacity_class=best.capacity_class,
                plan_valid=True,
                structural_score=best.structural_score,
                error="",
            )
        )
    else:
        report.baselines.append(
            BaselineResult(
                arm="best_panel",
                route_id=None,
                capacity_class=None,
                plan_valid=False,
                structural_score=None,
                error="no_valid_panel_plan",
            )
        )

    report.total_calls = call_count
    report.total_tokens = token_count
    report.wall_time_s = time.monotonic() - t_start
    return report


# --------------------------------------------------- summary table helpers


def summary_table(reports: list[FusionBenchReport]) -> str:
    """Render a plain-text summary table across all goals and arms."""
    if not reports:
        return "(no results)"
    rows: list[tuple[str, str, str, str, str]] = []
    headers = ("goal_id", "arm", "valid", "score", "routes")
    rows.append(headers)
    for rep in reports:
        for m in rep.panel_members:
            rows.append(
                (
                    rep.goal_id,
                    f"panel_{rep.panel_members.index(m)}",
                    "yes" if m.plan_valid else "no",
                    f"{m.structural_score:.4f}" if m.structural_score is not None else "-",
                    m.route_id or "-",
                )
            )
        for s in rep.synth_results:
            rows.append(
                (
                    rep.goal_id,
                    f"synth_{s.synth_option}",
                    "yes" if s.plan_valid else "no",
                    f"{s.structural_score:.4f}" if s.structural_score is not None else "-",
                    s.route_id or "-",
                )
            )
        for b in rep.baselines:
            rows.append(
                (
                    rep.goal_id,
                    b.arm,
                    "yes" if b.plan_valid else "no",
                    f"{b.structural_score:.4f}" if b.structural_score is not None else "-",
                    b.route_id or "-",
                )
            )
    widths = [max(len(r[i]) for r in rows) for i in range(5)]
    sep = "  ".join("-" * w for w in widths)
    lines: list[str] = []
    for i, row in enumerate(rows):
        lines.append("  ".join(cell.ljust(widths[j]) for j, cell in enumerate(row)))
        if i == 0:
            lines.append(sep)
    return "\n".join(lines)
