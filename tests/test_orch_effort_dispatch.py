"""Orchestrate path records and forwards Verdict's chosen effort per attempt."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest

from verdict.orchestration.contracts import NodeKind, WorkerTerminal, WorkGraph, WorkNode
from verdict.orchestration.runtime import DagRuntime, RuntimePolicy

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


class Events:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def emit(self, type: str, node_id: str = "", **data: Any) -> None:
        self.rows.append({"type": type, "node_id": node_id, **data})

    def of(self, type: str, node_id: str | None = None) -> list[dict[str, Any]]:
        return [
            row
            for row in self.rows
            if row["type"] == type and (node_id is None or row["node_id"] == node_id)
        ]


class Selector:
    def __init__(self, routes: list[str]) -> None:
        self.routes = routes

    def select(self, requirements: Any, *, now: datetime) -> tuple[Any, tuple[Any, ...]]:
        from verdict.orchestration.contracts import CapacityClass, EligibilityStage, RouteVerdict

        verdicts = []
        chosen = None
        for rank, route in enumerate(self.routes, 1):
            if route in requirements.exclude_routes:
                continue
            verdict = RouteVerdict(
                route,
                route.split("/")[0],
                EligibilityStage.SELECTED,
                None,
                "ok",
                CapacityClass.SUBSCRIPTION,
                rank=rank,
            )
            verdicts.append(verdict)
            if chosen is None:
                chosen = verdict
        return chosen, tuple(verdicts)

    def record_failure(self, route_id: str, failure: Any, *, now: datetime) -> None:
        return None

    def record_success(self, route_id: str, *, now: datetime) -> None:
        return None


class Classifier:
    def classify(self, terminal: WorkerTerminal, *, now: datetime) -> Any:
        from verdict.orchestration.contracts import FailureClassification

        return FailureClassification("unknown", "REROUTE", 0, "none", terminal.error)


class WritingExecutor:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def run(
        self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float
    ) -> WorkerTerminal:
        self.calls.append({"route_id": route_id, "cwd": cwd})
        node = prompt.split(":", 1)[0]
        (cwd / f"{node}.txt").write_text(f"{node} by {route_id}\n")
        return WorkerTerminal(ok=True, output="RESULT: DONE", model=route_id)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True)
    (root / "README.md").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
    return root


def _node(kind: NodeKind) -> WorkNode:
    if kind is NodeKind.IMPLEMENT:
        return WorkNode(
            "impl",
            "write impl",
            kind=kind,
            owned_files=("impl.txt",),
            verification_command=("sh", "-c", "test -f impl.txt"),
        )
    if kind is NodeKind.RESEARCH:
        return WorkNode("research", "look it up", kind=kind)
    return WorkNode("rev", "review the change", kind=kind)


async def _drive(
    tmp_path: Path, kind: NodeKind, *, model_rows: dict[str, Any] | None = None
) -> Events:
    repo = _repo(tmp_path)
    events = Events()
    runtime = DagRuntime(
        repo=repo,
        run_dir=tmp_path / "run",
        graph=WorkGraph("g", (_node(kind),)),
        selector=Selector(["cc/sonnet"]),
        executor=WritingExecutor(),
        classifier=Classifier(),
        events=events,
        prompt_for=lambda node, cwd: f"{node.node_id}: {node.objective}",
        policy=RuntimePolicy(require_review=False, run_repo_gates=False, max_attempts_per_node=1),
        now=lambda: NOW,
        model_rows=model_rows,
    )
    await runtime.run()
    return events


@pytest.mark.parametrize(
    ("kind", "expected"),
    [(NodeKind.IMPLEMENT, "medium"), (NodeKind.REVIEW, "high"), (NodeKind.RESEARCH, "high")],
)
async def test_selection_records_chosen_effort_for_node_kind(
    tmp_path: Path, kind: NodeKind, expected: str
) -> None:
    events = await _drive(tmp_path, kind)
    selections = events.of("selection")
    assert selections, f"{kind.value} emitted no selection event"
    assert selections[0]["chosen_effort"] == expected
    assert (
        selections[0]["effort_reason"]["task_kind"]
        == {NodeKind.IMPLEMENT: "implement", NodeKind.REVIEW: "review", NodeKind.RESEARCH: "plan"}[
            kind
        ]
    )


async def test_reasoning_disabled_records_no_override(tmp_path: Path) -> None:
    events = await _drive(
        tmp_path,
        NodeKind.IMPLEMENT,
        model_rows={"cc/sonnet": {"reasoning": False, "thinkingLevelMap": {"high": 1}}},
    )
    selection = events.of("selection", "impl")[0]
    assert selection["chosen_effort"] is None
    assert selection["effort_reason"]["clamp"] == "reasoning_disabled"
    assert selection["effort_reason"]["metadata_source"] == "model_rows"


async def test_missing_medium_clamps_down_never_up(tmp_path: Path) -> None:
    events = await _drive(
        tmp_path,
        NodeKind.IMPLEMENT,
        model_rows={"cc/sonnet": {"thinkingLevelMap": {"low": 1, "high": 1}}},
    )
    selection = events.of("selection", "impl")[0]
    assert selection["chosen_effort"] == "low"
    assert selection["effort_reason"]["clamp"] == "medium->low"
    assert "high" not in str(selection["chosen_effort"])


class _CapturingClient:
    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self.payloads = payloads

    async def __aenter__(self) -> _CapturingClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> Any:
        self.payloads.append(json)
        request = httpx.Request("POST", url)
        return httpx.Response(
            200,
            json={
                "model": json["model"],
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            },
            request=request,
        )


async def test_direct_gateway_sends_reasoning_effort_only_when_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from verdict.orchestration.executors import DirectGatewayExecutor

    payloads: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "verdict.orchestration.executors.httpx.AsyncClient",
        lambda **kwargs: _CapturingClient(payloads),
    )
    executor = DirectGatewayExecutor(base_url="http://gateway.test", api_key="not-a-secret")
    await executor.run("look", route_id="cc/sonnet", cwd=tmp_path, timeout_seconds=5, effort="high")
    await executor.run("look", route_id="cc/sonnet", cwd=tmp_path, timeout_seconds=5)
    await executor.run("look", route_id="cc/sonnet", cwd=tmp_path, timeout_seconds=5, effort=None)
    assert payloads[0]["reasoning_effort"] == "high"
    assert "reasoning_effort" not in payloads[1]
    assert "reasoning_effort" not in payloads[2]


def test_prime_per_call_effort_overrides_constructor_only(tmp_path: Path) -> None:
    from verdict.orchestration.executors import PrimeHeadlessExecutor

    executor = PrimeHeadlessExecutor(prime_bin="prime", thinking="low")
    overridden = executor._command("PROMPT", "cc/sonnet", tmp_path, effort="high")
    omitted = executor._command("PROMPT", "cc/sonnet", tmp_path)
    explicit_none = executor._command("PROMPT", "cc/sonnet", tmp_path, effort=None)
    assert executor.thinking == "low"
    assert overridden[overridden.index("--thinking") + 1] == "high"
    assert omitted[omitted.index("--thinking") + 1] == "low"
    assert "--thinking" not in explicit_none


async def test_fake_without_effort_parameter_still_runs(tmp_path: Path) -> None:
    from verdict.orchestration.controller_operations import bounded_executor_call

    class Fake:
        def __init__(self) -> None:
            self.calls = 0

        async def run(
            self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float
        ) -> WorkerTerminal:
            self.calls += 1
            return WorkerTerminal(ok=True, output="ok", model=route_id)

    fake = Fake()
    terminal = await bounded_executor_call(
        fake,
        "prompt",
        route_id="cc/sonnet",
        cwd=tmp_path,
        timeout_seconds=5,
        run_dir=tmp_path,
        phase="worker",
        attempt_id="impl-a1",
        effort="medium",
    )
    assert terminal.ok
    assert fake.calls == 1


def _receipt_run(tmp_path: Path, events: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    from verdict.orchestration.receipt import EventLog, build_run_receipt

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    graph = WorkGraph(
        "g",
        (
            WorkNode(
                "impl",
                "write impl",
                owned_files=("impl.txt",),
                verification_command=("sh", "-c", "test -f impl.txt"),
            ),
        ),
    )
    (run_dir / "graph.json").write_text(__import__("json").dumps(graph.to_dict()))
    log = EventLog(run_dir / "events.jsonl")
    log.emit("run_started", run_id="run-1", goal="g")
    for event_type, data in events:
        log.emit(event_type, node_id="impl", **data)
    log.emit("run_finished", outcome="COMPLETE", reason="done")
    return build_run_receipt(run_dir)


def test_receipt_attempt_carries_chosen_effort(tmp_path: Path) -> None:
    receipt = _receipt_run(
        tmp_path,
        [
            (
                "selection",
                {
                    "route_id": "cc/sonnet",
                    "attempt": 1,
                    "chosen_effort": "medium",
                    "effort_reason": {
                        "task_kind": "implement",
                        "default_effort": "medium",
                        "clamp": None,
                        "metadata_source": "model_rows",
                        "route_id": "cc/sonnet",
                        "attempt": 1,
                    },
                },
            ),
            ("dispatch", {"route_id": "cc/sonnet", "attempt": 1, "capacity_class": "subscription"}),
            (
                "terminal",
                {"route_id": "cc/sonnet", "attempt": 1, "ok": True, "reported_model": "cc/sonnet"},
            ),
        ],
    )
    attempt = receipt["nodes"][0]["attempts"][0]
    assert attempt["chosen_effort"] == "medium"
    assert attempt["effort_reason"]["task_kind"] == "implement"
    assert attempt["effort_reason"]["route_id"] == "cc/sonnet"
    assert attempt["executed_effort"] == "unverified"


def test_legacy_receipt_omits_effort_fields(tmp_path: Path) -> None:
    receipt = _receipt_run(
        tmp_path,
        [
            ("dispatch", {"route_id": "cc/sonnet", "attempt": 1, "capacity_class": "subscription"}),
            (
                "terminal",
                {"route_id": "cc/sonnet", "attempt": 1, "ok": True, "reported_model": "cc/sonnet"},
            ),
        ],
    )
    attempt = receipt["nodes"][0]["attempts"][0]
    assert "chosen_effort" not in attempt
    assert "effort_reason" not in attempt
    assert "executed_effort" not in attempt
    assert attempt["route_id"] == "cc/sonnet"
    assert attempt["outcome"] == "success"


def test_replaced_selection_does_not_leak_effort(tmp_path: Path) -> None:
    receipt = _receipt_run(
        tmp_path,
        [
            (
                "selection",
                {
                    "route_id": "cc/sonnet",
                    "attempt": 1,
                    "chosen_effort": "high",
                    "effort_reason": {"task_kind": "review", "route_id": "cc/sonnet", "attempt": 1},
                },
            ),
            (
                "selection",
                {
                    "route_id": "cx/gpt",
                    "attempt": 1,
                    "chosen_effort": "low",
                    "effort_reason": {"task_kind": "implement", "route_id": "cx/gpt", "attempt": 1},
                },
            ),
            ("dispatch", {"route_id": "cx/gpt", "attempt": 1}),
            (
                "terminal",
                {"route_id": "cx/gpt", "attempt": 1, "ok": True, "reported_model": "cx/gpt"},
            ),
        ],
    )
    attempt = receipt["nodes"][0]["attempts"][0]
    assert attempt["route_id"] == "cx/gpt"
    assert attempt["chosen_effort"] == "low"
    assert attempt["effort_reason"]["route_id"] == "cx/gpt"
    assert "high" not in str(attempt["chosen_effort"])


def test_receipt_preserves_null_chosen_effort(tmp_path: Path) -> None:
    receipt = _receipt_run(
        tmp_path,
        [
            (
                "selection",
                {
                    "route_id": "cc/sonnet",
                    "attempt": 1,
                    "chosen_effort": None,
                    "effort_reason": {"task_kind": "implement", "clamp": "reasoning_disabled"},
                },
            ),
            ("dispatch", {"route_id": "cc/sonnet", "attempt": 1}),
            ("terminal", {"route_id": "cc/sonnet", "attempt": 1, "ok": False, "error": "nope"}),
        ],
    )
    attempt = receipt["nodes"][0]["attempts"][0]
    assert "chosen_effort" in attempt
    assert attempt["chosen_effort"] is None
    assert attempt["executed_effort"] == "unverified"


async def test_wrappers_forward_distinct_efforts(tmp_path: Path) -> None:
    from verdict.orchestration.contracts import EFFORT_OMITTED, AnswerOnlyExecutor
    from verdict.orchestration.executors import FaultInjectingExecutor, MixedExecutor

    class Recording:
        answer_only_supported = True

        def __init__(self) -> None:
            self.efforts: list[Any] = []

        async def run(
            self,
            prompt: str,
            *,
            route_id: str,
            cwd: Path,
            timeout_seconds: float,
            tools: bool = True,
            effort: Any = EFFORT_OMITTED,
        ) -> WorkerTerminal:
            self.efforts.append(effort)
            return WorkerTerminal(ok=True, output="ok", model=route_id)

    inner = Recording()
    fault = FaultInjectingExecutor(inner, {})
    await fault.run("p", route_id="cc/sonnet", cwd=tmp_path, timeout_seconds=5, effort="high")
    await fault.run("p", route_id="cc/sonnet", cwd=tmp_path, timeout_seconds=5, effort=None)
    mixed = MixedExecutor({}, inner)
    await mixed.run("p", route_id="cx/gpt", cwd=tmp_path, timeout_seconds=5, effort="low")
    answer = AnswerOnlyExecutor(inner)
    await answer.run("p", route_id="cc/sonnet", cwd=tmp_path, timeout_seconds=5, effort="medium")
    assert inner.efforts == ["high", None, "low", "medium"]


def test_parallel_prime_commands_do_not_share_effort(tmp_path: Path) -> None:
    from verdict.orchestration.executors import PrimeHeadlessExecutor

    executor = PrimeHeadlessExecutor(prime_bin="prime", thinking="minimal")
    high = executor._command("a", "cc/sonnet", tmp_path, effort="high")
    low = executor._command("b", "cx/gpt", tmp_path, effort="low")
    assert executor.thinking == "minimal"
    assert high[high.index("--thinking") + 1] == "high"
    assert low[low.index("--thinking") + 1] == "low"


def test_public_effort_rows_drop_secrets() -> None:
    from verdict.orchestration.cli import _public_effort_rows

    class Selector:
        def effort_model_rows(self) -> dict[str, dict[str, Any]]:
            return {
                "cc/sonnet": {
                    "reasoning": True,
                    "thinkingLevelMap": {"medium": 1},
                    "api_key": "secret-value",
                }
            }

    class PrivateOnly:
        _rows: ClassVar[dict[str, dict[str, bool]]] = {"cc/sonnet": {"reasoning": False}}

    rows = _public_effort_rows(Selector())
    assert rows == {"cc/sonnet": {"reasoning": True, "thinkingLevelMap": {"medium": 1}}}
    assert "secret-value" not in str(rows)
    assert _public_effort_rows(PrivateOnly()) is None
