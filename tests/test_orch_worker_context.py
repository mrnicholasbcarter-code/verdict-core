"""Fixture-only regression evidence for the production planner hydration path."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

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


def test_registry_optional_tools_stay_unavailable_and_memory_is_never_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from verdict.capability_registry import build_default_registry
    from verdict.memory_gate import MemoryGate

    node = _repo(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("worker pack must not write durable memory")

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


@pytest.mark.parametrize(
    "kind",
    [
        "private",
        "private_directory",
        "transcript_file",
        "transcript_directory",
        "symlink",
        "secret",
    ],
)
def test_native_aggregate_members_are_filtered_before_graph_and_symbols(
    tmp_path: Path, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from verdict.code_graph import CodeGraphEngine
    from verdict.context_sources import _extract_definitions

    node = _repo(tmp_path)
    marker = "UNSAFE_AGGREGATE_CANARY"
    content = f"def compute_widget():\n    return 9\ndef test_compute_widget_{marker}():\n    return compute_widget()\n"
    if kind == "private":
        unsafe = tmp_path / ".private/test_hidden.py"
    elif kind == "private_directory":
        unsafe = tmp_path / "private/test_hidden.py"
    elif kind == "transcript_file":
        unsafe = tmp_path / "docs/transcript.py"
    elif kind == "transcript_directory":
        unsafe = tmp_path / "transcripts/raw.py"
    elif kind == "symlink":
        unsafe = tmp_path / "pkg/outside_link.py"
    else:
        unsafe = tmp_path / "pkg/sensitive.py"
        # Full-source policy must reject even credentials beyond all excerpt caps.
        content += "# harmless\n" * 1500 + "# api_key = 'fixture-credential-canary'\n"
    unsafe.parent.mkdir(parents=True, exist_ok=True)
    if kind == "symlink":
        outside = tmp_path.parent / "outside.py"
        outside.write_text(content)
        unsafe.symlink_to(outside)
    else:
        unsafe.write_text(content)
    parsed = []
    original_parse = CodeGraphEngine.parse_file

    def parse(self, path, root):
        parsed.append(path)
        return original_parse(self, path, root)

    monkeypatch.setattr(CodeGraphEngine, "parse_file", parse)
    definition_sources = []

    def definitions(path, root, symbols):
        definition_sources.append(path)
        return _extract_definitions(path, root, symbols)

    from verdict import context_sources

    monkeypatch.setattr(context_sources, "_extract_definitions", definitions)
    context = build_worker_context(node, repo=tmp_path)
    assert marker not in context.text
    assert unsafe not in parsed and unsafe not in definition_sources
    assert any("excluded_before_aggregation" in reason for reason in context.omissions)
    assert "def compute_widget" in context.text
    assert "test_compute_widget" in context.text
    # Aggregates are assembled only after source policy ran, not output masking.
    aggregates = [unit for unit in context.pack.units if unit.source_uri.startswith("code://")]
    assert aggregates
    assert all("source-filtered" in unit.transform_lineage for unit in aggregates)


def test_all_transcript_and_private_path_components_are_rejected(tmp_path: Path) -> None:
    node = _repo(tmp_path)
    (tmp_path / "transcripts").mkdir()
    (tmp_path / "transcripts/raw.md").write_text("compute_widget RAW_TRANSCRIPT_DIRECTORY_CANARY")
    (tmp_path / "sessions").mkdir()
    (tmp_path / "sessions/raw.md").write_text("compute_widget RAW_SESSION_DIRECTORY_CANARY")
    # Lexical aliases cannot hide forbidden source identity.
    (tmp_path / "docs/safe_alias.md").symlink_to(tmp_path / "transcripts/raw.md")
    node = WorkNode(
        "widget",
        node.objective,
        owned_files=node.owned_files,
        required_context=("transcripts/raw.md", "sessions/raw.md", "docs/safe_alias.md"),
        verification_command=("true",),
    )
    context = build_worker_context(node, repo=tmp_path)
    assert "RAW_TRANSCRIPT_DIRECTORY_CANARY" not in context.text
    assert "RAW_SESSION_DIRECTORY_CANARY" not in context.text
    assert "unsafe_or_unsupported_source" in context.omissions


def test_unfiltered_aggregate_is_rejected_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from verdict import context_sources

    node = _repo(tmp_path)
    original = context_sources.NativeCodeProvider.provide

    def unfiltered(self, **kwargs):
        result = original(self, **kwargs)
        if kwargs["capability_id"] == "code.symbols":
            return replace(
                result,
                units=tuple(replace(unit, transform_lineage=("raw",)) for unit in result.units),
            )
        return result

    monkeypatch.setattr(context_sources.NativeCodeProvider, "provide", unfiltered)
    context = build_worker_context(node, repo=tmp_path)
    assert not any(unit.source_uri == "code://symbols" for unit in context.pack.units)
    assert "unsafe_or_sensitive_retrieval_excluded" in context.omissions


@pytest.mark.parametrize("provider_kind", ["symbols", "graph", "docs"])
def test_native_provider_source_filter_precedes_discovery_reads_and_aggregation(
    tmp_path: Path, provider_kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from verdict.context_sources import NativeCodeProvider, NativeDocsProvider, NativeGraphProvider

    node = _repo(tmp_path)
    bad = tmp_path / "pkg/blocked.py"
    bad.write_text("def test_compute_widget_BLOCKED_CANARY():\n    return compute_widget()\n")
    bad_doc = tmp_path / "docs/blocked.md"
    bad_doc.write_text("compute_widget BLOCKED_DOC_CANARY")
    checked = []

    def source_filter(path):
        checked.append(path)
        return path not in {bad, bad_doc}

    original_read = Path.read_text

    def read(path, *args, **kwargs):
        if path in {bad, bad_doc}:
            raise AssertionError("provider read source before source filter")
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    provider = {
        "symbols": NativeCodeProvider,
        "graph": NativeGraphProvider,
        "docs": NativeDocsProvider,
    }[provider_kind]()
    capability = {"symbols": "code.symbols", "graph": "tests.impact", "docs": "docs.project"}[
        provider_kind
    ]
    result = provider.provide(
        capability_id=capability,
        query=node.objective,
        repo_root=tmp_path,
        hints={"symbols": ("compute_widget",), "source_filter": source_filter},
    )
    assert result.units
    assert all(
        "BLOCKED_CANARY" not in unit.content and "BLOCKED_DOC_CANARY" not in unit.content
        for unit in result.units
    )
    assert (bad_doc if provider_kind == "docs" else bad) in checked


def test_arbitrary_provider_cannot_claim_filtered_aggregate_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from verdict.context_intelligence import NativeCapabilityResolver
    from verdict.context_sources import CapabilityProvider, ProviderResult, make_unit

    node = _repo(tmp_path)

    class PretendProvider(CapabilityProvider):
        def provide(self, **kwargs):
            unit = make_unit(
                slot_type="evidence",
                key="pretend",
                content="UNTRUSTED_AGGREGATE_CANARY",
                source_uri="code://symbols",
                authority="pretend",
                transform_lineage=("raw", "source-filtered"),
            )
            return ProviderResult(kwargs["capability_id"], self.provider_id, (unit,))

    original = NativeCapabilityResolver.resolve_all

    def resolve(self, capability):
        if capability == "code.symbols":
            return (PretendProvider("pretend", frozenset({"code.symbols"})),)
        return original(self, capability)

    monkeypatch.setattr(NativeCapabilityResolver, "resolve_all", resolve)
    context = build_worker_context(node, repo=tmp_path)
    assert "UNTRUSTED_AGGREGATE_CANARY" not in context.text


def _plane(home: Path) -> Path:
    from verdict.memory_gate import MemoryGate, MemoryWriteRequest
    from verdict.memory_plane import MemoryPlane, MemoryRecord

    path = home / ".verdict" / "memory.db"
    with MemoryPlane(path) as plane:
        gate = MemoryGate(plane)
        for namespace, key, value, extra in (
            ("decisions", "widget-contract", "compute_widget keeps integer input; ADR-7.", {}),
            ("feedback", "widget-style", "compute_widget: no float coercion, operator 10-09.", {}),
            ("decisions", "widget-secret", "compute_widget", {"sensitivity": "confidential"}),
            ("patterns", "unrelated", "Rotate the dashboard colours weekly.", {}),
        ):
            result = gate.write(
                MemoryWriteRequest(
                    namespace,
                    key,
                    value,
                    "human" if namespace == "feedback" else "verdict-core",
                    provenance={"source": "fixture"},
                    **extra,
                )
            )
            assert result.allowed, result.reason
        # Not gate-verified, or not a fact namespace: never recalled.
        plane.put(MemoryRecord("raw", "decisions", "raw", "compute_widget unverified", "cli"))
        plane.put(
            MemoryRecord(
                "hist",
                "session_history",
                "s",
                "compute_widget transcript",
                "hook",
                authority_verified=True,
            )
        )
    return path


def test_worker_pack_recalls_gated_facts_with_citations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from verdict.memory_plane import MemoryPlane

    home = Path(os.environ["HOME"])
    path = _plane(home)
    node = _repo(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("worker pack must not write durable memory")

    monkeypatch.setattr(MemoryPlane, "put", forbidden)
    prompt = hydrate_node_prompt(node, repo=tmp_path, goal="ship widget", structured=True)
    text = prompt.text
    assert "compute_widget keeps integer input; ADR-7." in text
    assert "no float coercion, operator 10-09" in text
    for absent in ("unverified", "transcript", "Rotate the dashboard"):
        assert absent not in text
    assert text.count("compute_widget") > 0 and "widget-secret" not in text

    recalled = [s for s in prompt.evidence()["sources"] if s["path"].startswith("memory:gate_")]
    assert {s["authority"] for s in recalled} == {"verdict-core", "human"}
    assert all("memory-search" in s["transform_lineage"] for s in recalled)
    cited = [line for line in text.splitlines() if '"source": "memory:gate_' in line]
    assert len(cited) == 2 and all('"excerpt_digest": "sha256:' in line for line in cited)

    pack = prompt.context.pack
    assert pack is not None
    kinds = [unit.slot_type for unit in pack.units]
    assert kinds.index("memory") > kinds.index("evidence")  # code evidence first
    assert path.exists()


def test_worker_pack_recalls_gated_improvement_lesson(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from verdict.memory_gate import MemoryGate, MemoryWriteRequest

    memory_path = tmp_path / "memory.db"
    monkeypatch.setenv("VERDICT_MEMORY_PLANE_PATH", str(memory_path))
    with MemoryGate(db_path=memory_path) as gate:
        result = gate.write(
            MemoryWriteRequest(
                namespace="lessons",
                key="improve:c1",
                value={
                    "lesson": "compute_widget must keep each node focused and independently verified",
                    "layer": "planning",
                },
                authority="verdict-core",
                provenance={"cycle_id": "c1", "receipt": "/tmp/c1/receipt.json"},
            )
        )
    assert result.allowed
    node = _repo(tmp_path)
    prompt = hydrate_node_prompt(node, repo=tmp_path, goal="ship widget", structured=True)
    assert "compute_widget must keep each node focused" in prompt.text
    assert any(source["path"].startswith("memory:gate_") for source in prompt.evidence()["sources"])


def test_missing_or_broken_memory_plane_is_a_named_gap(tmp_path: Path) -> None:
    node = _repo(tmp_path)
    plane = Path(os.environ["HOME"]) / ".verdict" / "memory.db"

    context = build_worker_context(node, repo=tmp_path)
    assert "memory.search:no_default_location" in context.omissions
    assert not plane.exists()  # never created by a worker pack

    plane.parent.mkdir(parents=True)
    plane.write_text("not a sqlite database")
    context = build_worker_context(node, repo=tmp_path)
    assert "memory.search:memory_plane_unavailable" in context.omissions
    assert "must preserve integer input" in context.text


def test_recalled_fact_is_redacted_before_rendering(tmp_path: Path) -> None:
    from verdict.memory_plane import MemoryPlane, MemoryRecord

    key = "ghp_" + "Z" * 30
    path = Path(os.environ["HOME"]) / ".verdict" / "memory.db"
    with MemoryPlane(path) as plane:
        plane.put_verified(
            MemoryRecord(
                "gate_secret",
                "decisions",
                "k",
                f"compute_widget deploy uses {key}",
                "fixture",
                authority="verdict-core",
                authority_verified=True,
                sensitivity="standard",
            )
        )
    context = build_worker_context(_repo(tmp_path), repo=tmp_path)
    assert key not in context.text
    assert "compute_widget deploy uses [redacted]" in context.text


def _db_state(path: Path) -> dict[str, tuple[bytes, int]]:
    """Every file next to the plane (sidecars included) with its bytes and mtime."""
    return {
        item.name: (item.read_bytes(), item.stat().st_mtime_ns)
        for item in sorted(path.parent.iterdir())
    }


@pytest.mark.parametrize("kind", ["plane", "unrelated"])
def test_recall_never_writes_or_migrates_the_database(tmp_path: Path, kind: str) -> None:
    import sqlite3

    home = Path(os.environ["HOME"])
    if kind == "plane":
        path = _plane(home)
    else:
        path = home / ".verdict" / "memory.db"
        path.parent.mkdir(parents=True)
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE unrelated(a)")
    before = _db_state(path)
    context = build_worker_context(_repo(tmp_path), repo=tmp_path)
    assert _db_state(path) == before
    if kind == "unrelated":
        assert "memory.search:memory_plane_unavailable" in context.omissions


@pytest.mark.parametrize("field", ["trust", "source", "tags", "provenance"])
def test_secrets_in_any_recalled_record_field_never_reach_the_prompt(
    tmp_path: Path, field: str
) -> None:
    from verdict.memory_gate import MemoryGate, MemoryWriteRequest
    from verdict.memory_plane import MemoryPlane

    token = "oma_" + "live_" + "Q7rT2mWp7LzN4bYc8hJdQa1Ws2Ed3Rf4Tg5Yh6Uj7Ik"
    values: dict[str, object] = {
        "trust": token,
        "source": token,
        "tags": (token,),
        "provenance": {"source": "fixture", "note": token},
    }
    path = Path(os.environ["HOME"]) / ".verdict" / "memory.db"
    with MemoryPlane(path) as plane:
        request = MemoryWriteRequest(
            "decisions",
            "widget-decision",
            "compute_widget decision recorded for review",
            "verdict-core",
            provenance={"source": "fixture"},
        )
        request = replace(request, **{field: values[field]})
        assert MemoryGate(plane).write(request).allowed
    prompt = hydrate_node_prompt(_repo(tmp_path), repo=tmp_path, goal="g", structured=True)
    assert "compute_widget decision recorded for review" in prompt.text
    assert token not in prompt.text
    assert token not in json.dumps(prompt.evidence())


def test_a_plane_with_pending_wal_frames_is_busy_and_untouched(tmp_path: Path) -> None:
    from verdict.memory_plane import MemoryPlane, MemoryRecord

    path = _plane(Path(os.environ["HOME"]))
    writer = MemoryPlane(path)  # a live writer keeps committed frames in the WAL
    writer._db.execute("PRAGMA wal_autocheckpoint=0")
    writer.put(MemoryRecord("w1", "decisions", "w1", "compute_widget pending", "fixture"))
    try:
        before = _db_state(path)
        context = build_worker_context(_repo(tmp_path), repo=tmp_path)
        assert _db_state(path) == before
        assert "memory.search:memory_plane_busy" in context.omissions
        assert "must preserve integer input" in context.text
    finally:
        writer.close()


def _commit_now(path: Path, record_id: str) -> None:
    import sqlite3

    # A raw writer, so the test can patch the reader's sqlite3.connect.
    writer = _REAL_CONNECT(path, isolation_level=None)
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute(
        "UPDATE memories SET updated_at = updated_at + 1 WHERE record_id = "
        "(SELECT record_id FROM memories LIMIT 1)"
    )
    assert isinstance(writer, sqlite3.Connection)
    # Left open on purpose: its committed frames stay in the WAL during the read.
    _OPEN_WRITERS.append(writer)


_OPEN_WRITERS: list[object] = []
_REAL_CONNECT = __import__("sqlite3").connect


@pytest.fixture(autouse=True)
def _close_writers() -> Any:
    yield
    while _OPEN_WRITERS:
        _OPEN_WRITERS.pop().close()  # type: ignore[attr-defined]


def test_commit_between_check_and_open_is_busy_not_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    from verdict import memory_plane
    from verdict.memory_plane import MemoryPlane, WalPendingError

    path = _plane(Path(os.environ["HOME"]))
    real_connect = sqlite3.connect

    def connect_after_a_commit(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        _commit_now(path, "raced")
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(memory_plane.sqlite3, "connect", connect_after_a_commit)
    with pytest.raises(WalPendingError):
        MemoryPlane.open_readonly(path)


def test_commit_during_the_read_drops_recalled_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from verdict.context_sources import NativeMemoryProvider

    path = _plane(Path(os.environ["HOME"]))
    real_provide = NativeMemoryProvider.provide

    def provide_then_commit(self: NativeMemoryProvider, **kwargs: Any) -> Any:
        result = real_provide(self, **kwargs)
        _commit_now(path, "during")
        return result

    monkeypatch.setattr(NativeMemoryProvider, "provide", provide_then_commit)
    context = build_worker_context(_repo(tmp_path), repo=tmp_path)
    assert "memory.search:memory_plane_busy" in context.omissions
    assert "compute_widget keeps integer input" not in context.text
    assert "must preserve integer input" in context.text
