"""Integration tests for BOD-199 SHADOW decision signals."""

import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from verdict.decision_signals.contracts import DecisionSignalSetV1
from verdict.orchestration.contracts import (
    CapacityClass,
    EligibilityStage,
    FailureClassification,
    RouteVerdict,
    TaskRequirements,
    WorkerTerminal,
)
from verdict.orchestration.receipt import EventLog, build_run_receipt
from verdict.orchestration.run import plan_with_failover

_VALID_NODES_JSON = """
{"nodes": [
  {"node_id": "impl-a", "objective": "implement a", "kind": "implement",
   "owned_files": ["verdict/a.py"], "verification_command": ["pytest", "-q", "tests/test_a.py"]},
  {"node_id": "impl-b", "objective": "implement b", "kind": "implement",
   "owned_files": ["verdict/b.py"], "verification_command": ["pytest", "-q", "tests/test_b.py"]},
  {"node_id": "int", "objective": "integrate", "kind": "integrate",
   "depends_on": ["impl-a", "impl-b"], "verification_command": ["pytest", "-q"]}
]}
"""


def _init_git_repo(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=tmp_path, check=True)
    (tmp_path / "verdict").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "other").mkdir()
    (tmp_path / "verdict" / "mod.py").write_text("line1\nline2\nline3\n")
    (tmp_path / "tests" / "test_mod.py").write_text("line1\n")
    (tmp_path / "other" / "ignored.py").write_text("x\n")
    (tmp_path / "README.md").write_text("hi\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    return tmp_path


class _ScriptedExecutor:
    """Fake WorkerExecutor returning scripted terminals in sequence."""

    def __init__(self, outputs: list[WorkerTerminal]) -> None:
        self._outputs = list(outputs)
        self.calls: list[str] = []

    async def run(
        self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float
    ) -> WorkerTerminal:
        self.calls.append(prompt)
        if not self._outputs:
            raise AssertionError("no more scripted outputs")
        return self._outputs.pop(0)


class _RecordingSelector:
    """Selector that records every call to select() and returns a fixed route."""

    def __init__(self, route_id: str = "test/model") -> None:
        self.route_id = route_id
        self.recorded_requirements: list[TaskRequirements] = []

    def evaluate(self, r: TaskRequirements, *, now: datetime) -> tuple[RouteVerdict, ...]:
        return ()

    def select(
        self, r: TaskRequirements, *, now: datetime
    ) -> tuple[RouteVerdict | None, tuple[RouteVerdict, ...]]:
        self.recorded_requirements.append(r)
        v = RouteVerdict(
            self.route_id,
            "test-provider",
            EligibilityStage.SELECTED,
            None,
            "ok",
            CapacityClass.FREE,
            rank=0,
        )
        return v, (v,)

    def record_failure(self, route_id: str, f: FailureClassification, *, now: datetime) -> None: ...

    def record_success(self, route_id: str, *, now: datetime) -> None: ...


class _Classifier:
    def classify(self, t: WorkerTerminal, *, now: datetime) -> FailureClassification:
        return FailureClassification("unknown", "REROUTE", 1, "route")


# Test A: SHADOW mode, frontier_worthy 0.0 vs 1.0 -> identical TaskRequirements, same graph, 1 event
@pytest.mark.asyncio
async def test_shadow_frontier_worthy_does_not_affect_decision(tmp_path: Path, monkeypatch) -> None:
    """SHADOW: frontier_worthy 0.0 vs 1.0 produce identical requirements, graph, and 1 event."""
    monkeypatch.setenv("VERDICT_DECISION_SIGNALS_MODE", "SHADOW")

    repo = _init_git_repo(tmp_path)

    # Provider returning frontier_worthy=0.0
    class LowProvider:
        call_count = 0

        def signals(self, question, *, now):
            LowProvider.call_count += 1
            return DecisionSignalSetV1(
                schema_version="decision-signals/v1",
                provider="openjev",
                model="system-one",
                version="1.0.0",
                request_id="test-low",
                purpose=question.purpose,
                signals={"frontier_worthy": 0.0},
                confidence=0.95,
                latency_ms=100,
                usage={"input_tokens": 10, "output_tokens": 5},
                input_digest="a" * 64,
                observed_at=now.isoformat(),
                failure_class=None,
                mode="SHADOW",
            )

    # Run with low provider
    events_low = EventLog(tmp_path / "events_low.jsonl")
    selector_low = _RecordingSelector("test/model")
    executor_low = _ScriptedExecutor([WorkerTerminal(ok=True, output=_VALID_NODES_JSON)])

    graph_low = await plan_with_failover(
        "test goal",
        repo=repo,
        selector=selector_low,
        executor=executor_low,
        classifier=_Classifier(),
        events=events_low,
        decision_signal_provider=LowProvider(),
    )

    # Provider returning frontier_worthy=1.0
    class HighProvider:
        call_count = 0

        def signals(self, question, *, now):
            HighProvider.call_count += 1
            return DecisionSignalSetV1(
                schema_version="decision-signals/v1",
                provider="openjev",
                model="system-one",
                version="1.0.0",
                request_id="test-high",
                purpose=question.purpose,
                signals={"frontier_worthy": 1.0},
                confidence=0.95,
                latency_ms=100,
                usage={"input_tokens": 10, "output_tokens": 5},
                input_digest="b" * 64,
                observed_at=now.isoformat(),
                failure_class=None,
                mode="SHADOW",
            )

    # Run with high provider
    events_high = EventLog(tmp_path / "events_high.jsonl")
    selector_high = _RecordingSelector("test/model")
    executor_high = _ScriptedExecutor([WorkerTerminal(ok=True, output=_VALID_NODES_JSON)])

    graph_high = await plan_with_failover(
        "test goal",
        repo=repo,
        selector=selector_high,
        executor=executor_high,
        classifier=_Classifier(),
        events=events_high,
        decision_signal_provider=HighProvider(),
    )

    # Both providers were called
    assert LowProvider.call_count == 1
    assert HighProvider.call_count == 1

    # TaskRequirements passed to selector are IDENTICAL (SHADOW has zero effect)
    assert len(selector_low.recorded_requirements) == len(selector_high.recorded_requirements)
    assert len(selector_low.recorded_requirements) > 0
    for req_low, req_high in zip(
        selector_low.recorded_requirements, selector_high.recorded_requirements, strict=True
    ):
        # SHADOW mode: frontier_worthy is hard-coded to True regardless of signal
        assert req_low.frontier_worthy == req_high.frontier_worthy
        assert req_low.frontier_worthy is True

    # Returned graphs are identical
    assert graph_low.to_dict() == graph_high.to_dict()

    # Exactly 1 decision_signals event in each run
    events_low_list = events_low.read()
    decision_events_low = [e for e in events_low_list if e.type == "decision_signals"]
    assert len(decision_events_low) == 1
    assert decision_events_low[0].data["actual_decision"]["route_id"] == "test/model"
    assert decision_events_low[0].data["signals"]["signals"]["frontier_worthy"] == 0.0

    events_high_list = events_high.read()
    decision_events_high = [e for e in events_high_list if e.type == "decision_signals"]
    assert len(decision_events_high) == 1
    assert decision_events_high[0].data["actual_decision"]["route_id"] == "test/model"
    assert decision_events_high[0].data["signals"]["signals"]["frontier_worthy"] == 1.0


# Test B: SHADOW, provider raises -> planning succeeds, 0 events
@pytest.mark.asyncio
async def test_shadow_provider_raises_planning_succeeds(tmp_path: Path, monkeypatch) -> None:
    """SHADOW: provider that raises RuntimeError -> planning succeeds, 0 decision_signals events."""
    monkeypatch.setenv("VERDICT_DECISION_SIGNALS_MODE", "SHADOW")

    repo = _init_git_repo(tmp_path)

    class RaisingProvider:
        call_count = 0

        def signals(self, question, *, now):
            RaisingProvider.call_count += 1
            raise RuntimeError("Provider is down")

    events = EventLog(tmp_path / "events.jsonl")
    selector = _RecordingSelector("test/model")
    executor = _ScriptedExecutor([WorkerTerminal(ok=True, output=_VALID_NODES_JSON)])

    graph = await plan_with_failover(
        "test goal",
        repo=repo,
        selector=selector,
        executor=executor,
        classifier=_Classifier(),
        events=events,
        decision_signal_provider=RaisingProvider(),
    )

    # Provider was called
    assert RaisingProvider.call_count == 1

    # Planning succeeded
    assert graph is not None
    assert len(graph.nodes) == 3

    # 0 decision_signals events (exception was caught)
    events_list = events.read()
    decision_events = [e for e in events_list if e.type == "decision_signals"]
    assert len(decision_events) == 0


# Test B2: SHADOW, EventLog raises on emit -> planning still succeeds
@pytest.mark.asyncio
async def test_shadow_eventlog_raises_on_emit_planning_succeeds(
    tmp_path: Path, monkeypatch
) -> None:
    """SHADOW: EventLog whose emit raises -> planning succeeds (emit wrapped in try/except)."""
    monkeypatch.setenv("VERDICT_DECISION_SIGNALS_MODE", "SHADOW")

    repo = _init_git_repo(tmp_path)

    class NormalProvider:
        call_count = 0

        def signals(self, question, *, now):
            NormalProvider.call_count += 1
            return DecisionSignalSetV1(
                schema_version="decision-signals/v1",
                provider="test",
                model="test",
                version="1.0",
                request_id="test-b2",
                purpose=question.purpose,
                signals={"frontier_worthy": 0.5},
                confidence=0.9,
                latency_ms=50,
                usage={"input_tokens": 5, "output_tokens": 3},
                input_digest="f" * 64,
                observed_at=now.isoformat(),
                failure_class=None,
                mode="SHADOW",
            )

    # EventLog that raises on decision_signals emit
    class RaisingEventLog(EventLog):
        def emit(self, type: str, **data) -> None:
            if type == "decision_signals":
                raise RuntimeError("EventLog emit is broken")
            super().emit(type, **data)

    events = RaisingEventLog(tmp_path / "events.jsonl")
    selector = _RecordingSelector("test/model")
    executor = _ScriptedExecutor([WorkerTerminal(ok=True, output=_VALID_NODES_JSON)])

    # Expect warning about emit failure
    import warnings

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        graph = await plan_with_failover(
            "test goal",
            repo=repo,
            selector=selector,
            executor=executor,
            classifier=_Classifier(),
            events=events,
            decision_signal_provider=NormalProvider(),
        )

        # Warning was emitted about emit failure
        assert any(
            "decision_signals event emission failed" in str(warning.message) for warning in w
        )

    # Provider was called
    assert NormalProvider.call_count == 1

    # Planning succeeded despite emit raising
    assert graph is not None
    assert len(graph.nodes) == 3

    # No decision_signals events recorded (because emit raised)
    events_list = events.read()
    decision_events = [e for e in events_list if e.type == "decision_signals"]
    assert len(decision_events) == 0


# Test C: OFF mode with injected provider -> 0 calls, planning succeeds, 0 events
@pytest.mark.asyncio
async def test_off_mode_provider_never_called(tmp_path: Path, monkeypatch) -> None:
    """OFF: injected provider is never called, planning succeeds, 0 decision_signals events."""
    monkeypatch.delenv("VERDICT_DECISION_SIGNALS_MODE", raising=False)

    repo = _init_git_repo(tmp_path)

    class CountingProvider:
        call_count = 0

        def signals(self, question, *, now):
            CountingProvider.call_count += 1
            return DecisionSignalSetV1(
                schema_version="decision-signals/v1",
                provider="test",
                model="test",
                version="1.0",
                request_id="test",
                purpose=question.purpose,
                signals={"frontier_worthy": 0.5},
                confidence=0.0,
                latency_ms=0,
                usage={"input_tokens": 0, "output_tokens": 0},
                input_digest="a" * 64,
                observed_at=now.isoformat(),
                failure_class=None,
                mode="OFF",
            )

    events = EventLog(tmp_path / "events.jsonl")
    selector = _RecordingSelector("test/model")
    executor = _ScriptedExecutor([WorkerTerminal(ok=True, output=_VALID_NODES_JSON)])

    graph = await plan_with_failover(
        "test goal",
        repo=repo,
        selector=selector,
        executor=executor,
        classifier=_Classifier(),
        events=events,
        decision_signal_provider=CountingProvider(),
    )

    # Provider was NEVER called (OFF mode)
    assert CountingProvider.call_count == 0

    # Planning succeeded
    assert graph is not None
    assert len(graph.nodes) == 3

    # 0 decision_signals events
    events_list = events.read()
    decision_events = [e for e in events_list if e.type == "decision_signals"]
    assert len(decision_events) == 0


# Test D: Invalid mode -> 0 calls, warning emitted
@pytest.mark.asyncio
async def test_invalid_mode_treated_as_off(tmp_path: Path, monkeypatch) -> None:
    """Invalid VERDICT_DECISION_SIGNALS_MODE -> treated as OFF, warning emitted, 0 calls."""
    monkeypatch.setenv("VERDICT_DECISION_SIGNALS_MODE", "TURBO")  # invalid -> OFF + warning

    repo = _init_git_repo(tmp_path)

    class CountingProvider:
        call_count = 0

        def signals(self, question, *, now):
            CountingProvider.call_count += 1
            return DecisionSignalSetV1(
                schema_version="decision-signals/v1",
                provider="test",
                model="test",
                version="1.0",
                request_id="test",
                purpose=question.purpose,
                signals=None,
                confidence=0.0,
                latency_ms=0,
                usage={"input_tokens": 0, "output_tokens": 0},
                input_digest="a" * 64,
                observed_at=now.isoformat(),
                failure_class=None,
                mode="OFF",
            )

    events = EventLog(tmp_path / "events.jsonl")
    selector = _RecordingSelector("test/model")
    executor = _ScriptedExecutor([WorkerTerminal(ok=True, output=_VALID_NODES_JSON)])

    # Expect warning
    import warnings

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        graph = await plan_with_failover(
            "test goal",
            repo=repo,
            selector=selector,
            executor=executor,
            classifier=_Classifier(),
            events=events,
            decision_signal_provider=CountingProvider(),
        )

        # Warning was emitted (filter to only the invalid mode warning)
        invalid_mode_warnings = [
            warning
            for warning in w
            if "Invalid VERDICT_DECISION_SIGNALS_MODE" in str(warning.message)
        ]
        assert len(invalid_mode_warnings) >= 1

    # Provider was NEVER called (invalid mode treated as OFF)
    assert CountingProvider.call_count == 0

    # Planning succeeded
    assert graph is not None


# Test E: Failover with 2 attempts -> exactly 1 decision_signals event with SECOND route
@pytest.mark.asyncio
async def test_failover_emits_decision_signals_once_with_successful_route(
    tmp_path: Path, monkeypatch
) -> None:
    """SHADOW + failover: first attempt fails, second succeeds -> 1 decision_signals event with second route."""
    monkeypatch.setenv("VERDICT_DECISION_SIGNALS_MODE", "SHADOW")

    repo = _init_git_repo(tmp_path)

    # Selector that returns different routes on each call
    class FailoverSelector:
        def __init__(self):
            self.call_count = 0
            self.routes = ["first/model", "second/model"]

        def evaluate(self, r: TaskRequirements, *, now: datetime) -> tuple[RouteVerdict, ...]:
            return ()

        def select(
            self, r: TaskRequirements, *, now: datetime
        ) -> tuple[RouteVerdict | None, tuple[RouteVerdict, ...]]:
            route_id = self.routes[self.call_count % len(self.routes)]
            self.call_count += 1
            v = RouteVerdict(
                route_id,
                "test-provider",
                EligibilityStage.SELECTED,
                None,
                "ok",
                CapacityClass.FREE,
                rank=0,
            )
            return v, (v,)

        def record_failure(
            self, route_id: str, f: FailureClassification, *, now: datetime
        ) -> None: ...

        def record_success(self, route_id: str, *, now: datetime) -> None: ...

    class ProviderWithCount:
        call_count = 0

        def signals(self, question, *, now):
            ProviderWithCount.call_count += 1
            return DecisionSignalSetV1(
                schema_version="decision-signals/v1",
                provider="test",
                model="test",
                version="1.0",
                request_id=f"test-{ProviderWithCount.call_count}",
                purpose=question.purpose,
                signals={"frontier_worthy": 0.5},
                confidence=0.9,
                latency_ms=50,
                usage={"input_tokens": 5, "output_tokens": 3},
                input_digest="c" * 64,
                observed_at=now.isoformat(),
                failure_class=None,
                mode="SHADOW",
            )

    events = EventLog(tmp_path / "events.jsonl")
    selector = FailoverSelector()

    # First executor output: ok=False (triggers planning failure and retry)
    # Second executor output: valid JSON (succeeds)
    executor = _ScriptedExecutor(
        [
            WorkerTerminal(ok=False, error="rate limited"),
            WorkerTerminal(ok=True, output=_VALID_NODES_JSON),
        ]
    )

    graph = await plan_with_failover(
        "test goal",
        repo=repo,
        selector=selector,
        executor=executor,
        classifier=_Classifier(),
        events=events,
        decision_signal_provider=ProviderWithCount(),
        max_attempts=2,
    )

    # Provider called ONCE (before the loop)
    assert ProviderWithCount.call_count == 1

    # Planning succeeded on second attempt
    assert graph is not None
    assert len(graph.nodes) == 3

    # Exactly 1 decision_signals event, with the SECOND route (the one that succeeded)
    events_list = events.read()
    decision_events = [e for e in events_list if e.type == "decision_signals"]
    assert len(decision_events) == 1
    assert decision_events[0].data["actual_decision"]["route_id"] == "second/model"


# Test F: Receipt contains decision_signals; with OFF it's absent/None
@pytest.mark.asyncio
async def test_receipt_contains_decision_signals(tmp_path: Path, monkeypatch) -> None:
    """SHADOW: receipt contains decision_signals list; OFF: field is None."""
    # Test with SHADOW
    monkeypatch.setenv("VERDICT_DECISION_SIGNALS_MODE", "SHADOW")

    repo_shadow = _init_git_repo(tmp_path / "shadow")

    class ShadowProvider:
        def signals(self, question, *, now):
            return DecisionSignalSetV1(
                schema_version="decision-signals/v1",
                provider="test",
                model="test",
                version="1.0",
                request_id="shadow-receipt",
                purpose=question.purpose,
                signals={"frontier_worthy": 0.7},
                confidence=0.88,
                latency_ms=75,
                usage={"input_tokens": 8, "output_tokens": 4},
                input_digest="d" * 64,
                observed_at=now.isoformat(),
                failure_class=None,
                mode="SHADOW",
            )

    run_dir_shadow = tmp_path / "run_shadow"
    run_dir_shadow.mkdir()
    events_shadow = EventLog(run_dir_shadow / "events.jsonl")
    selector_shadow = _RecordingSelector("test/model")
    executor_shadow = _ScriptedExecutor([WorkerTerminal(ok=True, output=_VALID_NODES_JSON)])

    graph_shadow = await plan_with_failover(
        "test goal",
        repo=repo_shadow,
        selector=selector_shadow,
        executor=executor_shadow,
        classifier=_Classifier(),
        events=events_shadow,
        decision_signal_provider=ShadowProvider(),
    )

    # Write graph for receipt
    import json

    (run_dir_shadow / "graph.json").write_text(json.dumps(graph_shadow.to_dict()))

    # Build receipt
    receipt_shadow = build_run_receipt(run_dir_shadow)

    # Receipt contains decision_signals
    assert "decision_signals" in receipt_shadow
    assert receipt_shadow["decision_signals"] is not None
    assert len(receipt_shadow["decision_signals"]) == 1
    assert receipt_shadow["decision_signals"][0]["signals"]["signals"]["frontier_worthy"] == 0.7

    # Test with OFF
    monkeypatch.delenv("VERDICT_DECISION_SIGNALS_MODE", raising=False)

    repo_off = _init_git_repo(tmp_path / "off")

    class OffProvider:
        def signals(self, question, *, now):
            return DecisionSignalSetV1(
                schema_version="decision-signals/v1",
                provider="test",
                model="test",
                version="1.0",
                request_id="off-receipt",
                purpose=question.purpose,
                signals=None,
                confidence=0.0,
                latency_ms=0,
                usage={"input_tokens": 0, "output_tokens": 0},
                input_digest="e" * 64,
                observed_at=now.isoformat(),
                failure_class=None,
                mode="OFF",
            )

    run_dir_off = tmp_path / "run_off"
    run_dir_off.mkdir()
    events_off = EventLog(run_dir_off / "events.jsonl")
    selector_off = _RecordingSelector("test/model")
    executor_off = _ScriptedExecutor([WorkerTerminal(ok=True, output=_VALID_NODES_JSON)])

    graph_off = await plan_with_failover(
        "test goal",
        repo=repo_off,
        selector=selector_off,
        executor=executor_off,
        classifier=_Classifier(),
        events=events_off,
        decision_signal_provider=OffProvider(),
    )

    # Write graph for receipt
    (run_dir_off / "graph.json").write_text(json.dumps(graph_off.to_dict()))

    # Build receipt
    receipt_off = build_run_receipt(run_dir_off)

    # Receipt has decision_signals field but it's None (no events)
    assert "decision_signals" in receipt_off
    assert receipt_off["decision_signals"] is None


# ---------------------------------------------------------------------------
# Product wiring: factory auto-wire in plan_with_failover and run_golden_path
# ---------------------------------------------------------------------------


def _make_fake_provider(mode: str = "SHADOW") -> tuple[object, list]:
    """Return (fake_provider, calls_list). calls_list grows on each .signals() call."""
    calls: list[str] = []
    now_dt = datetime(2026, 9, 25, 12, 0, 0)

    class _FakeProvider:
        def signals(self, question, *, now):
            calls.append(question.purpose)
            return DecisionSignalSetV1(
                schema_version="decision-signals/v1",
                provider="fake",
                model="fake-1",
                version="1.0",
                request_id="fake-req",
                purpose=question.purpose,
                signals={"complexity": 0.5},
                confidence=0.8,
                latency_ms=1,
                usage={"input_tokens": 10, "output_tokens": 0},
                input_digest="a" * 64,
                observed_at=now_dt.isoformat() + "Z",
                failure_class=None,
                mode=mode,
            )

    return _FakeProvider(), calls


@pytest.mark.asyncio
async def test_plan_with_failover_auto_wires_factory(tmp_path: Path, monkeypatch) -> None:
    """plan_with_failover with decision_signal_provider=None (default) calls provider_from_env
    and uses the returned provider exactly once. Calling with OFF mode -> 0 calls."""
    monkeypatch.setenv("VERDICT_DECISION_SIGNALS_MODE", "SHADOW")

    repo = _init_git_repo(tmp_path)
    fake_provider, calls = _make_fake_provider()

    import verdict.decision_signals.factory as fmod

    monkeypatch.setattr(fmod, "provider_from_env", lambda: fake_provider)

    events_path = tmp_path / "events.jsonl"
    log = EventLog(events_path)
    selector = _RecordingSelector("test/model")
    executor = _ScriptedExecutor([WorkerTerminal(ok=True, output=_VALID_NODES_JSON)])

    # No provider passed -> factory is consulted
    graph = await plan_with_failover(
        "wiring test",
        repo=repo,
        selector=selector,
        executor=executor,
        classifier=_Classifier(),
        events=log,
        # decision_signal_provider intentionally omitted (default None)
    )

    assert len(calls) == 1, f"expected 1 provider call, got {len(calls)}"
    assert graph is not None

    decision_events = [e for e in log.read() if e.type == "decision_signals"]
    assert len(decision_events) == 1, "expected exactly 1 decision_signals event"


@pytest.mark.asyncio
async def test_plan_with_failover_off_mode_zero_calls(tmp_path: Path, monkeypatch) -> None:
    """plan_with_failover with mode=OFF: factory returns None, 0 provider calls."""
    monkeypatch.setenv("VERDICT_DECISION_SIGNALS_MODE", "OFF")

    repo = _init_git_repo(tmp_path)
    call_count = []

    import verdict.decision_signals.factory as fmod

    original_pfn = fmod.provider_from_env

    def tracking_pfn():
        result = original_pfn()
        call_count.append(result)
        return result

    monkeypatch.setattr(fmod, "provider_from_env", tracking_pfn)

    events_path = tmp_path / "events.jsonl"
    log = EventLog(events_path)

    graph = await plan_with_failover(
        "wiring test off",
        repo=repo,
        selector=_RecordingSelector("test/model"),
        executor=_ScriptedExecutor([WorkerTerminal(ok=True, output=_VALID_NODES_JSON)]),
        classifier=_Classifier(),
        events=log,
    )

    # factory called but returned None (mode=OFF)
    assert all(p is None for p in call_count), "OFF mode must return None from factory"
    decision_events = [e for e in log.read() if e.type == "decision_signals"]
    assert len(decision_events) == 0, "OFF mode must emit 0 decision_signals events"
    assert graph is not None


@pytest.mark.asyncio
async def test_mutation_proof_plan_with_failover_wiring(tmp_path: Path, monkeypatch) -> None:
    """MUTATION: if factory.provider_from_env() is bypassed (simulates replacing
    decision_signal_provider = provider_from_env() with = None), the baseline assertion
    that 1 event is emitted FAILS — proves the auto-wire code is load-bearing."""
    monkeypatch.setenv("VERDICT_DECISION_SIGNALS_MODE", "SHADOW")
    repo = _init_git_repo(tmp_path)

    import verdict.decision_signals.factory as fmod

    # --- Baseline: factory returns a real fake provider -> 1 event ---
    fake_provider, calls = _make_fake_provider()
    monkeypatch.setattr(fmod, "provider_from_env", lambda: fake_provider)

    log_base = EventLog(tmp_path / "events_base.jsonl")
    await plan_with_failover(
        "mutation test",
        repo=repo,
        selector=_RecordingSelector("test/model"),
        executor=_ScriptedExecutor([WorkerTerminal(ok=True, output=_VALID_NODES_JSON)]),
        classifier=_Classifier(),
        events=log_base,
    )
    assert len(calls) == 1, "baseline: factory-provided provider must be called once"
    base_events = [e for e in log_base.read() if e.type == "decision_signals"]
    assert len(base_events) == 1, "baseline: 1 decision_signals event"

    # --- Mutation: factory returns None (simulates `decision_signal_provider = None`) ---
    # This is the SAME call as the baseline, but now provider_from_env() returns None.
    # If the wiring code `decision_signal_provider = provider_from_env()` were replaced
    # with `decision_signal_provider = None`, the result would be the same: 0 calls, 0 events.
    # The baseline assertion above would fail -> proves the wiring is load-bearing.
    monkeypatch.setattr(fmod, "provider_from_env", lambda: None)  # MUTATION

    _, __ = _make_fake_provider()  # fake provider not called under mutation
    log_mut = EventLog(tmp_path / "events_mut.jsonl")
    await plan_with_failover(
        "mutation test",
        repo=repo,
        selector=_RecordingSelector("test/model"),
        executor=_ScriptedExecutor([WorkerTerminal(ok=True, output=_VALID_NODES_JSON)]),
        classifier=_Classifier(),
        events=log_mut,
    )
    mut_events = [e for e in log_mut.read() if e.type == "decision_signals"]
    # With mutation (None returned): 0 events
    assert len(mut_events) == 0, "mutated wiring must emit 0 decision_signals events"
    # Demonstrate that applying the mutation to the baseline assertion makes it fail
    # (i.e., the baseline `assert len(base_events) == 1` would NOT pass under mutation)
    assert len(base_events) != len(mut_events), (
        "baseline (1 event) differs from mutated (0 events) — proves wiring is load-bearing"
    )


@pytest.mark.asyncio
async def test_run_golden_path_auto_wires_factory(tmp_path: Path, monkeypatch) -> None:
    """run_golden_path with decision_signal_provider=None calls factory and uses provider."""
    from verdict.orchestration.run import run_golden_path
    from verdict.orchestration.runtime import RuntimePolicy

    monkeypatch.setenv("VERDICT_DECISION_SIGNALS_MODE", "SHADOW")

    repo = _init_git_repo(tmp_path)
    fake_provider, calls = _make_fake_provider()

    import verdict.decision_signals.factory as fmod

    monkeypatch.setattr(fmod, "provider_from_env", lambda: fake_provider)

    runs_root = tmp_path / "runs"
    runs_root.mkdir()

    result = await run_golden_path(
        "wiring test golden path",
        repo=repo,
        runs_root=runs_root,
        selector=_RecordingSelector("test/model"),
        executor=_ScriptedExecutor([WorkerTerminal(ok=True, output=_VALID_NODES_JSON)]),
        classifier=_Classifier(),
        reviewer=None,
        policy=RuntimePolicy(max_parallel=3),
        # decision_signal_provider intentionally omitted
    )

    # Provider was called once (in plan_with_failover)
    assert len(calls) == 1, f"expected 1 provider call, got {len(calls)}"

    # EventLog contains a decision_signals event
    log = EventLog(result.run_dir / "events.jsonl")
    decision_events = [e for e in log.read() if e.type == "decision_signals"]
    assert len(decision_events) == 1, "run_golden_path must emit exactly 1 decision_signals event"
