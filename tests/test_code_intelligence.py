"""Probes and cited code-intelligence facts for context packs.

Unit tests script the subprocess runner. They do not call the installed
binaries. The one live test skips when ``codebase-memory-mcp`` is absent.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import verdict.code_intelligence as code_intelligence
from verdict.capability_registry import build_default_registry, probe_and_register
from verdict.code_intelligence import (
    CommandResult,
    code_intelligence_doctor_section,
    compile_code_intelligence_pack,
    facts_to_context_units,
    probe_code_intelligence,
    retrieve_code_facts,
)

_READY_INDEX = {
    "project": "proj",
    "nodes": 3,
    "edges": 4,
    "status": "ready",
    "indexed_at": "2026-11-12T00:00:00Z",
}


def _git_repo(path: Path, message: str = "init") -> str:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "dev@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "dev"], cwd=path, check=True)
    (path / "readme.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "readme.txt"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=path, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    return head.stdout.strip()


def _which_present(name: str) -> str:
    return f"/usr/bin/{name}"


def _probe_runner(repo: Path, *, serena_ok: bool = True):
    real = code_intelligence.run_command

    def runner(argv: list[str], **kwargs: object) -> CommandResult:
        if argv and argv[0] == "git":
            return real(argv, cwd=kwargs.get("cwd") if isinstance(kwargs.get("cwd"), Path) else None)
        if "health-check" in argv:
            raise AssertionError("serena project health-check must not run in a probe")
        joined = " ".join(argv)
        if argv[:2] == ["serena", "--version"]:
            if not serena_ok:
                return CommandResult(1, "", "serena missing")
            return CommandResult(0, "Serena 2.1.0\n", "")
        if "list_projects" in joined:
            payload = {
                "projects": [{"name": "proj", "root_path": str(repo)}],
                "has_more": False,
            }
            return CommandResult(0, json.dumps(payload), "")
        if "index_status" in joined:
            payload = {**_READY_INDEX, "root_path": str(repo)}
            return CommandResult(0, json.dumps(payload), "")
        return CommandResult(1, "", f"unexpected {' '.join(argv)}")

    return runner


def test_probe_unavailable_when_binary_absent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(code_intelligence.shutil, "which", lambda _name: None)
    health = probe_code_intelligence(tmp_path)
    assert health.codebase_memory.status == "unavailable"
    assert "codebase-memory-mcp" in health.codebase_memory.detail
    assert health.serena.status == "unavailable"
    assert "serena" in health.serena.detail
    assert health.freshness == "unavailable"
    assert health.indexed_commit == "unknown"


def test_probe_never_raises_on_runner_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(code_intelligence.shutil, "which", _which_present)

    def runner(argv: list[str], **_kwargs: object) -> CommandResult:
        if argv and argv[0] == "git":
            return CommandResult(1, "", "not a git repo")
        raise OSError("binary exploded")

    monkeypatch.setattr(code_intelligence, "run_command", runner)
    health = probe_code_intelligence(tmp_path)
    assert health.codebase_memory.status == "unavailable"
    assert health.serena.status == "unavailable"
    assert health.freshness == "unavailable"


def test_probe_does_not_start_serena_language_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[list[str]] = []
    real = _probe_runner(tmp_path)

    def runner(argv: list[str], **kwargs: object) -> CommandResult:
        seen.append(list(argv))
        return real(argv, **kwargs)

    monkeypatch.setattr(code_intelligence.shutil, "which", _which_present)
    monkeypatch.setattr(code_intelligence, "run_command", runner)
    _git_repo(tmp_path)
    probe_code_intelligence(tmp_path)
    assert any(argv[:2] == ["serena", "--version"] for argv in seen)
    assert all("health-check" not in argv for argv in seen)


@pytest.mark.parametrize(
    ("artifact_commit", "expected"),
    [
        ("HEAD", "fresh"),
        ("other", "stale"),
        (None, "unverified"),
    ],
)
def test_index_freshness_from_artifact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    artifact_commit: str | None,
    expected: str,
) -> None:
    head = _git_repo(tmp_path)
    other = _git_repo(tmp_path / "other")
    monkeypatch.setattr(code_intelligence.shutil, "which", _which_present)
    monkeypatch.setattr(code_intelligence, "run_command", _probe_runner(tmp_path))
    if artifact_commit == "HEAD":
        recorded = head
    elif artifact_commit == "other":
        recorded = other
    else:
        recorded = None
    if recorded is not None:
        dest = tmp_path / ".codebase-memory"
        dest.mkdir()
        (dest / "artifact.json").write_text(
            json.dumps({"commit": recorded, "schema_version": 2}),
            encoding="utf-8",
        )
    health = probe_code_intelligence(tmp_path)
    assert health.freshness == expected
    if expected == "fresh":
        assert health.indexed_commit == head
        assert health.codebase_memory.status == "healthy"
    elif expected == "stale":
        assert health.indexed_commit == other
        assert health.codebase_memory.status == "degraded"
    else:
        assert health.indexed_commit == "unknown"
        assert health.codebase_memory.status == "healthy"
    assert health.project == "proj"
    assert health.nodes == 3
    assert health.edges == 4
    assert health.indexed_at == "2026-11-12T00:00:00Z"


def _healthy(freshness: str = "fresh"):
    from verdict.code_intelligence import CodeIntelligenceHealth, ToolProbe

    if freshness == "unavailable":
        cm_status = "unavailable"
    elif freshness == "stale":
        cm_status = "degraded"
    else:
        cm_status = "healthy"
    return CodeIntelligenceHealth(
        codebase_memory=ToolProbe(cm_status, "scripted"),
        serena=ToolProbe(
            "unavailable" if freshness == "unavailable" else "healthy",
            "missing" if freshness == "unavailable" else "Serena 2.1.0",
        ),
        project=None if freshness == "unavailable" else "proj",
        indexed_at="2026-11-12T00:00:00Z",
        nodes=3,
        edges=4,
        index_status="ready",
        indexed_commit="unknown" if freshness != "fresh" else "abc",
        freshness=freshness,  # type: ignore[arg-type]
    )


def _retrieval_runner(mode: str):
    def runner(argv: list[str], **_kwargs: object) -> CommandResult:
        if argv and argv[0] == "git":
            return CommandResult(0, "abc123\n", "")
        joined = " ".join(argv)
        if mode == "empty":
            return CommandResult(0, json.dumps({"groups": [], "total": 0, "count": 0}), "")
        if mode == "down":
            return CommandResult(1, "", "connection refused")
        if mode == "facts":
            if "trace_path" in joined:
                payload = {
                    "callers": {
                        "groups": [
                            {
                                "file": "verdict/orchestration/worker_context.py",
                                "rows": [["build_worker_context", 1, "617-640"]],
                            }
                        ]
                    }
                }
                return CommandResult(0, json.dumps(payload), "")
            if "docs/adr" in joined:
                return CommandResult(0, json.dumps({"groups": [], "total": 0, "count": 0}), "")
            payload = {
                "groups": [
                    {
                        "file": "verdict/context_pack.py",
                        "rows": [["ContextPackCompiler", "Class", "843-1125", 22, 0]],
                    }
                ],
                "total": 1,
                "count": 1,
            }
            return CommandResult(0, json.dumps(payload), "")
        return CommandResult(1, "", "unexpected")

    return runner


def test_retrieval_records_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(code_intelligence, "run_command", _retrieval_runner("empty"))
    facts = retrieve_code_facts(
        tmp_path, symbols=["Missing"], token_budget=2000, health=_healthy()
    )
    assert facts.facts == ()
    assert facts.query_log
    assert {entry.outcome for entry in facts.query_log} == {"empty"}
    assert facts.source_revision == "abc123"


def test_retrieval_records_unavailable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str], **kwargs: object) -> CommandResult:
        calls.append(list(argv))
        return _retrieval_runner("down")(argv, **kwargs)

    monkeypatch.setattr(code_intelligence, "run_command", runner)
    facts = retrieve_code_facts(
        tmp_path,
        symbols=["ContextPackCompiler"],
        token_budget=2000,
        health=_healthy("unavailable"),
    )
    assert facts.facts == ()
    assert {entry.outcome for entry in facts.query_log} == {"unavailable"}
    assert all(argv[0] == "git" for argv in calls)


def test_retrieval_records_stale(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str], **kwargs: object) -> CommandResult:
        calls.append(list(argv))
        return _retrieval_runner("facts")(argv, **kwargs)

    monkeypatch.setattr(code_intelligence, "run_command", runner)
    facts = retrieve_code_facts(
        tmp_path, symbols=["ContextPackCompiler"], token_budget=2000, health=_healthy("stale")
    )
    assert facts.facts == ()
    assert {entry.outcome for entry in facts.query_log} == {"stale"}
    assert all(argv[0] == "git" for argv in calls)


def test_retrieval_records_omitted_over_symbol_cap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(code_intelligence, "run_command", _retrieval_runner("empty"))
    symbols = ["s0", "s1", "s2", "s3", "s4"]
    facts = retrieve_code_facts(tmp_path, symbols=symbols, token_budget=8000, health=_healthy())
    omitted = [entry for entry in facts.query_log if entry.symbol == "s4"]
    assert omitted
    assert all(entry.outcome == "omitted" for entry in omitted)
    assert all(entry.outcome != "omitted" for entry in facts.query_log if entry.symbol == "s0")


def test_retrieval_records_omitted_over_token_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(code_intelligence, "run_command", _retrieval_runner("facts"))
    facts = retrieve_code_facts(
        tmp_path, symbols=["ContextPackCompiler"], token_budget=1, health=_healthy()
    )
    assert facts.facts == ()
    assert any(entry.outcome == "omitted" for entry in facts.query_log)


def test_retrieval_cites_definition_and_caller(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(code_intelligence, "run_command", _retrieval_runner("facts"))
    facts = retrieve_code_facts(
        tmp_path, symbols=["ContextPackCompiler"], token_budget=4000, health=_healthy()
    )
    files = {fact.file for fact in facts.facts}
    assert "verdict/context_pack.py" in files
    assert any(fact.kind == "caller" and fact.file.endswith("worker_context.py") for fact in facts.facts)
    kinds = {entry.query: entry.outcome for entry in facts.query_log}
    assert kinds["definition"] == "facts"
    assert kinds["callers"] == "facts"
    assert kinds["adr"] == "empty"


def test_facts_to_context_units_cite_file_line_and_mark_stale() -> None:
    from verdict.code_intelligence import CodeFact, CodeFactSet

    fact = CodeFact(
        symbol="ContextPackCompiler",
        kind="definition",
        text="ContextPackCompiler class",
        file="verdict/context_pack.py",
        line=843,
        end_line=1125,
    )
    stale = CodeFactSet(
        facts=(fact,),
        query_log=(),
        source_revision="abc123",
        freshness="stale",
        project="proj",
    )
    units = facts_to_context_units(stale)
    assert len(units) == 1
    unit = units[0]
    assert unit.slot_type == "evidence"
    assert unit.authority == "code-intelligence"
    assert unit.trust == "local-observation"
    assert unit.revision == "abc123"
    assert unit.source_uri == "verdict/context_pack.py:843"
    assert unit.status == "stale"
    assert unit.source_digest == "sha256:" + __import__("hashlib").sha256(
        unit.content.encode()
    ).hexdigest()
    assert "verdict/context_pack.py:843" in unit.content
    assert len(unit.content) <= 1500

    active = facts_to_context_units(
        CodeFactSet(
            facts=(fact,),
            query_log=(),
            source_revision="abc123",
            freshness="fresh",
            project="proj",
        )
    )
    assert active[0].status == "active"


def test_compile_code_intelligence_pack_contains_cited_fact() -> None:
    from verdict.code_intelligence import CodeFact, CodeFactSet

    fact = CodeFact(
        symbol="ContextPackCompiler",
        kind="definition",
        text="ContextPackCompiler lives in the compiler",
        file="verdict/context_pack.py",
        line=843,
    )
    units = facts_to_context_units(
        CodeFactSet(
            facts=(fact,),
            query_log=(),
            source_revision="abc123",
            freshness="fresh",
            project="proj",
        )
    )
    pack = compile_code_intelligence_pack(units, token_budget=2000, plan_id="plan-ci")
    assert pack.candidate_id == "code-intelligence"
    assert pack.plan_id == "plan-ci"
    assert "ContextPackCompiler lives in the compiler" in pack.compiled_prompt
    assert "verdict/context_pack.py:843" in pack.compiled_prompt


def test_doctor_section_states(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cases = {
        "unavailable": "failed",
        "stale": "warn",
        "fresh": "ok",
    }
    for freshness, state in cases.items():
        monkeypatch.setattr(
            code_intelligence,
            "probe_code_intelligence",
            lambda _root, freshness=freshness: _healthy(freshness),
        )
        label, got, detail = code_intelligence_doctor_section(tmp_path)
        assert label == "Code intelligence"
        assert got == state
        assert "codebase-memory" in detail
        assert "serena" in detail
        assert freshness in detail


def test_probe_and_register_overwrites_stub_health(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(code_intelligence, "probe_code_intelligence", lambda _root: _healthy("fresh"))
    registry = build_default_registry()
    before = {
        item.provider_id: item.health
        for item in registry.providers()
        if item.provider_id in {"adapter.serena_lsp", "adapter.codebase_memory"}
    }
    assert before == {
        "adapter.serena_lsp": "unavailable",
        "adapter.codebase_memory": "unavailable",
    }
    probe_and_register(registry, tmp_path)
    after = {
        item.provider_id: item.health
        for item in registry.providers()
        if item.provider_id in {"adapter.serena_lsp", "adapter.codebase_memory"}
    }
    assert after == {
        "adapter.serena_lsp": "healthy",
        "adapter.codebase_memory": "healthy",
    }


def test_doctor_collector_appends_section_after_capability_coverage() -> None:
    source = Path("verdict/doctor_diagnostics.py").read_text(encoding="utf-8")
    coverage = source.find("Capability coverage")
    call = source.find("code_intelligence_doctor_section")
    assert coverage != -1
    assert call != -1
    assert coverage < call


@pytest.mark.skipif(shutil.which("codebase-memory-mcp") is None, reason="codebase-memory-mcp absent")
def test_live_retrieve_cites_compiler_and_a_caller() -> None:
    repo = Path(__file__).resolve().parents[1]
    facts = retrieve_code_facts(
        repo, symbols=["ContextPackCompiler"], token_budget=4000
    )
    cited = " ".join(f"{fact.file}:{fact.line} {fact.text}" for fact in facts.facts)
    assert "verdict/context_pack.py" in cited
    assert any(fact.kind == "caller" for fact in facts.facts)
