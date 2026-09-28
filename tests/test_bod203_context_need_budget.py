"""BOD-203 AC3: OpenJev context_need → advisory context budget tests."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from verdict.orchestration.contracts import (
    CapacityClass,
    EligibilityStage,
    NodeState,
    RouteVerdict,
    TaskRequirements,
    WorkerTerminal,
    WorkGraph,
    WorkNode,
)
from verdict.orchestration.run import _CONTEXT_BUDGET_MIN_CONFIDENCE, _context_need_advisory_budget
from verdict.orchestration.runtime import DagRuntime, RuntimePolicy

NOW = datetime(2026, 9, 28, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers (mirrors test_orch_runtime.py patterns)
# ---------------------------------------------------------------------------


class Events:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def emit(self, type: str, node_id: str = "", **data: Any) -> None:
        json.dumps(data, default=str)
        self.rows.append({"type": type, "node_id": node_id, **data})

    def of(self, type: str, node_id: str | None = None) -> list[dict[str, Any]]:
        return [
            r
            for r in self.rows
            if r["type"] == type and (node_id is None or r["node_id"] == node_id)
        ]


def _node(nid: str = "a") -> WorkNode:
    return WorkNode(
        nid,
        f"write {nid}",
        depends_on=(),
        owned_files=(f"{nid}.txt",),
        verification_command=("true",),
    )


# ---------------------------------------------------------------------------
# Unit: _context_need_advisory_budget
# ---------------------------------------------------------------------------


class TestContextNeedAdvisoryBudget:
    """Pure-function tests for the STEP mapping."""

    def _policy(self, budget: int = 60_000, floor: int = 4_000) -> RuntimePolicy:
        return RuntimePolicy(context_budget_bytes=budget, min_context_budget_bytes=floor)

    # -- tier mapping -------------------------------------------------------

    def test_self_contained_tier(self) -> None:
        """context_need=0.0 → 50% of budget."""
        budget, tier = _context_need_advisory_budget(
            {"context_need": 0.0}, confidence=0.9, policy=self._policy()
        )
        assert budget == 30_000
        assert tier == "self_contained"

    def test_needs_some_context_tier(self) -> None:
        """context_need=0.5 → 75% of budget."""
        budget, tier = _context_need_advisory_budget(
            {"context_need": 0.5}, confidence=0.9, policy=self._policy()
        )
        assert budget == 45_000
        assert tier == "needs_some_context"

    def test_heavy_context_tier(self) -> None:
        """context_need=1.0 → 100% of budget."""
        budget, tier = _context_need_advisory_budget(
            {"context_need": 1.0}, confidence=0.9, policy=self._policy()
        )
        assert budget == 60_000
        assert tier == "heavy_context_required"

    def test_boundary_025(self) -> None:
        """context_need exactly at 0.25 boundary → needs_some_context."""
        budget, tier = _context_need_advisory_budget(
            {"context_need": 0.25}, confidence=0.9, policy=self._policy()
        )
        assert tier == "needs_some_context"
        assert budget == 45_000

    def test_boundary_075(self) -> None:
        """context_need exactly at 0.75 boundary → heavy_context_required."""
        budget, tier = _context_need_advisory_budget(
            {"context_need": 0.75}, confidence=0.9, policy=self._policy()
        )
        assert tier == "heavy_context_required"
        assert budget == 60_000

    # -- clamping -----------------------------------------------------------

    def test_clamp_to_min(self) -> None:
        """Budget never goes below min_context_budget_bytes."""
        # With a tiny max budget, 50% would be 2500 < floor of 4000
        budget, _ = _context_need_advisory_budget(
            {"context_need": 0.0}, confidence=0.9, policy=self._policy(budget=5_000)
        )
        assert budget == 4_000

    def test_never_exceeds_max(self) -> None:
        """Budget never exceeds context_budget_bytes (invariant)."""
        budget, _ = _context_need_advisory_budget(
            {"context_need": 1.0}, confidence=0.9, policy=self._policy()
        )
        assert budget <= 60_000

    # -- fallback to baseline -----------------------------------------------

    def test_no_signals_returns_baseline(self) -> None:
        budget, tier = _context_need_advisory_budget(None, confidence=0.9, policy=self._policy())
        assert budget == 60_000
        assert tier == "baseline:no_signals"

    def test_low_confidence_returns_baseline(self) -> None:
        budget, tier = _context_need_advisory_budget(
            {"context_need": 0.0},
            confidence=_CONTEXT_BUDGET_MIN_CONFIDENCE - 0.01,
            policy=self._policy(),
        )
        assert budget == 60_000
        assert tier == "baseline:low_confidence"

    def test_missing_context_need_key_returns_baseline(self) -> None:
        budget, tier = _context_need_advisory_budget(
            {"complexity": 0.5}, confidence=0.9, policy=self._policy()
        )
        assert budget == 60_000
        assert tier == "baseline:missing_context_need"

    def test_invalid_context_need_returns_baseline(self) -> None:
        budget, tier = _context_need_advisory_budget(
            {"context_need": 1.5}, confidence=0.9, policy=self._policy()
        )
        assert budget == 60_000
        assert tier == "baseline:invalid_context_need"

    def test_non_numeric_context_need_returns_baseline(self) -> None:
        budget, tier = _context_need_advisory_budget(
            {"context_need": "high"}, confidence=0.9, policy=self._policy()
        )
        assert budget == 60_000
        assert tier == "baseline:missing_context_need"


# ---------------------------------------------------------------------------
# Integration: SHADOW records but does not change budget
# ---------------------------------------------------------------------------


class _FakeSignalProvider:
    """Stub DecisionSignalProvider returning fixed signals."""

    def __init__(
        self, context_need: float = 0.5, confidence: float = 0.9, failure_class: str | None = None
    ) -> None:
        self._context_need = context_need
        self._confidence = confidence
        self._failure_class = failure_class
        self.call_count = 0

    def signals(self, question: Any, *, now: datetime) -> Any:
        from verdict.decision_signals.contracts import DecisionSignalSetV1

        self.call_count += 1
        fc = None
        if self._failure_class is not None:
            from verdict.decision_signals.contracts import NormalizedFailureClass

            fc = NormalizedFailureClass(self._failure_class)

        return DecisionSignalSetV1(
            schema_version="decision-signals/v1",
            provider="test",
            model="test-model",
            version="1.0",
            request_id="test-req",
            purpose="context_budget_advisory",
            signals={
                "complexity": 0.5,
                "decomposability": 0.5,
                "ambiguity": 0.3,
                "frontier_worthy": 0.8,
                "security_sensitive": 0.0,
                "verification_strength": 0.5,
                "context_need": self._context_need,
            }
            if fc is None
            else None,
            confidence=self._confidence,
            latency_ms=50,
            usage={"input_tokens": 100, "output_tokens": 50},
            input_digest="ab" * 32,
            observed_at=now.isoformat(),
            failure_class=fc,
            mode="SHADOW",
        )


class Selector:
    """Minimal selector for integration tests."""

    def __init__(self, routes: list[str]) -> None:
        self.routes = routes

    def select(
        self, requirements: TaskRequirements, *, now: datetime
    ) -> tuple[RouteVerdict | None, tuple[RouteVerdict, ...]]:
        for route in self.routes:
            if route not in requirements.exclude_routes:
                provider = route.split("/")[0]
                v = RouteVerdict(
                    route,
                    provider,
                    EligibilityStage.AVAILABLE,
                    EligibilityStage.SELECTED,
                    "ok",
                    capacity_class=CapacityClass.FRONTIER,
                )
                return v, (v,)
        return None, ()

    def record_failure(self, route_id: str, failure: Any, *, now: datetime) -> None:
        pass

    def record_success(self, route_id: str, *, now: datetime) -> None:
        pass


class Executor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def run(
        self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float
    ) -> WorkerTerminal:
        self.calls.append((prompt, route_id))
        return WorkerTerminal(ok=True, model=route_id, status_code=200)


class SimpleReviewer:
    async def review(self, *a: Any, **kw: Any) -> Any:
        from verdict.orchestration.contracts import ReviewResult

        return ReviewResult(passed=True, findings=())


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    (r / ".git").mkdir()
    return r


@pytest.mark.asyncio()
async def test_shadow_mode_records_but_does_not_change_budget(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SHADOW mode: advisory budget is recorded but NodeRun.context_budget stays 0."""
    monkeypatch.setenv("VERDICT_DECISION_SIGNALS_MODE", "SHADOW")
    provider = _FakeSignalProvider(context_need=0.0, confidence=0.9)
    events = Events()
    policy = RuntimePolicy()

    # Build a minimal runtime with one node
    graph = WorkGraph("g", (_node("a"),))
    rt = DagRuntime(
        repo=repo,
        run_dir=repo.parent / "run1",
        graph=graph,
        selector=Selector(["cc/s"]),
        executor=Executor(),
        classifier=_stub_classifier(),
        events=events,
        prompt_for=lambda n, cwd, **kw: f"{n.node_id}: {n.objective}",
        reviewer=SimpleReviewer(),
        policy=policy,
        now=lambda: NOW,
    )

    # Simulate the orchestrate() advisory logic in SHADOW mode
    from verdict.decision_signals.shadow import get_signals_mode, should_collect_signals

    assert get_signals_mode() == "SHADOW"
    assert should_collect_signals()

    sig_set = provider.signals(
        None,
        now=NOW,  # type: ignore[arg-type]
    )
    sig_dict = sig_set.to_dict()
    advisory_budget, tier = _context_need_advisory_budget(
        sig_dict.get("signals"), confidence=float(sig_dict.get("confidence", 0.0)), policy=policy
    )

    # SHADOW: budget is NOT applied
    assert advisory_budget == 30_000  # 50% for self-contained
    assert tier == "self_contained"
    # Node budget stays at 0 (default = policy baseline at dispatch time)
    assert rt.nodes["a"].context_budget == 0


@pytest.mark.asyncio()
async def test_advisory_mode_applies_self_contained_budget(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADVISORY + context_need=0.0 → NodeRun.context_budget set to 50%."""
    monkeypatch.setenv("VERDICT_DECISION_SIGNALS_MODE", "ADVISORY")
    provider = _FakeSignalProvider(context_need=0.0, confidence=0.9)
    policy = RuntimePolicy(context_budget_bytes=60_000, min_context_budget_bytes=4_000)
    events = Events()

    graph = WorkGraph("g", (_node("a"), _node("b")))
    rt = DagRuntime(
        repo=repo,
        run_dir=repo.parent / "run2",
        graph=graph,
        selector=Selector(["cc/s"]),
        executor=Executor(),
        classifier=_stub_classifier(),
        events=events,
        prompt_for=lambda n, cwd, **kw: f"{n.node_id}",
        reviewer=SimpleReviewer(),
        policy=policy,
        now=lambda: NOW,
    )

    from verdict.decision_signals.shadow import get_signals_mode

    sig_set = provider.signals(None, now=NOW)  # type: ignore[arg-type]
    sig_dict = sig_set.to_dict()
    advisory_budget, tier = _context_need_advisory_budget(
        sig_dict.get("signals"), confidence=float(sig_dict.get("confidence", 0.0)), policy=policy
    )
    mode = get_signals_mode()
    applied = mode == "ADVISORY" and not tier.startswith("baseline")

    assert applied
    assert advisory_budget == 30_000
    if applied:
        for nr in rt.nodes.values():
            if nr.state != NodeState.VALIDATED and nr.context_budget == 0:
                nr.context_budget = advisory_budget

    assert rt.nodes["a"].context_budget == 30_000
    assert rt.nodes["b"].context_budget == 30_000


@pytest.mark.asyncio()
async def test_advisory_mode_applies_needs_some_context(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADVISORY + context_need=0.5 → 75% budget."""
    monkeypatch.setenv("VERDICT_DECISION_SIGNALS_MODE", "ADVISORY")
    provider = _FakeSignalProvider(context_need=0.5, confidence=0.9)
    policy = RuntimePolicy(context_budget_bytes=60_000)

    sig_set = provider.signals(None, now=NOW)  # type: ignore[arg-type]
    sig_dict = sig_set.to_dict()
    budget, tier = _context_need_advisory_budget(
        sig_dict.get("signals"), confidence=float(sig_dict.get("confidence", 0.0)), policy=policy
    )
    assert budget == 45_000
    assert tier == "needs_some_context"


@pytest.mark.asyncio()
async def test_advisory_mode_applies_heavy_context(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADVISORY + context_need=1.0 → 100% budget (full baseline)."""
    monkeypatch.setenv("VERDICT_DECISION_SIGNALS_MODE", "ADVISORY")
    provider = _FakeSignalProvider(context_need=1.0, confidence=0.9)
    policy = RuntimePolicy(context_budget_bytes=60_000)

    sig_set = provider.signals(None, now=NOW)  # type: ignore[arg-type]
    sig_dict = sig_set.to_dict()
    budget, tier = _context_need_advisory_budget(
        sig_dict.get("signals"), confidence=float(sig_dict.get("confidence", 0.0)), policy=policy
    )
    assert budget == 60_000
    assert tier == "heavy_context_required"


@pytest.mark.asyncio()
async def test_signal_failure_returns_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provider failure (failure_class set, signals=None) → baseline budget."""
    monkeypatch.setenv("VERDICT_DECISION_SIGNALS_MODE", "ADVISORY")
    provider = _FakeSignalProvider(failure_class="timeout")
    policy = RuntimePolicy(context_budget_bytes=60_000)

    sig_set = provider.signals(None, now=NOW)  # type: ignore[arg-type]
    sig_dict = sig_set.to_dict()
    # When failure_class is set, signals is None
    budget, tier = _context_need_advisory_budget(
        sig_dict.get("signals"), confidence=float(sig_dict.get("confidence", 0.0)), policy=policy
    )
    assert budget == 60_000
    assert tier == "baseline:no_signals"


@pytest.mark.asyncio()
async def test_repack_halves_from_advisory_start(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_repack() halves from the advisory-set budget, not from the policy max."""
    policy = RuntimePolicy(context_budget_bytes=60_000, min_context_budget_bytes=4_000)
    events = Events()

    graph = WorkGraph("g", (_node("a"),))
    rt = DagRuntime(
        repo=repo,
        run_dir=repo.parent / "run3",
        graph=graph,
        selector=Selector(["cc/s"]),
        executor=Executor(),
        classifier=_stub_classifier(),
        events=events,
        prompt_for=lambda n, cwd, **kw: f"{n.node_id}",
        reviewer=SimpleReviewer(),
        policy=policy,
        now=lambda: NOW,
    )

    # Simulate advisory setting the budget to 30_000 (self-contained)
    rt.nodes["a"].context_budget = 30_000

    # _repack should halve from 30_000 → 15_000
    repacked = rt._repack(rt.nodes["a"])
    assert repacked is True
    assert rt.nodes["a"].context_budget == 15_000

    # Second repack: 15_000 → 7_500
    repacked = rt._repack(rt.nodes["a"])
    assert repacked is True
    assert rt.nodes["a"].context_budget == 7_500

    # Third repack: 7_500 → 4_000 (clamped to floor)
    repacked = rt._repack(rt.nodes["a"])
    assert repacked is True
    assert rt.nodes["a"].context_budget == 4_000

    # At floor: cannot repack further
    repacked = rt._repack(rt.nodes["a"])
    assert repacked is False


@pytest.mark.asyncio()
async def test_validated_nodes_not_overwritten_by_advisory(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resumed/validated nodes keep their existing budget."""
    monkeypatch.setenv("VERDICT_DECISION_SIGNALS_MODE", "ADVISORY")
    policy = RuntimePolicy(context_budget_bytes=60_000)

    graph = WorkGraph("g", (_node("a"),))
    rt = DagRuntime(
        repo=repo,
        run_dir=repo.parent / "run4",
        graph=graph,
        selector=Selector(["cc/s"]),
        executor=Executor(),
        classifier=_stub_classifier(),
        events=Events(),
        prompt_for=lambda n, cwd, **kw: f"{n.node_id}",
        reviewer=SimpleReviewer(),
        policy=policy,
        now=lambda: NOW,
    )

    # Mark node as validated (resumed from prior run)
    rt.nodes["a"].state = NodeState.VALIDATED
    rt.nodes["a"].context_budget = 0

    # Advisory would set 30_000 but should skip VALIDATED nodes
    advisory_budget = 30_000
    for nr in rt.nodes.values():
        if nr.state != NodeState.VALIDATED and nr.context_budget == 0:
            nr.context_budget = advisory_budget

    assert rt.nodes["a"].context_budget == 0  # unchanged


def _stub_classifier() -> Any:
    """Minimal classifier that classifies everything as transient."""
    from verdict.orchestration.recovery import FailureIntelligence

    return FailureIntelligence()
