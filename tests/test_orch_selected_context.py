"""Selected worker-context bridge: real DAG/git, scripted workers only."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import replace
from typing import Any

import pytest

from verdict.context_pack import ContextPlan, estimate_tokens
from verdict.orchestration.contracts import (
    CapacityClass,
    EligibilityStage,
    RouteVerdict,
    RunOutcome,
    WorkerTerminal,
    WorkGraph,
    WorkNode,
)
from verdict.orchestration.planner import hydrate_node_prompt
from verdict.orchestration.recovery import FailureIntelligence
from verdict.orchestration.runtime import DagRuntime, RuntimePolicy
from verdict.orchestration.worker_context import WorkerPrompt


class Selector:
    def __init__(self, window: int | None):
        self.window = window
        self.failures = []

    def select(self, requirements, *, now):
        selected = RouteVerdict(
            "fixture/selected",
            "fixture",
            EligibilityStage.SELECTED,
            None,
            "fixture",
            CapacityClass.FREE,
            context_window=self.window,
        )
        # Unselected candidate's large window must never leak into the plan.
        other = replace(selected, route_id="fixture/other", context_window=200_000)
        return selected, (other, selected)

    def evaluate(self, requirements, *, now):
        return self.select(requirements, now=now)[1]

    def record_success(self, *args, **kwargs):
        pass

    def record_failure(self, *args, **kwargs):
        self.failures.append((args, kwargs))


class Events:
    def __init__(self):
        self.rows: list[dict[str, Any]] = []

    def emit(self, type, node_id="", **data):
        json.dumps(data)
        self.rows.append({"type": type, "node_id": node_id, **data})

    def of(self, type):
        return [r for r in self.rows if r["type"] == type]


class Executor:
    def __init__(self, *, overflow=False):
        self.prompts = []
        self.overflow = overflow

    async def run(self, prompt, *, route_id, cwd, timeout_seconds):
        self.prompts.append(prompt)
        if self.overflow and len(self.prompts) == 1:
            return WorkerTerminal(
                ok=False,
                model=route_id,
                status_code=400,
                error="maximum context length exceeded",
                executor_kind="scripted",
            )
        (cwd / "widget.py").write_text("def compute_widget():\n    return 2\n")
        return WorkerTerminal(
            ok=True, output="RESULT: DONE", model=route_id, executor_kind="scripted"
        )


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "widget.py").write_text("# padding\n" * 800 + "def compute_widget():\n    return 1\n")
    (root / "design.md").write_text("compute_widget must preserve the widget contract.\n")
    (root / "transcript.md").write_text("TRANSCRIPT_MUST_NOT_APPEAR")
    (root / "secret.py").write_text("def compute_widget_secret(): pass\n# api_key = 'canary'\n")
    for args in (
        ["init", "-q"],
        ["config", "user.name", "fixture"],
        ["config", "user.email", "fixture@example.invalid"],
        ["add", "."],
        ["commit", "-qm", "fixture"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True)
    return root


def make(repo, window, *, callback=None, overflow=False, **policy):
    node = WorkNode(
        "widget",
        "Update compute_widget",
        owned_files=("widget.py",),
        required_context=("design.md", "transcript.md", "secret.py"),
        acceptance=("preserve contract",),
        verification_command=("true",),
    )
    results = []

    def prompt_for(n, cwd, *, context_budget_bytes=60_000, context_plan=None, mandatory_suffix=""):
        result = hydrate_node_prompt(
            n,
            repo=cwd,
            goal="ship widget",
            max_context_bytes=context_budget_bytes,
            context_plan=context_plan,
            mandatory_suffix=mandatory_suffix,
            structured=True,
        )
        results.append(result)
        return result

    events, selector, executor = Events(), Selector(window), Executor(overflow=overflow)
    runtime = DagRuntime(
        repo=repo,
        run_dir=repo.parent / "run",
        graph=WorkGraph("ship widget", (node,)),
        selector=selector,
        executor=executor,
        classifier=FailureIntelligence(),
        events=events,
        prompt_for=callback or prompt_for,
        policy=RuntimePolicy(require_review=False, run_repo_gates=False, **policy),
    )
    return runtime, events, selector, executor, results


@pytest.mark.parametrize("window", [2600, None])
async def test_selected_window_and_actual_pack_sources(repo, window):
    rt, ev, selector, ex, results = make(repo, window)
    outcome = await rt.run()
    assert outcome.outcome is RunOutcome.COMPLETE, outcome.reason
    row = ev.of("hydrate")[0]
    assert row["context_plan"]["candidate_id"] == "fixture/selected"
    assert row["context_plan"]["candidate_context_window"] == window
    assert row["budget_source"] == (
        "candidate_plan" if window else "conservative_unknown_candidate"
    )
    assert row["route_id"] == "fixture/selected"
    assert row["prompt_tokens_measured"] is None
    assert row["tokenizer"] == "unknown:offline_four_char_estimate"
    assert row["prompt_tokens_estimated"] == estimate_tokens(ex.prompts[0])
    assert row["prompt_tokens_estimated"] <= row["input_token_budget"]
    assert row["sources_status"] == "actual_selected_pack"
    context = results[0].context
    assert context.pack is not None
    assert {s["unit_id"] for s in row["sources"]} == {u.unit_id for u in context.pack.units}
    for source, unit, slot in zip(
        row["sources"], context.pack.units, context.pack.slots, strict=True
    ):
        assert source["excerpt_digest"] == unit.source_digest
        assert source["span"] == unit.span
        assert source["revision"] == "unknown"
        assert (
            source["rendered_content_digest"]
            == "sha256:" + hashlib.sha256(slot.content.encode()).hexdigest()
        )
        assert source["path"] not in {"secret.py", "transcript.md"}
        assert unit.source_uri in ex.prompts[0]
    assert row["omissions"]
    assert not selector.failures


async def test_tiny_window_does_not_launch_or_cool_provider(repo):
    rt, ev, selector, ex, _ = make(repo, 100)
    result = await rt.run()
    assert result.outcome is RunOutcome.BLOCKED
    assert ex.prompts == []
    assert not selector.failures
    row = ev.of("hydrate")[0]
    assert row["estimated_fit"] is False
    assert row["input_token_budget"] == 0
    assert row["sources"] == []
    failure = ev.of("failure")[0]
    assert failure["category"] == "context_input_rejected"
    assert failure["action"] == "FAIL_CLOSED"


async def test_overflow_repack_reuses_exact_route_window_and_shrinks_full_input(repo):
    rt, ev, _, ex, results = make(
        repo, 9000, overflow=True, context_budget_bytes=8000, min_context_budget_bytes=4000
    )
    result = await rt.run()
    assert result.outcome is RunOutcome.COMPLETE, result.reason
    rows = ev.of("hydrate")
    assert len(ex.prompts) == len(rows) == 2
    assert [r["budget_bytes"] for r in rows] == [8000, 4000]
    assert [r["input_token_budget"] for r in rows] == [2000, 1000]
    for row, prompt, hydrated in zip(rows, ex.prompts, results, strict=True):
        assert row["route_id"] == "fixture/selected"
        assert row["context_plan"]["candidate_context_window"] == 9000
        assert estimate_tokens(prompt) <= row["input_token_budget"]
        assert len(prompt.encode()) <= row["budget_bytes"]
        assert len(hydrated.context.text.encode()) <= row["budget_bytes"]
        assert row["sources"] == hydrated.evidence()["sources"]
    assert len(ex.prompts[1]) <= len(ex.prompts[0])
    assert ev.of("repack")


async def test_retry_suffix_counted_before_pack(repo):
    rt, ev, _, ex, results = make(repo, 3000)
    run = rt.nodes["widget"]
    from verdict.orchestration.contracts import FailureClassification

    run.failures.append(
        FailureClassification("verification_failed", "CORRECT_IMPLEMENTATION", 0, "none", "fixture")
    )
    run.failure_feedback = "verification detail " * 100
    result = await rt.run()
    assert result.outcome is RunOutcome.COMPLETE, result.reason
    row = ev.of("hydrate")[0]
    assert "PREVIOUS_ATTEMPT_FAILED_VERIFICATION" in ex.prompts[0]
    assert row["mandatory_prompt_tokens_estimated"] > 650
    assert estimate_tokens(ex.prompts[0]) <= row["input_token_budget"]
    assert row["mandatory_prompt_tokens_estimated"] == results[0].mandatory_prompt_tokens


@pytest.mark.parametrize("kind", ["positional", "budget", "plan", "kwargs"])
async def test_legacy_callback_signatures_are_optional_and_provenance_unknown(repo, kind):
    observed = []

    def positional(n, cwd):
        return "short scripted prompt"

    def budget(n, cwd, *, context_budget_bytes):
        observed.append(context_budget_bytes)
        return "short scripted prompt"

    def plan(n, cwd, *, context_plan):
        observed.append(context_plan)
        return "short scripted prompt"

    def kwargs(n, cwd, **values):
        observed.append(values)
        return "short scripted prompt"

    callback = {"positional": positional, "budget": budget, "plan": plan, "kwargs": kwargs}[kind]
    rt, ev, _, _ex, _ = make(repo, None, callback=callback)
    result = await rt.run()
    assert result.outcome is RunOutcome.COMPLETE, result.reason
    row = ev.of("hydrate")[0]
    assert row["sources"] == []
    assert row["sources_status"] == "unknown_legacy_callback"
    assert "pack_id" not in row
    if kind == "budget":
        assert observed == [60_000]
    if kind == "plan":
        assert isinstance(observed[0], ContextPlan)
    if kind == "kwargs":
        assert observed[0]["context_plan"].candidate_id == "fixture/selected"


def test_default_prompt_remains_string(repo):
    node = WorkNode(
        "widget",
        "compute_widget",
        owned_files=("widget.py",),
        required_context=("design.md",),
        verification_command=("true",),
    )
    assert isinstance(hydrate_node_prompt(node, repo=repo, goal="g"), str)
    assert isinstance(hydrate_node_prompt(node, repo=repo, goal="g", structured=True), WorkerPrompt)


async def test_full_prompt_byte_ceiling_includes_mandatory_text(repo):
    rt, ev, selector, ex, _ = make(repo, 30_000, context_budget_bytes=800)
    result = await rt.run()
    assert result.outcome is RunOutcome.BLOCKED
    assert ex.prompts == []
    assert ev.of("hydrate")[0]["prompt_bytes"] > 800
    assert ev.of("hydrate")[0]["sources"] == []
    assert not selector.failures


async def test_string_callback_without_suffix_parameter_gets_retry_feedback(repo):
    from verdict.orchestration.contracts import FailureClassification

    rt, ev, _, ex, _ = make(repo, None, callback=lambda n, cwd: "scripted prompt")
    run = rt.nodes["widget"]
    run.failures.append(
        FailureClassification("verification_failed", "CORRECT_IMPLEMENTATION", 0, "none", "fixture")
    )
    run.failure_feedback = "fixture verification canary"
    result = await rt.run()
    assert result.outcome is RunOutcome.COMPLETE, result.reason
    assert "fixture verification canary" in ex.prompts[0]
    assert "PREVIOUS_ATTEMPT_FAILED_VERIFICATION" in ex.prompts[0]
    assert ev.of("hydrate")[0]["prompt_tokens_estimated"] == estimate_tokens(ex.prompts[0])
    assert ev.of("hydrate")[0]["sources_status"] == "unknown_legacy_callback"
