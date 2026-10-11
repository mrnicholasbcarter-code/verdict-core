"""Fixture-only regression evidence for the production planner hydration path."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from verdict.context_pack import ContextPlan, estimate_tokens
from verdict.context_sources import NativeCodeProvider, NativeGraphProvider
from verdict.orchestration.contracts import WorkNode
from verdict.orchestration.planner import hydrate_node_prompt
from verdict.orchestration.worker_context import build_worker_context


def _repo(tmp_path: Path) -> WorkNode:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "pkg/widget.py").write_text(
        "# unrelated padding\n" * 1500
        + "def compute_widget(value: int) -> int:\n    return value + 1\n"
    )
    (tmp_path / "pkg/caller.py").write_text(
        "from pkg.widget import compute_widget\ndef use_widget():\n    return compute_widget(2)\n"
    )
    (tmp_path / "tests/test_widget.py").write_text(
        "from pkg.widget import compute_widget\ndef test_compute_widget():\n    assert compute_widget(2) == 3\n"
    )
    (tmp_path / "docs/design.md").write_text(
        "# Design\ncompute_widget must preserve integer input and add one.\n"
    )
    return WorkNode(
        "widget",
        "Update compute_widget without changing its contract",
        owned_files=("pkg/widget.py",),
        required_context=("docs/design.md", "tests/test_widget.py"),
        verification_command=("python", "-m", "pytest", "tests/test_widget.py"),
        acceptance=("compute_widget integer contract remains valid",),
    )


def test_production_planner_prompt_has_target_contract_callers_tests_design(tmp_path: Path) -> None:
    node = _repo(tmp_path)
    prompt = hydrate_node_prompt(node, repo=tmp_path, goal="ship widget")
    for expected in (
        "WORKER_CONTEXT",
        "def compute_widget",
        "return compute_widget(2)",
        "def test_compute_widget",
        "must preserve integer input",
        "CITE:",
        "never route authorization",
        "conservative_unknown_candidate",
        "candidate=unknown",
    ):
        assert expected in prompt
    assert "# unrelated padding\n" * 100 not in prompt
    assert "Only edit files listed in OWNED_FILES" in prompt
    assert "RESULT: DONE" in prompt


def test_pack_has_bounded_exact_excerpt_provenance(tmp_path: Path) -> None:
    node = _repo(tmp_path)
    context = build_worker_context(node, repo=tmp_path)
    assert context.pack is not None
    for unit in context.pack.units:
        citation = json.loads(unit.content.splitlines()[0].removeprefix("CITE: "))
        assert citation["excerpt_digest"].startswith("sha256:")
        assert citation["revision"] == "unknown"
        assert citation["observed_at"]
        if unit.source_uri.startswith("code://"):
            continue
        span = citation["span"]
        if span is not None:
            source = (tmp_path / citation["source"]).read_text().splitlines()
            excerpt = "\n".join(unit.content.splitlines()[1:])
            assert "\n".join(source[span["start_line"] - 1 : span["end_line"]]) == excerpt


@pytest.mark.parametrize("byte_budget", [0, 100, 512, 1400, 4000, 60000])
def test_complete_context_projection_obeys_bytes_and_estimated_tokens(
    tmp_path: Path, byte_budget: int
) -> None:
    node = _repo(tmp_path)
    (tmp_path / "docs/design.md").write_text("compute_widget " + "é界" * 5000)
    result = build_worker_context(node, repo=tmp_path, max_context_bytes=byte_budget)
    assert len(result.text.encode()) <= byte_budget
    if result.text:
        assert estimate_tokens(result.text) <= min(4096, byte_budget // 4)
    assert result.budget_source == "conservative_unknown_candidate"


def test_candidate_reserves_and_mandatory_prompt_count_against_window(tmp_path: Path) -> None:
    node = _repo(tmp_path)
    plan = ContextPlan(
        "candidate",
        "fixture/model",
        token_budget=2200,
        output_token_reserve=400,
        tool_token_reserve=200,
        candidate_context_window=2000,
    )
    prompt = hydrate_node_prompt(node, repo=tmp_path, goal="ship widget", context_plan=plan)
    assert "candidate_plan" in prompt
    assert "fixture/model" in prompt
    assert estimate_tokens(prompt) + 400 + 200 <= 2000
    context = build_worker_context(
        node, repo=tmp_path, context_plan=plan, mandatory_prompt_tokens=300
    )
    assert estimate_tokens(context.text) <= 1100


def test_parser_outage_keeps_native_required_contract_and_explicit_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    node = _repo(tmp_path)

    def broken(self, **kwargs):
        raise RuntimeError("password=do-not-render-exception")

    monkeypatch.setattr(NativeGraphProvider, "provide", broken)
    monkeypatch.setattr(NativeCodeProvider, "provide", broken)
    context = build_worker_context(node, repo=tmp_path)
    assert "must preserve integer input" in context.text
    assert any("retrieval_error" in reason for reason in context.omissions)
    assert "do-not-render-exception" not in context.text


def test_query_failure_falls_back_to_required_excerpts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from verdict.orchestration import worker_context

    node = _repo(tmp_path)

    def broken(*args, **kwargs):
        raise OSError("sensitive backend path")

    monkeypatch.setattr(worker_context, "execute_context_query", broken)
    context = build_worker_context(node, repo=tmp_path)
    assert "must preserve integer input" in context.text
    assert "native_query_error:required_excerpts_fallback" in context.omissions
    assert "sensitive backend path" not in context.text


@pytest.mark.parametrize(
    "secret",
    [
        "api_key = 'fixture-canary'",
        "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz012345",
        "https://user:fixture-canary@example.invalid",
    ],
)
def test_secrets_are_rejected_before_slicing(tmp_path: Path, secret: str) -> None:
    node = _repo(tmp_path)
    (tmp_path / "pkg/widget.py").write_text(
        "def compute_widget(v):\n    return v\n" + "# harmless\n" * 400 + "# " + secret
    )
    context = build_worker_context(node, repo=tmp_path)
    assert secret not in context.text
    assert not any(u.source_uri == "pkg/widget.py" for u in context.pack.units)


def test_no_transcript_dotfile_or_outside_symlink_content(tmp_path: Path) -> None:
    node = _repo(tmp_path)
    external = tmp_path.parent / "outside.py"
    external.write_text("def compute_widget():\n    return 'OUTSIDE_CANARY'\n")
    (tmp_path / "pkg/link.py").symlink_to(external)
    (tmp_path / "docs/transcript.md").write_text("compute_widget RAW_TRANSCRIPT_CANARY")
    (tmp_path / ".env").write_text("compute_widget DOTFILE_CANARY")
    node = WorkNode(
        "widget",
        node.objective,
        owned_files=node.owned_files,
        required_context=("pkg/link.py", "docs/transcript.md", ".env"),
        verification_command=("true",),
    )
    prompt = hydrate_node_prompt(node, repo=tmp_path, goal="g")
    assert all(
        value not in prompt
        for value in ("OUTSIDE_CANARY", "RAW_TRANSCRIPT_CANARY", "DOTFILE_CANARY")
    )


def test_registry_optional_tools_stay_unavailable_and_memory_is_not_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from verdict.capability_registry import build_default_registry
    from verdict.memory_gate import MemoryGate

    node = _repo(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("worker pack must not open durable memory")

    monkeypatch.setattr(MemoryGate, "__init__", forbidden)
    before = build_default_registry().resolve("code.references")
    prompt = hydrate_node_prompt(node, repo=tmp_path, goal="g")
    after = build_default_registry().resolve("code.references")
    assert before.selected is not None and after.selected is not None
    assert before.selected.provider_id == after.selected.provider_id

    assert "def compute_widget" in prompt
    assert "adapter.serena" not in prompt
    health = build_default_registry().health_report()
    assert "adapter.serena_lsp" in health["by_health"]["unavailable"]


@pytest.mark.asyncio
async def test_runtime_dispatches_planner_native_pack_to_scripted_worker(tmp_path: Path) -> None:
    import subprocess

    from verdict.orchestration.contracts import (
        CapacityClass,
        EligibilityStage,
        RouteVerdict,
        RunOutcome,
        WorkerTerminal,
        WorkGraph,
    )
    from verdict.orchestration.recovery import FailureIntelligence
    from verdict.orchestration.runtime import DagRuntime, RuntimePolicy

    repo = tmp_path / "repo"
    repo.mkdir()
    node = _repo(repo)
    # Fixture repository and scripted worker only; this is not live proof.
    for argv in (
        ["git", "init", "-q"],
        ["git", "config", "user.name", "fixture"],
        ["git", "config", "user.email", "fixture@example.invalid"],
        ["git", "add", "."],
        ["git", "commit", "-qm", "fixture"],
    ):
        subprocess.run(argv, cwd=repo, check=True)
    node = WorkNode(
        "widget",
        node.objective,
        owned_files=("pkg/widget.py",),
        required_context=node.required_context,
        verification_command=("true",),
    )
    prompts = []

    class Selector:
        def select(self, requirements, *, now):
            route = RouteVerdict(
                "fixture/model",
                "fixture",
                EligibilityStage.SELECTED,
                None,
                "fixture",
                CapacityClass.FREE,
            )
            return route, (route,)

        def evaluate(self, requirements, *, now):
            return self.select(requirements, now=now)[1]

        def record_success(self, *args, **kwargs):
            pass

    class Events:
        def emit(self, *args, **kwargs):
            pass

    class Executor:
        async def run(self, prompt, *, route_id, cwd, timeout_seconds):
            prompts.append(prompt)
            (cwd / "pkg/widget.py").write_text(
                "def compute_widget(value: int) -> int:\n    return value + 1\n"
            )
            return WorkerTerminal(
                ok=True, output="RESULT: DONE", model=route_id, executor_kind="scripted"
            )

    def prompt_for(n, cwd, *, context_budget_bytes=60_000):
        return hydrate_node_prompt(
            n, repo=cwd, goal="ship widget", max_context_bytes=context_budget_bytes
        )

    runtime = DagRuntime(
        repo=repo,
        run_dir=tmp_path / "run",
        graph=WorkGraph("ship widget", (node,)),
        selector=Selector(),
        executor=Executor(),
        classifier=FailureIntelligence(),
        events=Events(),
        prompt_for=prompt_for,
        policy=RuntimePolicy(require_review=False, run_repo_gates=False),
    )
    result = await runtime.run()
    assert result.outcome is RunOutcome.COMPLETE, result.reason
    assert len(prompts) == 1
    assert "WORKER_CONTEXT" in prompts[0]
    assert "must preserve integer input" in prompts[0]
    assert "def compute_widget" in prompts[0]
