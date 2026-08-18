"""Offline dependency-documentation discovery and ingestion.

Produces a deduplicated list of external packages that the Verdict
ecosystem imports or interacts with, resolves documentation for each
package either from in-repo markdown or from a caller-supplied fetcher
(``context7``), and persists a provenance-attributed summary into the
local :class:`verdict.memory_plane.MemoryPlane`.

The module itself performs no network I/O.  The ``fetcher`` callable is
injected by the caller, so discovery and record construction are fully
testable offline.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from verdict.memory_document_adapter import MEMORY_SCHEMA_VERSION
from verdict.memory_plane import MemoryPlane, MemoryRecord

DEPENDENCY_INGEST_VERSION = "1"
DOC_NAMESPACE = "dependency-docs"
DEFAULT_SOURCE_ROOTS = ("verdict", "scripts")
DEFAULT_DOC_ROOTS = ("docs", "README.md")
MAX_FETCH_CAP = 12
_IMPORT_RE = re.compile(r"^(?:from\s+([a-zA-Z0-9_]+)|import\s+([a-zA-Z0-9_]+))")
_INTERACT_WITH = frozenset(
    {
        "anthropic",
        "openai",
        "ruflo",
        "ruvector",
        "omniroute",
        "openviking",
        "langgraph",
        "crewai",
        "litellm",
        "openrouter",
        "claude",
        "langchain",
    }
)
# These are stdlib modules never treated as external packages.
_STDLIB = frozenset(
    {
        "__future__",
        "abc",
        "argparse",
        "asyncio",
        "collections",
        "concurrent",
        "contextlib",
        "dataclasses",
        "datetime",
        "enum",
        "fcntl",
        "fnmatch",
        "hashlib",
        "hmac",
        "importlib",
        "inspect",
        "io",
        "ipaddress",
        "itertools",
        "json",
        "logging",
        "math",
        "os",
        "pathlib",
        "platform",
        "re",
        "shlex",
        "shutil",
        "signal",
        "socket",
        "sqlite3",
        "stat",
        "statistics",
        "subprocess",
        "sys",
        "tarfile",
        "tempfile",
        "threading",
        "time",
        "types",
        "typing",
        "unicodedata",
        "urllib",
        "uuid",
    }
)
_QUARANTINE_DIRS = frozenset(
    {".git", "__pycache__", ".venv", "venv", "node_modules", ".ruff_cache"}
)
_DEP_TABLE_KEYS = ("dependencies", "optional-dependencies", "peerDependencies", "devDependencies")


def _is_external(name: str) -> bool:
    """True for a real third-party package name, false for stdlib/self."""
    if name in _STDLIB:
        return False
    if name.startswith("@") or name.startswith("verdict"):
        return False
    return bool(re.match(r"^[A-Za-z0-9_.\-]+$", name))


def discover_dependencies(
    source_root: Path, *, pyproject: Path | None = None, package_json: Path | None = None
) -> list[str]:
    """Return a deduplicated, sorted list of external package names.

    ``pyproject.toml`` and ``package.json`` are parsed when supplied; the
    ``verdict/`` and ``scripts/`` trees are scanned for import statements.
    Provider frameworks referenced by name (anthropic, openai, ruflo, ...)
    are also detected so the list includes packages Verdict "interacts
    with" even when they are not hard dependencies.
    """
    counts: Counter[str] = Counter()
    for text in _walk_python_texts(source_root):
        for match in _IMPORT_RE.finditer(text):
            name = match.group(1) or match.group(2)
            if name in _STDLIB:
                continue
            counts[name] += 1
    for manifest in (pyproject, package_json):
        if manifest is None or not manifest.is_file():
            continue
        for name in _parse_manifest_dependencies(manifest):
            if not _is_external(name):
                continue
            counts[name] += 1
    if not counts:
        return []
    known = {name for name, _ in counts.most_common()}
    known |= _INTERACT_WITH
    return sorted(known)


def discover_dependencies_full(
    source_root: Path, *, pyproject: Path | None = None, package_json: Path | None = None
) -> list[PackageRef]:
    """Like :func:`discover_dependencies` but with usage counts and kinds."""
    counts = _count_imports(source_root)
    declared = set()
    for manifest in (pyproject, package_json):
        if manifest is not None and manifest.is_file():
            parsed = _parse_manifest_dependencies(manifest)
            declared.update(parsed)
            for name in parsed:
                if _is_external(name):
                    counts[name] += 1
    known = {name for name, _ in counts.most_common()}
    known |= _INTERACT_WITH
    refs: list[PackageRef] = []
    for name in sorted(known):
        refs.append(
            PackageRef(
                name=name,
                usage=counts.get(name, 0),
                kind="declared" if name in declared else "referenced",
            )
        )
    return refs


def find_in_repo_docs(package: str, repo_root: Path) -> Path | None:
    """Return the first in-repo markdown file that mentions the package."""
    if not repo_root.is_dir():
        return None
    for path in _walk_docs(repo_root):
        try:
            head = path.read_text(encoding="utf-8", errors="ignore")[:200_000]
        except OSError:
            continue
        if re.search(rf"\b{re.escape(package)}\b", head, re.IGNORECASE):
            return path
    return None


def ingest_dependency_docs(
    plane: MemoryPlane,
    packages: Sequence[PackageRef],
    repo_root: Path,
    *,
    fetcher: Callable[[str], str] | None = None,
    cap: int = MAX_FETCH_CAP,
    now: float | None = None,
) -> list[DependencyDocResult]:
    """Persist a doc record per package and return one result per package."""
    results: list[DependencyDocResult] = []
    fetched = 0
    for package in packages:
        if package.name in _STDLIB:
            continue
        local = find_in_repo_docs(package.name, repo_root)
        if local is not None:
            record = _make_record(package.name, package.kind, "in-repo", str(local))
            plane.put(record)
            results.append(
                DependencyDocResult(
                    package=package.name, source="in-repo", source_path=str(local), status="exists"
                )
            )
            continue
        if package.kind != "declared" or fetched >= cap or fetcher is None:
            results.append(
                DependencyDocResult(
                    package=package.name, source="skipped", source_path=None, status="skipped"
                )
            )
            continue
        try:
            fetched += 1
            summary = fetcher(package.name)
        except Exception as exc:
            results.append(
                DependencyDocResult(
                    package=package.name,
                    source="context7",
                    source_path=None,
                    status="failed",
                    error=str(exc),
                )
            )
            continue
        if not summary.strip():
            results.append(
                DependencyDocResult(
                    package=package.name,
                    source="context7",
                    source_path=None,
                    status="failed",
                    error="empty fetcher result",
                )
            )
            continue
        record = _make_record(
            package.name, package.kind, "context7", None, summary=summary, now=now
        )
        plane.put(record)
        results.append(
            DependencyDocResult(
                package=package.name, source="context7", source_path=None, status="ingested"
            )
        )
    return results


def build_memory_record(
    package: str,
    *,
    source: str,
    source_path: str | None = None,
    summary: str = "",
    version: str | None = None,
    now: float | None = None,
) -> MemoryRecord:
    """Construct a dependency-docs memory record with full provenance."""
    content = summary or (f"Referenced in-repo by {source_path}" if source_path else "")
    ts = datetime.now(timezone.utc) if now is None else datetime.fromtimestamp(now, tz=timezone.utc)
    provenance: dict[str, Any] = {
        "source": source,
        "adapter": "dependency-ingest",
        "adapter_version": DEPENDENCY_INGEST_VERSION,
        "schema_version": MEMORY_SCHEMA_VERSION,
        "retrieved_at": ts.isoformat(),
    }
    if source_path:
        provenance["source_path"] = source_path
    if version:
        provenance["version"] = version
    return MemoryRecord(
        record_id=_record_id(package, source, source_path or "", content),
        namespace=DOC_NAMESPACE,
        key=package,
        content=content,
        source=source,
        trust="local-observation",
        scope="default",
        metadata={"adapter": "dependency-ingest", "package": package, "source": source},
        created_at=ts.timestamp(),
        updated_at=ts.timestamp(),
        confidence=1.0,
        sensitivity="standard",
        provenance=provenance,
    )


@dataclass(frozen=True)
class PackageRef:
    """One discovered package with usage context."""

    name: str
    usage: int
    kind: str


@dataclass(frozen=True)
class DependencyDocResult:
    """Stable result row for the report table."""

    package: str
    source: str
    source_path: str | None
    status: str
    error: str | None = None


def report_rows(
    results: Iterable[DependencyDocResult],
    *,
    header: tuple[str, str, str] = ("package", "source", "status"),
) -> str:
    """Render a deterministic text table for the report."""
    rows = list(results)
    if not rows:
        return "".join(_format_line(header))
    lines = [_format_line(header), _format_line(("", "", ""))]
    for row in sorted(rows, key=lambda r: (r.package, r.source)):
        lines.append(_format_line((row.package, row.source, row.status)))
    return "\n".join(lines) + "\n"


def _count_imports(source_root: Path) -> Counter[str]:
    counts: Counter[str] = Counter()
    for text in _walk_python_texts(source_root):
        for match in _IMPORT_RE.finditer(text):
            name = match.group(1) or match.group(2)
            if name not in _STDLIB:
                counts[name] += 1
    return counts


def _walk_python_texts(source_root: Path) -> Iterable[str]:
    if not source_root.is_dir():
        return
    for path in source_root.rglob("*.py"):
        parts = set(path.parts)
        if parts & _QUARANTINE_DIRS:
            continue
        try:
            yield path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue


def _walk_docs(repo_root: Path) -> Iterable[Path]:
    seen: set[Path] = set()
    for pattern in ("docs/**/*.md", "docs/**/*.markdown", "README.md"):
        for path in repo_root.glob(pattern):
            if path.is_file() and path not in seen:
                seen.add(path)
                yield path
    for candidate in repo_root.glob("README*"):
        if candidate.is_file() and candidate not in seen:
            seen.add(candidate)
            yield candidate


def _parse_manifest_dependencies(manifest: Path) -> set[str]:
    text = manifest.read_text(encoding="utf-8", errors="ignore")
    names: set[str] = set()
    if manifest.name == "package.json":
        try:
            payload = json.loads(text)
        except ValueError:
            return names
        for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
            names.update(str(name) for name in payload.get(key, {}))
        return names
    if manifest.name == "pyproject.toml":
        return _parse_pyproject_dependencies(text)
    return names


_TOML_DEP_KEYS = frozenset(
    {"dependencies", "optional-dependencies", "server", "dashboard", "all", "dev"}
)


def _parse_pyproject_dependencies(text: str) -> set[str]:
    """Extract package names from dependency array tables in ``[project]``.

    Tracks the current TOML table and array state so optional and dev
    tables (multi-line arrays) are parsed while unrelated arrays such as
    ``keywords`` and build artifacts are ignored.
    """
    names: set[str] = set()
    in_array = False
    for raw in _iter_toml_lines(text):
        stripped = raw.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_array = False
            continue
        if in_array:
            for quoted in re.findall(r'"([^"]+)"', raw):
                names.add(_name_from(quoted))
            if "]" in raw:
                in_array = False
            continue
        key_match = re.match(r"^\s*([A-Za-z0-9_-]+)\s*=\s*\[", raw)
        if key_match and key_match.group(1) in _TOML_DEP_KEYS:
            in_array = True
            for quoted in re.findall(r'"([^"]+)"', raw):
                names.add(_name_from(quoted))
            if "]" in raw:
                in_array = False
    return names


def _name_from(quoted: str) -> str:
    return re.split(r"[<>=!~]", quoted, maxsplit=1)[0].strip()


def _iter_toml_lines(text: str) -> Iterable[str]:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue
        yield line


def _find_list_names(line: str) -> list[str]:
    names: list[str] = []
    if "[" in line:
        line = line.split("[", 1)[1]
    for segment in re.split(r"[,\]]", line):
        segment = segment.split("#", 1)[0].strip().strip('"').strip("'")
        if "=" in segment:
            segment = segment.split("=", 1)[0].strip().strip('"').strip("'")
        match = re.match(r"^[A-Za-z0-9_.\-]+", segment)
        if match:
            names.append(match.group(0))
    return names


def _make_record(
    package: str,
    kind: str,
    source: str,
    path_value: str | None,
    *,
    summary: str = "",
    now: float | None = None,
) -> MemoryRecord:
    ts = datetime.now(timezone.utc) if now is None else datetime.fromtimestamp(now, tz=timezone.utc)
    provenance: dict[str, Any] = {
        "source": source,
        "adapter": "dependency-ingest",
        "adapter_version": DEPENDENCY_INGEST_VERSION,
        "schema_version": MEMORY_SCHEMA_VERSION,
        "retrieved_at": ts.isoformat(),
        "package_kind": kind,
    }
    content: str
    if summary:
        content = summary
        provenance["retrieval"] = "context7"
    else:
        content = f"Referenced in-repo by {path_value}"
        if path_value:
            provenance["source_path"] = path_value
    return MemoryRecord(
        record_id=_record_id(package, source, path_value or "", content),
        namespace=DOC_NAMESPACE,
        key=package,
        content=content,
        source=source,
        trust="local-observation",
        scope="default",
        metadata={"adapter": "dependency-ingest", "package": package, "source": source},
        created_at=ts.timestamp(),
        updated_at=ts.timestamp(),
        confidence=1.0,
        sensitivity="standard",
        provenance=provenance,
    )


def _record_id(package: str, source: str, extra: str, content: str) -> str:
    import hashlib

    identity = "\0".join((package, source, extra, content))
    return "dep_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]


def _format_line(values: Sequence[str]) -> str:
    if len(values) == 1:
        return str(values[0]) + "\n"
    return "\t".join(str(value) for value in values) + "\n"


__all__ = [
    "DEPENDENCY_INGEST_VERSION",
    "DOC_NAMESPACE",
    "DependencyDocResult",
    "PackageRef",
    "build_memory_record",
    "discover_dependencies",
    "discover_dependencies_full",
    "find_in_repo_docs",
    "ingest_dependency_docs",
    "report_rows",
]
