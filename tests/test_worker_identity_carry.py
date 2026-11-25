"""The run id and the node id travel from the runtime to the executor (#1325).

They go through ``run_executor`` and ``bounded_executor_call`` as optional keywords, the pattern
``tools`` and ``effort`` already use: an executor that does not take them is called exactly as it
was before, and the two wrappers (``MixedExecutor``, ``FaultInjectingExecutor``) hand them on to
the executor they wrap, so ``--executor-map node=opencode`` and fault injection still sign.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from verdict.orchestration import controller_operations as operations
from verdict.orchestration.contracts import (
    AnswerOnlyExecutor,
    OrchestrationError,
    WorkerTerminal,
    run_executor,
)
from verdict.orchestration.executors import FaultInjectingExecutor, MixedExecutor

ROUTE = "cc/claude-sonnet-5"


class Plain:
    """The shape every executor had before: no optional keywords."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def run(
        self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float
    ) -> WorkerTerminal:
        self.calls.append({"route_id": route_id, "timeout_seconds": timeout_seconds})
        return WorkerTerminal(ok=True, output="ok", model=route_id)


class Taker:
    """An executor that asks for the ids."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def run(
        self,
        prompt: str,
        *,
        route_id: str,
        cwd: Path,
        timeout_seconds: float,
        run_id: str | None = None,
        node_id: str | None = None,
    ) -> WorkerTerminal:
        self.calls.append(
            {"run_id": run_id, "node_id": node_id, "timeout_seconds": timeout_seconds}
        )
        return WorkerTerminal(ok=True, output="ok", model=route_id)


class OnlyRun:
    """Takes the run id and not the node id: each keyword is offered on its own."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def run(
        self,
        prompt: str,
        *,
        route_id: str,
        cwd: Path,
        timeout_seconds: float,
        run_id: str | None = None,
    ) -> WorkerTerminal:
        self.calls.append({"run_id": run_id})
        return WorkerTerminal(ok=True, output="ok", model=route_id)


class Wild:
    """Takes ``**kwargs`` and records them: it is offered what a keyword-taking executor is."""

    def __init__(self) -> None:
        self.kwargs: list[dict[str, Any]] = []

    async def run(self, prompt: str, **kwargs: Any) -> WorkerTerminal:
        self.kwargs.append(kwargs)
        return WorkerTerminal(ok=True, output="ok", model=kwargs["route_id"])


def _go(executor: Any, **ids: Any) -> WorkerTerminal:
    return asyncio.run(
        run_executor(executor, "prompt", route_id=ROUTE, cwd=Path("."), timeout_seconds=7.0, **ids)
    )


def test_ids_reach_an_executor_that_asks_for_them() -> None:
    taker = Taker()
    assert _go(taker, run_id="run-1", node_id="node-a").ok
    assert taker.calls == [{"run_id": "run-1", "node_id": "node-a", "timeout_seconds": 7.0}]


def test_each_id_is_offered_on_its_own() -> None:
    only = OnlyRun()
    assert _go(only, run_id="run-1", node_id="node-a").ok
    assert only.calls == [{"run_id": "run-1"}]


def test_an_executor_that_takes_neither_is_called_exactly_as_before() -> None:
    plain = Plain()
    assert _go(plain, run_id="run-1", node_id="node-a").ok
    assert plain.calls == [{"route_id": ROUTE, "timeout_seconds": 7.0}]


def test_a_call_without_ids_is_byte_identical_to_a_plain_run() -> None:
    wild = Wild()
    assert _go(wild).ok
    assert wild.kwargs == [{"route_id": ROUTE, "cwd": Path("."), "timeout_seconds": 7.0}]


def test_a_kwargs_executor_is_offered_the_ids_and_only_when_there_are_some() -> None:
    wild = Wild()
    assert _go(wild, run_id="run-1", node_id="node-a").ok
    assert wild.kwargs[0]["run_id"] == "run-1"
    assert wild.kwargs[0]["node_id"] == "node-a"
    wild_none = Wild()
    assert _go(wild_none, run_id=None, node_id=None).ok
    assert "run_id" not in wild_none.kwargs[0] and "node_id" not in wild_none.kwargs[0]


def test_bounded_executor_call_carries_the_ids_to_the_executor(tmp_path: Path) -> None:
    taker = Taker()
    terminal = asyncio.run(
        operations.bounded_executor_call(
            taker,
            "prompt",
            route_id=ROUTE,
            cwd=tmp_path,
            timeout_seconds=100.0,
            run_dir=None,
            phase="worker",
            attempt_id="node-a-a1",
            run_id="run-1",
            node_id="node-a",
        )
    )
    assert terminal.ok
    assert [(c["run_id"], c["node_id"]) for c in taker.calls] == [("run-1", "node-a")]


def test_bounded_executor_call_without_ids_calls_a_plain_executor_as_before(tmp_path: Path) -> None:
    plain = Plain()
    terminal = asyncio.run(
        operations.bounded_executor_call(
            plain,
            "prompt",
            route_id=ROUTE,
            cwd=tmp_path,
            timeout_seconds=100.0,
            run_dir=None,
            phase="planner",
            attempt_id="plan-1",
        )
    )
    assert terminal.ok and len(plain.calls) == 1


def test_mixed_executor_hands_the_ids_to_the_executor_it_picks(tmp_path: Path) -> None:
    taker = Taker()
    mixed = MixedExecutor({}, taker)
    cwd = tmp_path / "worktrees" / "node-a-a1"
    cwd.mkdir(parents=True)
    terminal = asyncio.run(
        run_executor(
            mixed,
            "prompt",
            route_id=ROUTE,
            cwd=cwd,
            timeout_seconds=5.0,
            run_id="run-1",
            node_id="node-a",
        )
    )
    assert terminal.ok
    assert [(c["run_id"], c["node_id"]) for c in taker.calls] == [("run-1", "node-a")]


def test_mixed_executor_over_a_plain_executor_is_called_as_before(tmp_path: Path) -> None:
    plain = Plain()
    mixed = MixedExecutor({}, plain)
    terminal = asyncio.run(
        run_executor(
            mixed,
            "prompt",
            route_id=ROUTE,
            cwd=tmp_path,
            timeout_seconds=5.0,
            run_id="run-1",
            node_id="node-a",
        )
    )
    assert terminal.ok and plain.calls == [{"route_id": ROUTE, "timeout_seconds": 5.0}]


def test_fault_injecting_executor_hands_the_ids_to_the_executor_it_wraps(tmp_path: Path) -> None:
    taker = Taker()
    wrapped = FaultInjectingExecutor(taker, {})
    terminal = asyncio.run(
        run_executor(
            wrapped,
            "prompt",
            route_id=ROUTE,
            cwd=tmp_path,
            timeout_seconds=5.0,
            run_id="run-1",
            node_id="node-a",
        )
    )
    assert terminal.ok
    assert [(c["run_id"], c["node_id"]) for c in taker.calls] == [("run-1", "node-a")]


def test_a_planner_call_has_no_node_and_the_answer_only_wrapper_refuses_an_agent_executor() -> None:
    """The planner calls through ``AnswerOnlyExecutor``, which takes no ids and so passes none on.

    An executor that does its work with tools (``OpenCodeExecutor``) is refused there before any
    call is made, so a planner call never reaches a signing executor without a node id.
    """
    from verdict.orchestration.executor_opencode import OpenCodeExecutor

    with pytest.raises(OrchestrationError, match="answer-only"):
        AnswerOnlyExecutor(OpenCodeExecutor())

    taker = Taker()
    taker.answer_only_supported = True  # type: ignore[attr-defined]
    answer = AnswerOnlyExecutor(taker)
    terminal = asyncio.run(
        run_executor(
            answer,
            "prompt",
            route_id=ROUTE,
            cwd=Path("."),
            timeout_seconds=5.0,
            run_id="run-1",
            node_id="node-a",
        )
    )
    assert terminal.ok
    assert [(c["run_id"], c["node_id"]) for c in taker.calls] == [(None, None)]
