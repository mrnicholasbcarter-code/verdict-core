"""BOD-268: assignment + failover end to end, through real Verdict components.

Nothing in Verdict's decision path is faked here:

* role/task profile: ``TaskRequirements.for_node`` (via ``DagRuntime``);
* admission/ranking: the real ``EligibilityLadder`` over a fixture inventory;
* failure policy: the real ``FailureIntelligence`` classifier;
* reviewer: the real ``OpenCodeReviewer`` selecting through the same ladder.

Only the process boundaries are fakes: the worker executor (returns scripted
terminals and writes the owned file), the health probe, and the ``ocr`` CLI
runner (writes a clean review and reports the ``--model`` it was given).
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from tests.test_orch_eligibility import FakeProbe, conn
from tests.test_orch_eligibility import row as inv_row
from tests.test_orch_runtime import NOW, Events, Executor, node
from verdict.orchestration.contracts import RunOutcome, WorkerTerminal, WorkGraph, route_family
from verdict.orchestration.eligibility import EligibilityLadder
from verdict.orchestration.recovery import FailureIntelligence
from verdict.orchestration.review import OcrRun, OpenCodeReviewer
from verdict.orchestration.runtime import DagRuntime, RuntimePolicy

CONTEXT_400 = "[kiro/claude-opus-5.5] [400]: Input is too long. (reset after 84h 5m 18s)"
CLEAN = (Path(__file__).parent / "fixtures" / "ocr" / "sample-clean.json").read_text()

INVENTORY = [
    inv_row("cc/claude-sonnet-4.6", owned_by="claude"),
    inv_row("cc/claude-sonnet-5", owned_by="claude"),
    inv_row("kr/claude-sonnet-5", owned_by="kiro"),
    inv_row("cx/gpt-5.4", owned_by="codex"),
]
CONNECTIONS = [conn("claude"), conn("kiro"), conn("codex")]


class ScriptedExecutor(Executor):
    """Worker boundary: scripted quota/context failures, otherwise real file writes."""

    def __init__(self, behaviour: dict[tuple[str, str], str]) -> None:
        super().__init__({k: v for k, v in behaviour.items() if v != "context"}, delay=0.01)
        self.context = {k for k, v in behaviour.items() if v == "context"}

    async def run(
        self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float
    ) -> WorkerTerminal:
        node_id = prompt.split(":", 1)[0]
        if (node_id, route_id) in self.context:
            self.calls.append((node_id, route_id))
            return WorkerTerminal(ok=False, model=route_id, status_code=400, error=CONTEXT_400)
        return await super().run(
            prompt, route_id=route_id, cwd=cwd, timeout_seconds=timeout_seconds
        )


class OcrCli:
    """``ocr`` process boundary: clean review, manifest reports the given --model."""

    def __init__(self) -> None:
        self.models: list[str] = []

    def __call__(self, argv: Sequence[str], *, env: Mapping[str, str], timeout: float) -> OcrRun:
        argv = list(argv)
        if "--version" in argv:
            return OcrRun(exit_code=0, stdout="open-code-review v1.12.9\n")
        model = argv[argv.index("--model") + 1]
        self.models.append(model)
        payload = json.loads(CLEAN)
        payload["manifest"]["execution"]["model"] = model
        out = Path(argv[argv.index("--output") + 1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload))
        return OcrRun(exit_code=0)


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


def _run(
    repo: Path, behaviour: dict[tuple[str, str], str]
) -> tuple[Any, Events, ScriptedExecutor, OcrCli, Path]:
    state = repo.parent / "ladder-state.json"
    ladder = EligibilityLadder(
        INVENTORY, CONNECTIONS, FakeProbe(), state, allow_unknown_capacity=True
    )
    events, executor, ocr = Events(), ScriptedExecutor(behaviour), OcrCli()
    reviewer = OpenCodeReviewer(
        ladder, api_key_env="TEST_OCR_KEY", out_dir=repo.parent / "review", runner=ocr
    )
    runtime = DagRuntime(
        repo=repo,
        run_dir=repo.parent / "run1",
        graph=WorkGraph("g", (node("a"),)),
        selector=ladder,
        executor=executor,
        classifier=FailureIntelligence(),
        events=events,
        prompt_for=lambda n, cwd: f"{n.node_id}: {n.objective}",
        reviewer=reviewer,
        policy=RuntimePolicy(),
        now=lambda: NOW,
    )
    import asyncio

    return asyncio.run(runtime.run()), events, executor, ocr, state


def test_provider_quota_failover_to_independent_review(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_OCR_KEY", "k")
    result, events, executor, ocr, state = _run(repo, {("a", "cc/claude-sonnet-4.6"): "quota"})

    assert result.outcome is RunOutcome.COMPLETE, result.reason
    # Ranking: cheapest sufficient in the preferred capacity/provider goes first.
    selections = [e["route_id"] for e in events.of("selection", "a")]
    assert selections == ["cc/claude-sonnet-4.6", "kr/claude-sonnet-5"]
    # Exact route ids at dispatch; the cooled sibling is never dispatched.
    assert [e["route_id"] for e in events.of("dispatch", "a")] == selections
    assert executor.calls == [("a", "cc/claude-sonnet-4.6"), ("a", "kr/claude-sonnet-5")]
    # Real FailureIntelligence: usage-limit 429 is provider-scoped quota exhaustion.
    failure = events.of("failure", "a")[0]
    assert (failure["category"], failure["action"]) == ("quota_exhausted", "REROUTE")
    cooldown = events.of("cooldown", "a")[0]
    assert (cooldown["key"], cooldown["scope"]) == ("claude", "pool")
    # The ladder persists the provider cooldown under the inventory owner.
    cooldowns = json.loads(state.read_text())["cooldowns"]
    assert "provider:claude" in cooldowns
    assert "pool:claude" in cooldowns
    reassign = events.of("reassign", "a")[0]
    assert (reassign["from_route"], reassign["to_route"]) == (
        "cc/claude-sonnet-4.6",
        "kr/claude-sonnet-5",
    )
    assert result.nodes["a"].route_id == "kr/claude-sonnet-5"
    # Reviewer: selected by the same ladder, excludes implementer route AND family,
    # and the observed OCR model equals the selected one.
    review = events.of("review")[0]
    assert review["status"] == "PASS"
    assert review["route_id"] == "cx/gpt-5.4"
    assert route_family(review["route_id"]) != route_family("kr/claude-sonnet-5")
    assert ocr.models == ["cx/gpt-5.4"]


def test_context_length_400_is_request_scoped(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_OCR_KEY", "k")
    result, events, executor, _, state = _run(repo, {("a", "cc/claude-sonnet-4.6"): "context"})

    assert result.outcome is RunOutcome.COMPLETE, result.reason
    failure = events.of("failure", "a")[0]
    assert failure["category"] == "context_length_exceeded"
    # No cooldown for the route or its provider, despite the 84h "reset" text.
    assert events.of("cooldown", "a") == []
    cooldowns = json.loads(state.read_text()).get("cooldowns", {}) if state.exists() else {}
    assert not any("claude" in key for key in cooldowns)
    # The healthy sibling on the SAME provider stays eligible and completes the node.
    assert executor.calls == [("a", "cc/claude-sonnet-4.6"), ("a", "cc/claude-sonnet-5")]
    assert result.nodes["a"].route_id == "cc/claude-sonnet-5"
