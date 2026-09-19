"""Native Context Intelligence source providers (BOD-123 Wave-1).

External MCP/Serena adapters plug into the same CapabilityProvider protocol
later. This module is the always-available Verdict native baseline.
"""

from __future__ import annotations

import ast
import hashlib
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from verdict.context_pack import ContextUnit
from verdict.memory_plane import MemoryPlane

PROVIDER_ID = "native.verdict"
DEFAULT_MAX_FILE_BYTES = 8_192
DEFAULT_MAX_UNITS = 8
_SKIP_DIR_PARTS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    ".tox",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
}
_DOC_NAMES = frozenset({"readme.md", "readme", "agents.md", "contributing.md"})
_SECRET = re.compile(
    r"(?i)(?:api[_-]?key\s*[:=]|password\s*[:=]|bearer\s+[a-z0-9._~+/=-]{16,}|sk-[a-z0-9]{20,})"
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _digest_text(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode()).hexdigest()}"


def _skip_path(path: Path) -> bool:
    return any(part in _SKIP_DIR_PARTS for part in path.parts)


def _looks_secret(text: str) -> bool:
    return _SECRET.search(text) is not None


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


@dataclass(frozen=True)
class ProviderResult:
    """Units from one capability invoke, or an explicit gap."""

    capability_id: str
    provider_id: str
    units: tuple[ContextUnit, ...] = ()
    gap_reason: str | None = None

    @property
    def gap(self) -> str | None:
        return self.gap_reason


@dataclass(frozen=True)
class CapabilityProvider:
    """Descriptor + callable for a semantic context capability."""

    provider_id: str
    capabilities: frozenset[str]
    authority: str = "native-baseline"
    health: str = "healthy"

    def provide(
        self,
        *,
        capability_id: str,
        query: str,
        repo_root: Path,
        max_units: int = DEFAULT_MAX_UNITS,
        hints: Mapping[str, Any] | None = None,
        plane: MemoryPlane | None = None,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    ) -> ProviderResult:
        raise NotImplementedError


class CapabilityProviderProtocol(Protocol):
    provider_id: str
    capabilities: frozenset[str]

    def provide(
        self,
        *,
        capability_id: str,
        query: str,
        repo_root: Path,
        max_units: int = DEFAULT_MAX_UNITS,
        hints: Mapping[str, Any] | None = None,
        plane: MemoryPlane | None = None,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    ) -> ProviderResult: ...


def make_unit(
    *,
    slot_type: str,
    key: str,
    content: str,
    source_uri: str,
    authority: str,
    trust: str = "local-observation",
    confidence: float = 1.0,
    span: Mapping[str, int] | None = None,
    revision: str = "unknown",
    transform_lineage: tuple[str, ...] = (),
) -> ContextUnit | None:
    if _looks_secret(content):
        return None
    return ContextUnit(
        unit_id=_digest_text(source_uri + key)[:24],
        slot_type=slot_type,  # type: ignore[arg-type]
        key=key,
        content=content,
        source_uri=source_uri,
        source_digest=_digest_text(content),
        observed_at=_now_iso(),
        trust=trust,
        authority=authority,
        confidence=confidence,
        span=dict(span) if span is not None else None,
        revision=revision,
        transform_lineage=transform_lineage,
    )


def _symbols_from_hints(hints: Mapping[str, Any] | None, query: str) -> tuple[str, ...]:
    symbols: list[str] = []
    if hints:
        raw = hints.get("symbols") or hints.get("target_symbols") or ()
        if isinstance(raw, str):
            symbols.append(raw)
        elif isinstance(raw, Sequence):
            symbols.extend(str(item) for item in raw if str(item).strip())
    if not symbols:
        found = re.findall(r"\b([A-Za-z_][A-Za-z0-9_]{2,})\b", query)
        symbols.extend(found[:6])
    seen: set[str] = set()
    ordered: list[str] = []
    stop = {"def", "class", "return", "from", "import", "the", "and", "for"}
    for name in symbols:
        if name not in seen and name not in stop:
            seen.add(name)
            ordered.append(name)
    return tuple(ordered)


@dataclass
class _SymbolHit:
    name: str
    kind: str
    file_path: str
    lineno: int
    end_lineno: int
    snippet: str
    is_definition: bool


def _iter_py_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    files: list[Path] = []
    for path in root.rglob("*.py"):
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(root)
        except ValueError:
            rel = path
        if _skip_path(rel):
            continue
        files.append(path)
    return sorted(files)


def _extract_definitions(path: Path, root: Path, symbols: Sequence[str]) -> list[_SymbolHit]:
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []
    lines = source.splitlines()
    rel = _rel(path, root)
    wanted = set(symbols)
    hits: list[_SymbolHit] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if wanted and node.name not in wanted:
                continue
            end = getattr(node, "end_lineno", None) or node.lineno
            snippet = "\n".join(lines[node.lineno - 1 : min(end, node.lineno + 40)])
            kind = "class" if isinstance(node, ast.ClassDef) else "function"
            hits.append(
                _SymbolHit(
                    name=node.name,
                    kind=kind,
                    file_path=rel,
                    lineno=node.lineno,
                    end_lineno=end,
                    snippet=snippet[:DEFAULT_MAX_FILE_BYTES],
                    is_definition=True,
                )
            )
    return hits


def _extract_references(path: Path, root: Path, symbols: Sequence[str]) -> list[_SymbolHit]:
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []
    lines = source.splitlines()
    rel = _rel(path, root)
    wanted = set(symbols)
    if not wanted:
        return []
    def_header_lines: set[tuple[str, int]] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.name in wanted
        ):
            def_header_lines.add((node.name, node.lineno))

    hits: list[_SymbolHit] = []
    for node in ast.walk(tree):
        name = ""
        lineno = getattr(node, "lineno", None)
        if lineno is None:
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in wanted:
            name = node.id
        elif isinstance(node, ast.Attribute) and node.attr in wanted:
            name = node.attr
        elif isinstance(node, ast.alias):
            candidate = node.asname or node.name.split(".")[-1]
            if candidate in wanted:
                name = candidate
        if not name:
            continue
        if (name, lineno) in def_header_lines:
            continue
        start_line = max(1, lineno - 2)
        end_line = min(len(lines), lineno + 2)
        snippet = "\n".join(lines[start_line - 1 : end_line])
        hits.append(
            _SymbolHit(
                name=name,
                kind="reference",
                file_path=rel,
                lineno=lineno,
                end_lineno=end_line,
                snippet=snippet[:DEFAULT_MAX_FILE_BYTES],
                is_definition=False,
            )
        )
    return hits


class NativeCodeProvider(CapabilityProvider):
    """AST-light Python symbols / definitions / references."""

    def __init__(self) -> None:
        super().__init__(
            provider_id=PROVIDER_ID,
            capabilities=frozenset({"code.symbols", "code.definitions", "code.references"}),
            authority="native-code",
        )

    def provide(
        self,
        *,
        capability_id: str,
        query: str,
        repo_root: Path,
        max_units: int = DEFAULT_MAX_UNITS,
        hints: Mapping[str, Any] | None = None,
        plane: MemoryPlane | None = None,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    ) -> ProviderResult:
        root = repo_root.resolve()
        symbols = _symbols_from_hints(hints, query)
        if not symbols:
            return ProviderResult(capability_id, self.provider_id, gap_reason="no_symbol_seed")
        units: list[ContextUnit] = []
        files = _iter_py_files(root)
        if capability_id == "code.definitions":
            for path in files:
                for hit in _extract_definitions(path, root, symbols):
                    unit = make_unit(
                        slot_type="evidence",
                        key=f"def:{hit.file_path}:{hit.name}:{hit.lineno}",
                        content=hit.snippet,
                        source_uri=hit.file_path,
                        authority="native-code",
                        span={"start_line": hit.lineno, "end_line": hit.end_lineno},
                        transform_lineage=("raw", "ast-definition-slice"),
                    )
                    if unit is not None:
                        units.append(unit)
                    if len(units) >= max_units:
                        break
                if len(units) >= max_units:
                    break
        elif capability_id == "code.references":
            for path in files:
                for hit in _extract_references(path, root, symbols):
                    unit = make_unit(
                        slot_type="evidence",
                        key=f"ref:{hit.file_path}:{hit.name}:{hit.lineno}",
                        content=hit.snippet,
                        source_uri=hit.file_path,
                        authority="native-code",
                        span={"start_line": hit.lineno, "end_line": hit.end_lineno},
                        transform_lineage=("raw", "ast-reference-slice"),
                    )
                    if unit is not None:
                        units.append(unit)
                    if len(units) >= max_units:
                        break
                if len(units) >= max_units:
                    break
        else:
            seen: set[tuple[str, str, int]] = set()
            lines_out: list[str] = []
            for path in files:
                for hit in _extract_definitions(path, root, symbols):
                    key = (hit.file_path, hit.name, hit.lineno)
                    if key in seen:
                        continue
                    seen.add(key)
                    lines_out.append(f"{hit.kind} {hit.name} @ {hit.file_path}:{hit.lineno}")
                    if len(lines_out) >= max_units * 4:
                        break
                if len(lines_out) >= max_units * 4:
                    break
            if lines_out:
                unit = make_unit(
                    slot_type="evidence",
                    key="symbols:index",
                    content="\n".join(lines_out),
                    source_uri="code://symbols",
                    authority="native-code",
                    transform_lineage=("raw", "ast-symbol-index"),
                )
                if unit is not None:
                    units.append(unit)
        if not units:
            return ProviderResult(capability_id, self.provider_id, gap_reason="not_found")
        return ProviderResult(capability_id, self.provider_id, tuple(units[:max_units]))


class NativeDocsProvider(CapabilityProvider):
    """Project README / AGENTS / ADR / architecture docs."""

    def __init__(self) -> None:
        super().__init__(
            provider_id=PROVIDER_ID,
            capabilities=frozenset({"docs.project"}),
            authority="project-docs",
        )

    def provide(
        self,
        *,
        capability_id: str,
        query: str,
        repo_root: Path,
        max_units: int = DEFAULT_MAX_UNITS,
        hints: Mapping[str, Any] | None = None,
        plane: MemoryPlane | None = None,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    ) -> ProviderResult:
        root = repo_root.resolve()
        candidates: list[Path] = []
        for name in ("README.md", "README", "AGENTS.md", "CONTRIBUTING.md"):
            path = root / name
            if path.is_file():
                candidates.append(path)
        for sub in (root / "docs" / "adr", root / "docs" / "architecture", root / "docs"):
            if sub.is_dir():
                candidates.extend(sorted(sub.rglob("*.md")))
        terms = [t.lower() for t in re.findall(r"[A-Za-z0-9_-]{3,}", query)]
        units: list[ContextUnit] = []
        seen: set[str] = set()
        for path in candidates:
            try:
                rel_check = path.relative_to(root)
            except ValueError:
                rel_check = path
            if _skip_path(rel_check) or not path.is_file():
                continue
            rel = _rel(path, root)
            if rel in seen:
                continue
            try:
                payload = path.read_text(encoding="utf-8", errors="replace")[:max_file_bytes]
            except OSError:
                continue
            lowered = payload.lower()
            name_l = path.name.lower()
            is_project_doc = name_l in _DOC_NAMES or "docs/adr" in rel or "architecture" in rel
            term_hit = any(term in lowered or term in rel.lower() for term in terms)
            if terms and not term_hit:
                continue
            if not is_project_doc and not terms:
                continue
            seen.add(rel)
            unit = make_unit(
                slot_type="evidence",
                key=f"docs:{rel}",
                content=payload,
                source_uri=rel,
                authority="project-docs",
                transform_lineage=("raw", "docs-slice"),
            )
            if unit is not None:
                units.append(unit)
            if len(units) >= max_units:
                break
        if not units:
            return ProviderResult(capability_id, self.provider_id, gap_reason="not_found")
        return ProviderResult(capability_id, self.provider_id, tuple(units))


class NativeMemoryProvider(CapabilityProvider):
    """MemoryPlane lexical search — consume only, no plane rewrite."""

    def __init__(self) -> None:
        super().__init__(
            provider_id=PROVIDER_ID,
            capabilities=frozenset({"memory.search"}),
            authority="memory-plane",
        )

    def provide(
        self,
        *,
        capability_id: str,
        query: str,
        repo_root: Path,
        max_units: int = DEFAULT_MAX_UNITS,
        hints: Mapping[str, Any] | None = None,
        plane: MemoryPlane | None = None,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    ) -> ProviderResult:
        if plane is None:
            return ProviderResult(capability_id, self.provider_id, gap_reason="no_default_location")
        queries: list[str] = [query]
        if hints:
            symbols = hints.get("symbols") or hints.get("target_symbols") or ()
            if isinstance(symbols, str) and symbols.strip():
                queries.append(symbols.strip())
            elif isinstance(symbols, Sequence):
                for item in symbols:
                    text = str(item).strip()
                    if text:
                        queries.append(text)
        units: list[ContextUnit] = []
        seen: set[str] = set()
        for search_query in queries:
            if not search_query.strip():
                continue
            ranked = plane.search_ranked(search_query, limit=max_units)
            for result in ranked:
                if result.stale:
                    continue
                if result.record.record_id in seen:
                    continue
                seen.add(result.record.record_id)
                unit = make_unit(
                    slot_type="memory",
                    key=f"memory:{result.record.key}",
                    content=result.record.content,
                    source_uri=f"memory:{result.record.record_id}",
                    authority=result.record.authority or "memory-plane",
                    trust=result.record.trust,
                    confidence=max(0.1, min(1.0, 1.0 / (result.rank or 1))),
                    transform_lineage=("raw", "memory-search"),
                )
                if unit is not None:
                    units.append(unit)
                if len(units) >= max_units:
                    break
            if len(units) >= max_units:
                break
        if not units:
            return ProviderResult(capability_id, self.provider_id, gap_reason="not_found")
        return ProviderResult(capability_id, self.provider_id, tuple(units))


def _run_git(root: Path, *args: str) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            ["git", *args], cwd=root, check=False, capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    out = (completed.stdout or "") + (
        completed.stderr or "" if completed.returncode != 0 and not completed.stdout else ""
    )
    return completed.returncode, out


class NativeGitProvider(CapabilityProvider):
    """git.diff + repo.state from local git — degraded gap if git missing."""

    def __init__(self) -> None:
        super().__init__(
            provider_id=PROVIDER_ID,
            capabilities=frozenset({"git.diff", "repo.state"}),
            authority="native-git",
        )

    def provide(
        self,
        *,
        capability_id: str,
        query: str,
        repo_root: Path,
        max_units: int = DEFAULT_MAX_UNITS,
        hints: Mapping[str, Any] | None = None,
        plane: MemoryPlane | None = None,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    ) -> ProviderResult:
        root = repo_root.resolve()
        if not (root / ".git").exists():
            return ProviderResult(
                capability_id, self.provider_id, gap_reason="provider_unavailable"
            )
        if capability_id == "git.diff":
            code, out = _run_git(root, "diff", "--", ".")
            if code != 0:
                return ProviderResult(
                    capability_id, self.provider_id, gap_reason="provider_unavailable"
                )
            text = out.strip()
            if not text:
                code2, status = _run_git(root, "status", "--porcelain")
                if code2 != 0 or not status.strip():
                    return ProviderResult(capability_id, self.provider_id, gap_reason="not_found")
                text = status[:max_file_bytes]
            unit = make_unit(
                slot_type="evidence",
                key="git:diff",
                content=text[:max_file_bytes],
                source_uri="git://diff",
                authority="native-git",
                transform_lineage=("raw", "git-diff"),
            )
            units = (unit,) if unit is not None else ()
            if not units:
                return ProviderResult(capability_id, self.provider_id, gap_reason="not_found")
            return ProviderResult(capability_id, self.provider_id, units[:max_units])

        parts: list[str] = []
        for args, label in (
            (("rev-parse", "HEAD"), "revision"),
            (("rev-parse", "--abbrev-ref", "HEAD"), "branch"),
            (("status", "--short", "--branch"), "status"),
        ):
            code, out = _run_git(root, *args)
            if code == 0 and out.strip():
                parts.append(f"{label}: {out.strip()}")
        if not parts:
            return ProviderResult(
                capability_id, self.provider_id, gap_reason="provider_unavailable"
            )
        revision = parts[0].split(":", 1)[-1].strip() if parts else "unknown"
        unit = make_unit(
            slot_type="state",
            key="repo:state",
            content="\n".join(parts),
            source_uri="git://repo-state",
            authority="native-git",
            transform_lineage=("raw", "git-state"),
            revision=revision,
        )
        units = (unit,) if unit is not None else ()
        return ProviderResult(capability_id, self.provider_id, units)


class NativeTaskProvider(CapabilityProvider):
    """task.requirements / task.proof from plan hints — never invents."""

    def __init__(self) -> None:
        super().__init__(
            provider_id=PROVIDER_ID,
            capabilities=frozenset({"task.requirements", "task.proof"}),
            authority="task-contract",
        )

    def provide(
        self,
        *,
        capability_id: str,
        query: str,
        repo_root: Path,
        max_units: int = DEFAULT_MAX_UNITS,
        hints: Mapping[str, Any] | None = None,
        plane: MemoryPlane | None = None,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    ) -> ProviderResult:
        hints = hints or {}
        if capability_id == "task.requirements":
            task = str(hints.get("task") or query or "").strip()
            criteria = tuple(hints.get("acceptance_criteria") or ())
            lines = [f"Task: {task}"] if task else []
            if criteria:
                lines.append("Acceptance criteria:")
                lines.extend(f"- {item}" for item in criteria)
            if not lines:
                return ProviderResult(capability_id, self.provider_id, gap_reason="not_found")
            unit = make_unit(
                slot_type="instructions",
                key="task:requirements",
                content="\n".join(lines),
                source_uri="task://requirements",
                authority="task-contract",
                transform_lineage=("raw", "task-requirements"),
            )
        else:
            criteria = tuple(hints.get("proof_criteria") or ())
            errors = tuple(hints.get("errors") or ())
            proof_lines: list[str] = []
            if criteria:
                proof_lines.append("Proof criteria:")
                proof_lines.extend(f"- {item}" for item in criteria)
            if errors:
                proof_lines.append("Active errors:")
                proof_lines.extend(f"- {item}" for item in errors)
            if not proof_lines:
                return ProviderResult(capability_id, self.provider_id, gap_reason="not_found")
            unit = make_unit(
                slot_type="policy",
                key="task:proof",
                content="\n".join(proof_lines),
                source_uri="task://proof",
                authority="task-contract",
                transform_lineage=("raw", "task-proof"),
            )
        units = (unit,) if unit is not None else ()
        return ProviderResult(capability_id, self.provider_id, units[:max_units])


class NativeGraphProvider(CapabilityProvider):
    """Optional bounded code.graph / tests.impact via CodeGraphEngine."""

    def __init__(self) -> None:
        super().__init__(
            provider_id=PROVIDER_ID,
            capabilities=frozenset({"code.graph", "tests.impact"}),
            authority="native-code-graph",
        )

    def provide(
        self,
        *,
        capability_id: str,
        query: str,
        repo_root: Path,
        max_units: int = DEFAULT_MAX_UNITS,
        hints: Mapping[str, Any] | None = None,
        plane: MemoryPlane | None = None,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    ) -> ProviderResult:
        from verdict.code_graph import CodeGraphEngine

        root = repo_root.resolve()
        engine = CodeGraphEngine()
        engine.parse_directory(root)
        symbols = _symbols_from_hints(hints, query)
        lines: list[str] = []
        if capability_id == "tests.impact":
            for symbol in symbols[:4]:
                tests = engine.tests_for(symbol)
                callers = engine.callers_of(symbol)
                lines.append(f"symbol={symbol}")
                for node in tests[:5]:
                    lines.append(f"  test {node.name} @ {node.file_path}:{node.line_number}")
                for node in callers[:5]:
                    lines.append(f"  caller {node.name} @ {node.file_path}:{node.line_number}")
        else:
            for symbol in symbols[:4]:
                lines.append(f"symbol={symbol}")
                for callee in engine.callees_of(symbol)[:8]:
                    lines.append(f"  callee {callee}")
                for node in engine.callers_of(symbol)[:8]:
                    lines.append(f"  caller {node.name} @ {node.file_path}")
        if len(lines) <= len(symbols[:4]):
            # Only symbol headers — treat as empty neighborhood.
            return ProviderResult(capability_id, self.provider_id, gap_reason="not_found")
        unit = make_unit(
            slot_type="evidence",
            key=f"graph:{capability_id}",
            content="\n".join(lines)[:max_file_bytes],
            source_uri=f"code://{capability_id}",
            authority="native-code-graph",
            transform_lineage=("raw", "code-graph-slice"),
        )
        units = (unit,) if unit is not None else ()
        return ProviderResult(capability_id, self.provider_id, units)


def build_native_providers() -> tuple[CapabilityProvider, ...]:
    return (
        NativeCodeProvider(),
        NativeDocsProvider(),
        NativeMemoryProvider(),
        NativeGitProvider(),
        NativeTaskProvider(),
        NativeGraphProvider(),
    )


__all__ = [
    "PROVIDER_ID",
    "CapabilityProvider",
    "CapabilityProviderProtocol",
    "NativeCodeProvider",
    "NativeDocsProvider",
    "NativeGitProvider",
    "NativeGraphProvider",
    "NativeMemoryProvider",
    "NativeTaskProvider",
    "ProviderResult",
    "build_native_providers",
    "make_unit",
]
