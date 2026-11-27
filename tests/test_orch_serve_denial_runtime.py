"""The runtime's reaction to a ``verdict serve`` route denial (GitHub issue #1366, part 2).

A denial is classified ``route_denied``. A permanent code removes the route for every
node of the run; ``provider_unhealthy`` cools the id prefix; any other code cools the
route. Nothing is persisted: no cooldown event and no ``record_failure`` call.
"""

from __future__ import annotations

import asyncio
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from tests.test_orch_runtime import NOW, Events, Executor, Selector
from verdict.orchestration import receipt
from verdict.orchestration.contracts import RunOutcome, WorkerTerminal, WorkGraph, WorkNode
from verdict.orchestration.recovery import FailureIntelligence
from verdict.orchestration.runtime import DagRuntime, RuntimePolicy

REFUSED = "claude/x"
TWIN = "cc/x"


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
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


class HookSelector(Selector):
    """The test selector plus the two run-scoped hooks the real ladder has."""

    def __init__(self, routes: list[str]) -> None:
        super().__init__(routes)
        self.excluded: list[tuple[str, str]] = []
        self.cooled: list[tuple[str, float]] = []
        self.failures: list[str] = []

    def exclude_for_run(self, route_id: str, reason: str) -> None:
        self.excluded.append((route_id, reason))
        self.routes = [r for r in self.routes if r != route_id]

    def cool_for_run(self, key: str, seconds: float, *, now: datetime) -> None:
        self.cooled.append((key, seconds))

    def record_failure(self, route_id: str, failure: Any, *, now: datetime) -> None:
        self.failures.append(route_id)
        super().record_failure(route_id, failure, now=now)


class RefusingExecutor(Executor):
    """Returns ``error`` with status 503 for the refused route, else does the work."""

    def __init__(self, error: str) -> None:
        super().__init__({})
        self.error = error
        self.attempts: list[tuple[str, str]] = []

    async def run(
        self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float
    ) -> WorkerTerminal:
        self.attempts.append((prompt.split(":", 1)[0], route_id))
        if route_id == REFUSED:
            return WorkerTerminal(
                ok=False, model=route_id, harness="opencode", status_code=503, error=self.error
            )
        return await super().run(
            prompt, route_id=route_id, cwd=cwd, timeout_seconds=timeout_seconds
        )


def _denial(code: str) -> str:
    return f"assigned route {REFUSED!r} denied: {code} (detail)"


def _run(repo: Path, error: str, selector: Selector) -> tuple[RunOutcome, Events, RefusingExecutor]:
    events, executor = Events(), RefusingExecutor(error)
    node_a = WorkNode(
        "a", "write a", owned_files=("a.txt",), verification_command=("sh", "-c", "test -f a.txt")
    )
    node_b = WorkNode(
        "b",
        "write b",
        depends_on=("a",),
        owned_files=("b.txt",),
        verification_command=("sh", "-c", "test -f b.txt"),
    )
    runtime = DagRuntime(
        repo=repo,
        run_dir=repo.parent / "run-denial",
        graph=WorkGraph("g", (node_a, node_b)),
        selector=selector,
        executor=executor,
        classifier=FailureIntelligence(),
        events=events,
        prompt_for=lambda n, cwd: f"{n.node_id}: {n.objective}",
        reviewer=None,
        policy=RuntimePolicy(require_review=False, run_repo_gates=False),
        now=lambda: NOW,
    )
    result = asyncio.run(runtime.run())
    return result.outcome, events, executor


def _denial_events(events: Events) -> list[dict[str, Any]]:
    return [e for e in events.of("controller") if e.get("phase") == "serve_denial"]


def test_permanent_denial_excludes_the_route_for_every_node(repo: Path) -> None:
    selector = HookSelector([REFUSED, TWIN])

    outcome, events, executor = _run(repo, _denial("opaque_route_disallowed"), selector)

    assert outcome is RunOutcome.COMPLETE
    assert executor.attempts == [("a", REFUSED), ("a", TWIN), ("b", TWIN)]
    assert [e["category"] for e in events.of("failure")] == ["route_denied"]
    assert selector.excluded == [(REFUSED, "serve_denied:opaque_route_disallowed")]
    assert selector.cooled == []
    assert _denial_events(events) == [
        {
            **_denial_events(events)[0],
            "route_id": REFUSED,
            "code": "opaque_route_disallowed",
            "action": "excluded",
        }
    ]


def test_a_denial_persists_no_cooldown(repo: Path) -> None:
    selector = HookSelector([REFUSED, TWIN])

    _, events, _ = _run(repo, _denial("opaque_route_disallowed"), selector)

    assert events.of("cooldown") == []
    assert selector.failures == []


def test_provider_unhealthy_cools_the_id_prefix_and_removes_nothing(repo: Path) -> None:
    selector = HookSelector([REFUSED, TWIN])

    outcome, events, _ = _run(repo, _denial("provider_unhealthy"), selector)

    assert outcome is RunOutcome.COMPLETE
    assert selector.excluded == []
    assert selector.cooled and set(selector.cooled) == {("prefix:claude", 300.0)}
    assert {e["action"] for e in _denial_events(events)} == {"prefix_cooled"}


def test_unknown_code_cools_only_the_refused_route(repo: Path) -> None:
    selector = HookSelector([REFUSED, TWIN])

    _, events, _ = _run(repo, _denial("some_new_reason"), selector)

    assert selector.excluded == []
    assert selector.cooled and set(selector.cooled) == {(f"route:{REFUSED}", 60.0)}
    assert {e["code"] for e in _denial_events(events)} == {"some_new_reason"}
    assert {e["action"] for e in _denial_events(events)} == {"route_cooled"}


@pytest.mark.parametrize(
    "code", ["opaque_route_disallowed", "provider_unhealthy", "some_new_reason"]
)
def test_the_twin_route_is_never_named_in_an_exclusion_or_cooldown(repo: Path, code: str) -> None:
    selector = HookSelector([REFUSED, TWIN])

    _run(repo, _denial(code), selector)

    named = [route for route, _ in selector.excluded] + [key for key, _ in selector.cooled]
    assert named
    assert all(TWIN not in key and key != "prefix:cc" for key in named)


def test_a_selector_without_the_hooks_still_classifies_and_records(repo: Path) -> None:
    outcome, events, _ = _run(repo, _denial("opaque_route_disallowed"), Selector([REFUSED, TWIN]))

    assert outcome is RunOutcome.COMPLETE
    assert next(e["category"] for e in events.of("failure")) == "route_denied"
    assert next(e["action"] for e in _denial_events(events)) == "none"


def test_a_real_503_keeps_todays_behaviour(repo: Path) -> None:
    selector = HookSelector([REFUSED, TWIN])

    _, events, _ = _run(repo, "upstream exploded", selector)

    assert next(e["category"] for e in events.of("failure")) == "upstream_temporary"
    assert events.of("cooldown")
    assert selector.failures and selector.failures[0] == REFUSED
    assert _denial_events(events) == []
    assert selector.excluded == [] and selector.cooled == []


def test_route_denied_is_not_counted_as_a_provider_fault_in_the_ledger() -> None:
    """Left out of the fixed class map on purpose, so its ledger class is ``other``."""
    assert receipt._CATEGORY_CLASS.get("route_denied", "other") == "other"
    assert receipt._CATEGORY_CLASS["upstream_temporary"] == "provider_5xx"
