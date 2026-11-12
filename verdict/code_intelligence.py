"""Real code-intelligence probes and cited facts for context packs.

Serena is probed for health only. Its symbol tools need a running language
server, which the operator forbids on the per-pack path (a workspace check
starts pyright and has taken down the gateway). Serena is never asked to
resolve a symbol here. Symbol facts come from ``codebase-memory-mcp`` only.

Neither tool is imported. Both are optional subprocesses with a 30 second
timeout. A missing binary, a non-zero exit, a timeout, or unparseable output
is ``unavailable``. A present index whose recorded commit differs from HEAD is
``degraded`` and ``stale``. An index with no recorded commit is ``unverified``,
never ``fresh``.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from verdict.context_pack import (
    ContextPack,
    ContextPackCompiler,
    ContextPlan,
    ContextUnit,
    estimate_tokens,
)
from verdict.context_sources import make_unit

ToolStatus = Literal["healthy", "degraded", "unavailable"]
Freshness = Literal["fresh", "stale", "unverified", "unavailable"]
QueryOutcome = Literal["facts", "empty", "unavailable", "stale", "omitted"]

_TIMEOUT_SECONDS = 30.0
_SYMBOL_CAP = 4
_RESULT_LIMIT = 10
_TRACE_DEPTH = 1
_CONTENT_LIMIT = 1500
_STDERR_TAIL = 400
_PAGE_LIMIT = 200
_MAX_PAGES = 5
_CODEBASE_BIN = "codebase-memory-mcp"
_SERENA_BIN = "serena"
_QUERY_KINDS = ("definition", "callers", "adr")


@dataclass(frozen=True)
class CommandResult:
    """One bounded subprocess result. Failures are values, not exceptions."""

    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class ToolProbe:
    """One tool's probed status. ``detail`` names the binary or the stderr tail."""

    status: ToolStatus
    detail: str


@dataclass(frozen=True)
class CodeIntelligenceHealth:
    """Probe result for both tools plus the codebase-memory index."""

    codebase_memory: ToolProbe
    serena: ToolProbe
    project: str | None
    indexed_at: str | None
    nodes: int | None
    edges: int | None
    index_status: str | None
    indexed_commit: str
    freshness: Freshness


@dataclass(frozen=True)
class CodeFact:
    """One cited definition, caller, or ADR hit."""

    symbol: str
    kind: str
    text: str
    file: str
    line: int
    end_line: int | None = None


@dataclass(frozen=True)
class QueryLogEntry:
    """One query's outcome. A stub is never recorded as facts."""

    symbol: str
    query: str
    outcome: QueryOutcome
    detail: str = ""


@dataclass(frozen=True)
class CodeFactSet:
    """Facts plus the per-query log and the git revision they were read against."""

    facts: tuple[CodeFact, ...]
    query_log: tuple[QueryLogEntry, ...]
    source_revision: str
    freshness: Freshness
    project: str | None


_LOCK_RETRY_SECONDS = 2.0


def run_command(
    argv: Sequence[str], *, cwd: Path | None = None, timeout: float = _TIMEOUT_SECONDS
) -> CommandResult:
    """Run ``argv`` without a shell. Timeout and OS errors become a non-zero result."""

    try:
        completed = subprocess.run(
            list(argv),
            cwd=str(cwd) if cwd is not None else None,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        err = exc.stderr if isinstance(exc.stderr, str) else ""
        return CommandResult(124, "", err or "timeout")
    except OSError as exc:
        return CommandResult(127, "", str(exc))
    return CommandResult(completed.returncode, completed.stdout or "", completed.stderr or "")


def _run(argv: Sequence[str], *, cwd: Path | None, timeout: float) -> CommandResult:
    result = run_command(argv, cwd=cwd, timeout=timeout)
    if result.returncode == 0 or "another editor or agent session" not in result.stderr:
        return result
    time.sleep(_LOCK_RETRY_SECONDS)
    return run_command(argv, cwd=cwd, timeout=timeout)


def probe_code_intelligence(repo_root: Path) -> CodeIntelligenceHealth:
    """Probe both binaries. Never raises. Never starts a language server."""

    root = Path(repo_root)
    try:
        serena = _probe_serena()
        codebase, project, indexed_at, nodes, edges, index_status, indexed_commit, freshness = (
            _probe_codebase(root)
        )
    except Exception as exc:
        detail = f"probe failed: {type(exc).__name__}"
        missing = ToolProbe("unavailable", detail)
        return CodeIntelligenceHealth(
            missing, missing, None, None, None, None, None, "unknown", "unavailable"
        )
    return CodeIntelligenceHealth(
        codebase_memory=codebase,
        serena=serena,
        project=project,
        indexed_at=indexed_at,
        nodes=nodes,
        edges=edges,
        index_status=index_status,
        indexed_commit=indexed_commit,
        freshness=freshness,
    )


def retrieve_code_facts(
    repo_root: Path,
    *,
    symbols: Sequence[str],
    token_budget: int,
    health: CodeIntelligenceHealth | None = None,
) -> CodeFactSet:
    """Bounded definition, caller, and ADR queries. Serena is not queried."""

    root = Path(repo_root)
    revision = _git_head(root)
    active = health if health is not None else probe_code_intelligence(root)
    names = _bounded_symbols(symbols)
    log: list[QueryLogEntry] = []
    facts: list[CodeFact] = []
    if (
        active.codebase_memory.status == "unavailable"
        or active.freshness == "unavailable"
        or not active.project
    ):
        detail = active.codebase_memory.detail or "codebase-memory unavailable"
        for symbol in names:
            _log_all(log, symbol, "unavailable", detail)
        return CodeFactSet((), tuple(log), revision, active.freshness, active.project)
    if active.freshness == "stale":
        for symbol in names:
            _log_all(log, symbol, "stale", "index commit differs from HEAD")
        return CodeFactSet((), tuple(log), revision, active.freshness, active.project)

    kept = names[:_SYMBOL_CAP]
    for symbol in names[_SYMBOL_CAP:]:
        _log_all(log, symbol, "omitted", "symbol cap")
    remaining = token_budget if isinstance(token_budget, int) and token_budget > 0 else 0
    project = active.project
    for symbol in kept:
        if remaining < 1:
            _log_all(log, symbol, "omitted", "token budget")
            continue
        for kind in _QUERY_KINDS:
            outcome, detail, found = _query(project, symbol, kind)
            if outcome != "facts":
                log.append(QueryLogEntry(symbol, kind, outcome, detail))
                continue
            included: list[CodeFact] = []
            for fact in found:
                cost = estimate_tokens(fact.text)
                if cost > remaining:
                    continue
                remaining -= cost
                included.append(fact)
            if included:
                facts.extend(included)
                log.append(QueryLogEntry(symbol, kind, "facts", detail))
            else:
                log.append(QueryLogEntry(symbol, kind, "omitted", "token budget"))
    return CodeFactSet(tuple(facts), tuple(log), revision, active.freshness, project)


def facts_to_context_units(facts: CodeFactSet) -> tuple[ContextUnit, ...]:
    """One evidence unit per fact, citing ``file:line`` and digesting its content."""

    status: Literal["stale", "active"] = "stale" if facts.freshness == "stale" else "active"
    units: list[ContextUnit] = []
    for fact in facts.facts:
        citation = f"{fact.file}:{fact.line}"
        content = _bounded_content(fact.text, citation)
        unit = make_unit(
            slot_type="evidence",
            key=f"code-intelligence:{fact.kind}:{fact.symbol}:{citation}",
            content=content,
            source_uri=citation,
            authority="code-intelligence",
            trust="local-observation",
            revision=facts.source_revision or "unknown",
        )
        if unit is None:
            continue
        if status != "active":
            unit = replace(unit, status=status)
        units.append(unit)
    return tuple(units)


def compile_code_intelligence_pack(
    units: Sequence[ContextUnit], *, token_budget: int, plan_id: str
) -> ContextPack:
    """Compile cited units through the existing compiler. This is the proof seam."""

    plan = ContextPlan(plan_id=plan_id, candidate_id="code-intelligence", token_budget=token_budget)
    return ContextPackCompiler().compile_units(tuple(units), plan)


def code_intelligence_doctor_section(repo_root: Path) -> tuple[str, str, str]:
    """``(label, state, detail)`` for ``verdict doctor``. Never silent."""

    health = probe_code_intelligence(Path(repo_root))
    codebase = health.codebase_memory.status
    serena = health.serena.status
    if codebase == "unavailable" and serena == "unavailable":
        state = "failed"
    elif codebase == "healthy" and serena == "healthy" and health.freshness != "stale":
        state = "ok"
    else:
        state = "warn"
    detail = f"codebase-memory {codebase}; serena {serena}; index {health.freshness}"
    return ("Code intelligence", state, detail)


def _probe_serena() -> ToolProbe:
    if shutil.which(_SERENA_BIN) is None:
        return ToolProbe("unavailable", f"{_SERENA_BIN}: binary not found")
    try:
        result = _run([_SERENA_BIN, "--version"], cwd=None, timeout=_TIMEOUT_SECONDS)
    except Exception as exc:
        return ToolProbe("unavailable", f"{_SERENA_BIN}: {type(exc).__name__}")
    if result.returncode != 0 or not result.stdout.strip():
        tail = _tail(result.stderr) or "no version output"
        return ToolProbe("unavailable", f"{_SERENA_BIN}: {tail}")
    return ToolProbe("healthy", result.stdout.strip())


def _probe_codebase(
    repo_root: Path,
) -> tuple[ToolProbe, str | None, str | None, int | None, int | None, str | None, str, Freshness]:
    empty: tuple[str | None, str | None, int | None, int | None, str | None, str, Freshness] = (
        None,
        None,
        None,
        None,
        None,
        "unknown",
        "unavailable",
    )
    if shutil.which(_CODEBASE_BIN) is None:
        return (ToolProbe("unavailable", f"{_CODEBASE_BIN}: binary not found"), *empty)
    try:
        projects, error = _list_projects()
    except Exception as exc:
        return (ToolProbe("unavailable", f"{_CODEBASE_BIN}: {type(exc).__name__}"), *empty)
    if error is not None:
        return (ToolProbe("unavailable", f"{_CODEBASE_BIN}: {error}"), *empty)
    match = _match_project(projects, repo_root)
    if match is None:
        return (
            ToolProbe("unavailable", f"{_CODEBASE_BIN}: no indexed project for {repo_root}"),
            *empty,
        )
    name, _root = match
    try:
        status_payload, error = _index_status(name)
    except Exception as exc:
        return (ToolProbe("unavailable", f"{_CODEBASE_BIN}: {type(exc).__name__}"), *empty)
    if error is not None or status_payload is None:
        return (
            ToolProbe("unavailable", f"{_CODEBASE_BIN}: {error or 'unparseable index status'}"),
            *empty,
        )
    indexed_at = _text(status_payload.get("indexed_at"))
    nodes = _int(status_payload.get("nodes"))
    edges = _int(status_payload.get("edges"))
    index_status = _text(status_payload.get("status"))
    project = _text(status_payload.get("project")) or name
    recorded = _artifact_commit(repo_root)
    head = _git_head(repo_root)
    if recorded is None or head == "unknown":
        freshness: Freshness = "unverified"
        indexed_commit = recorded or "unknown"
    elif recorded == head:
        freshness = "fresh"
        indexed_commit = recorded
    else:
        freshness = "stale"
        indexed_commit = recorded
    if freshness == "stale" or (index_status is not None and index_status != "ready"):
        tool = ToolProbe("degraded", f"{_CODEBASE_BIN}: index {freshness} status={index_status}")
    else:
        tool = ToolProbe("healthy", f"{_CODEBASE_BIN}: {project} {index_status or 'ready'}")
    return tool, project, indexed_at, nodes, edges, index_status, indexed_commit, freshness


def _list_projects() -> tuple[list[tuple[str, str]], str | None]:
    found: list[tuple[str, str]] = []
    offset = 0
    for _page in range(_MAX_PAGES):
        result = _run(
            [
                _CODEBASE_BIN,
                "cli",
                "list_projects",
                "--format",
                "json",
                "--limit",
                str(_PAGE_LIMIT),
                "--offset",
                str(offset),
            ],
            cwd=None,
            timeout=_TIMEOUT_SECONDS,
        )
        payload = _payload(result)
        if payload is None:
            return [], _tail(result.stderr) or "unparseable list_projects"
        projects = payload.get("projects")
        if not isinstance(projects, list):
            return [], "list_projects returned no projects list"
        for item in projects:
            if not isinstance(item, Mapping):
                continue
            name = _text(item.get("name"))
            root = _text(item.get("root_path"))
            if name and root:
                found.append((name, root))
        if not payload.get("has_more"):
            break
        next_offset = _int(payload.get("next_offset"))
        offset = next_offset if next_offset is not None else offset + _PAGE_LIMIT
    return found, None


def _index_status(project: str) -> tuple[Mapping[str, Any] | None, str | None]:
    result = _run(
        [_CODEBASE_BIN, "cli", "index_status", "--project", project, "--format", "json"],
        cwd=None,
        timeout=_TIMEOUT_SECONDS,
    )
    payload = _payload(result)
    if payload is None:
        return None, _tail(result.stderr) or "unparseable index_status"
    return payload, None


def _match_project(projects: Sequence[tuple[str, str]], repo_root: Path) -> tuple[str, str] | None:
    target = repo_root.resolve()
    matches: list[tuple[int, str, str]] = []
    for name, root in projects:
        try:
            candidate = Path(root).resolve()
            target.relative_to(candidate)
        except (OSError, ValueError):
            continue
        matches.append((len(str(candidate)), name, root))
    if not matches:
        return None
    _length, name, root = max(matches)
    return name, root


def _artifact_commit(repo_root: Path) -> str | None:
    path = repo_root / ".codebase-memory" / "artifact.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    payload = _load_object(raw)
    if payload is None:
        return None
    commit = payload.get("commit")
    if isinstance(commit, str) and commit.strip():
        return commit.strip()
    return None


def _git_head(repo_root: Path) -> str:
    try:
        result = run_command(["git", "-C", str(repo_root), "rev-parse", "HEAD"])
    except Exception:
        return "unknown"
    if result.returncode != 0:
        return "unknown"
    head = result.stdout.strip().splitlines()
    return head[0] if head and head[0] else "unknown"


def _bounded_symbols(symbols: Sequence[str]) -> tuple[str, ...]:
    if isinstance(symbols, str):
        symbols = (symbols,)
    names: list[str] = []
    for symbol in symbols:
        if isinstance(symbol, str) and symbol.strip():
            names.append(symbol.strip())
    return tuple(names)


def _log_all(log: list[QueryLogEntry], symbol: str, outcome: QueryOutcome, detail: str) -> None:
    for kind in _QUERY_KINDS:
        log.append(QueryLogEntry(symbol, kind, outcome, detail))


def _query(project: str, symbol: str, kind: str) -> tuple[QueryOutcome, str, tuple[CodeFact, ...]]:
    try:
        if kind == "definition":
            argv = [
                _CODEBASE_BIN,
                "cli",
                "search_graph",
                "--project",
                project,
                "--name-pattern",
                symbol,
                "--limit",
                str(_RESULT_LIMIT),
                "--format",
                "json",
            ]
        elif kind == "callers":
            argv = [
                _CODEBASE_BIN,
                "cli",
                "trace_path",
                "--project",
                project,
                "--function-name",
                symbol,
                "--direction",
                "inbound",
                "--depth",
                str(_TRACE_DEPTH),
                "--limit",
                str(_RESULT_LIMIT),
                "--format",
                "json",
            ]
        else:
            argv = [
                _CODEBASE_BIN,
                "cli",
                "search_graph",
                "--project",
                project,
                "--file-pattern",
                "docs/adr/*",
                "--name-pattern",
                symbol,
                "--limit",
                str(_RESULT_LIMIT),
                "--format",
                "json",
            ]
        result = _run(argv, cwd=None, timeout=_TIMEOUT_SECONDS)
    except Exception as exc:
        return "unavailable", f"{_CODEBASE_BIN}: {type(exc).__name__}", ()
    payload = _payload(result)
    if payload is None:
        return "unavailable", f"{_CODEBASE_BIN}: {_tail(result.stderr) or 'unparseable output'}", ()
    if kind == "callers":
        facts = _caller_facts(project, symbol, payload)
    else:
        fact_kind = "adr" if kind == "adr" else "definition"
        facts = _graph_facts(symbol, fact_kind, payload)
    if not facts:
        return "empty", "", ()
    return "facts", "", facts


def _graph_facts(symbol: str, kind: str, payload: Mapping[str, Any]) -> tuple[CodeFact, ...]:
    groups = payload.get("groups")
    if not isinstance(groups, list):
        return ()
    columns = _columns(payload.get("cols"))
    facts: list[CodeFact] = []
    for group in groups:
        if not isinstance(group, Mapping):
            continue
        file_name = _text(group.get("file"))
        rows = group.get("rows")
        if not file_name or not isinstance(rows, list):
            continue
        for row in rows:
            fields = _row_fields(row, columns)
            if fields is None:
                continue
            name = _text(fields.get("name")) or symbol
            line, end_line = _span(fields.get("lines"))
            if line is None:
                line = _int(fields.get("start_line")) or 1
                end_line = _int(fields.get("end_line"))
            facts.append(_fact(symbol, kind, name, file_name, line, end_line))
            if len(facts) >= _RESULT_LIMIT:
                return tuple(facts)
    return tuple(facts)


def _caller_facts(project: str, symbol: str, payload: Mapping[str, Any]) -> tuple[CodeFact, ...]:
    callers = payload.get("callers")
    groups: object = None
    columns: tuple[str, ...] = ()
    if isinstance(callers, Mapping):
        groups = callers.get("groups")
        columns = _columns(callers.get("cols"))
    if not isinstance(groups, list):
        return ()
    facts: list[CodeFact] = []
    for group in groups:
        if not isinstance(group, Mapping):
            continue
        rows = group.get("rows")
        if not isinstance(rows, list):
            continue
        file_name = _text(group.get("file"))
        for row in rows:
            fields = _row_fields(row, columns)
            if fields is None:
                continue
            name = _text(fields.get("name"))
            if not name:
                continue
            line, end_line = _span(fields.get("lines"))
            if line is None:
                line = _int(fields.get("start_line"))
                end_line = _int(fields.get("end_line"))
            if file_name is None or line is None:
                located = _lookup_symbol(project, name)
                if located is not None:
                    file_name, line, end_line = located
            if file_name is None:
                file_name = _file_from_prefix(_text(group.get("qn_prefix")) or "", project)
            if file_name is None:
                continue
            if line is None:
                line = 1
            facts.append(_fact(symbol, "caller", name, file_name, line, end_line))
            if len(facts) >= _RESULT_LIMIT:
                return tuple(facts)
    return tuple(facts)


def _columns(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _row_fields(row: object, columns: Sequence[str]) -> Mapping[str, object] | None:
    if isinstance(row, Mapping):
        return row
    if not isinstance(row, list):
        return None
    if columns:
        return {name: row[index] for index, name in enumerate(columns) if index < len(row)}
    names = ("name", "label", "lines")
    return {name: row[index] for index, name in enumerate(names) if index < len(row)}


def _span(value: object) -> tuple[int | None, int | None]:
    if isinstance(value, int) and not isinstance(value, bool):
        return value, None
    if not isinstance(value, str):
        return None, None
    text = value.strip()
    if not text or not text[0].isdigit():
        return None, None
    start_text, sep, end_text = text.partition("-")
    if not start_text.isdigit() or (sep and not end_text.isdigit()):
        return None, None
    return int(start_text), int(end_text) if sep else None


def _lookup_symbol(project: str, name: str) -> tuple[str, int, int | None] | None:
    try:
        result = _run(
            [
                _CODEBASE_BIN,
                "cli",
                "search_graph",
                "--project",
                project,
                "--name-pattern",
                name,
                "--limit",
                "1",
                "--format",
                "json",
            ],
            cwd=None,
            timeout=_TIMEOUT_SECONDS,
        )
    except Exception:
        return None
    payload = _payload(result)
    if payload is None:
        return None
    facts = _graph_facts(name, "definition", payload)
    if not facts:
        return None
    fact = facts[0]
    return fact.file, fact.line, fact.end_line


def _file_from_prefix(prefix: str, project: str) -> str | None:
    marker = project + "."
    if not prefix.startswith(marker):
        return None
    rest = prefix[len(marker) :]
    if not rest or "/" in rest:
        return None
    return rest.replace(".", "/") + ".py"


def _fact(
    symbol: str, kind: str, name: str, file_name: str, line: int, end_line: int | None
) -> CodeFact:
    span = f"{line}-{end_line}" if end_line is not None else str(line)
    text = f"{name} {kind} {file_name}:{span}"
    return CodeFact(symbol, kind, text, file_name, line, end_line)


def _bounded_content(text: str, citation: str) -> str:
    body = text.strip() or citation
    suffix = f"\n({citation})"
    if citation not in body:
        body = f"{body}{suffix}"
    if len(body) > _CONTENT_LIMIT:
        keep = _CONTENT_LIMIT - len(suffix)
        body = (body[:keep] if keep > 0 else "") + suffix
        body = body[:_CONTENT_LIMIT]
    digest = hashlib.sha256(body.encode()).hexdigest()
    if not digest:
        return citation[:_CONTENT_LIMIT]
    return body


def _payload(result: CommandResult) -> Mapping[str, Any] | None:
    if result.returncode != 0:
        return None
    return _load_object(result.stdout)


def _load_object(raw: str) -> dict[str, Any] | None:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    return value


def _text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _tail(text: str) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= _STDERR_TAIL:
        return cleaned
    return cleaned[-_STDERR_TAIL:]


__all__ = [
    "CodeFact",
    "CodeFactSet",
    "CodeIntelligenceHealth",
    "CommandResult",
    "Freshness",
    "QueryLogEntry",
    "QueryOutcome",
    "ToolProbe",
    "ToolStatus",
    "code_intelligence_doctor_section",
    "compile_code_intelligence_pack",
    "facts_to_context_units",
    "probe_code_intelligence",
    "retrieve_code_facts",
    "run_command",
]
