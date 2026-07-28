"""Authoritative documentation discovery, verification, and ingestion.

The preflight is deliberately deterministic at the boundary: local sources
are identified by their repository commit and remote sources by their
resolved commit, while every stored chunk carries both the raw and normalized
content hashes.  The source manifest is stored in the same shared
``~/.verdict/memory.db`` as the chunks, so a task can fail closed when a source
is missing, unverifiable, or stale.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

from verdict.memory_plane import MemoryPlane, MemoryRecord

DOCUMENT_PREFLIGHT_VERSION = "1"
DEFAULT_FRESHNESS_SECONDS = 86_400
DEFAULT_RUFLO_REPOSITORY = "https://github.com/ruvnet/ruflo"
DEFAULT_RUVECTOR_REPOSITORY = "https://github.com/ruvnet/RuVector"
DEFAULT_RUVECTOR_API = "https://api.github.com/repos/ruvnet/RuVector"


class DocumentationPreflightError(RuntimeError):
    """Raised when implementation cannot proceed with trusted documentation."""


@dataclass(frozen=True)
class DocumentationSource:
    """An authoritative local checkout or remote repository source."""

    source_id: str
    ecosystem: str
    repository: str
    ref: str
    root: Path | None = None
    api_base: str | None = None
    freshness_seconds: int = DEFAULT_FRESHNESS_SECONDS


@dataclass(frozen=True)
class DocumentationEntry:
    """An ADR/document in a source inventory."""

    source: DocumentationSource
    relative_path: str
    local_path: Path | None
    raw_url: str | None
    tree_sha: str | None = None


@dataclass(frozen=True)
class DocumentationPreflightReport:
    """Machine-readable result of a documentation preflight."""

    status: str
    sources: int
    inventory: int
    ingested: int
    skipped_fresh: int
    stale: int
    missing: int
    unverifiable: int
    errors: tuple[str, ...] = ()
    source_commits: dict[str, str] | None = None
    inventory_details: dict[str, tuple[dict[str, Any], ...]] | None = None
    duplicate_projections: tuple[dict[str, Any], ...] = ()

    @property
    def passed(self) -> bool:
        return self.status == "ready"

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": "documentation-preflight",
            "preflight_version": DOCUMENT_PREFLIGHT_VERSION,
            "status": self.status,
            "passed": self.passed,
            "sources": self.sources,
            "inventory": self.inventory,
            "ingested": self.ingested,
            "skipped_fresh": self.skipped_fresh,
            "stale": self.stale,
            "missing": self.missing,
            "unverifiable": self.unverifiable,
            "errors": list(self.errors),
            "source_commits": dict(self.source_commits or {}),
            "inventory_details": {
                source_id: [dict(item) for item in entries]
                for source_id, entries in (self.inventory_details or {}).items()
            },
            "duplicate_projections": [dict(item) for item in self.duplicate_projections],
        }


def shared_memory_path(home: Path | None = None) -> Path:
    """Return the cross-tool memory database path."""

    configured = os.getenv("VERDICT_MEMORY_PLANE_PATH")
    return (
        Path(configured).expanduser()
        if configured
        else (home or Path.home()) / ".verdict" / "memory.db"
    )


def discover_sources(repo_root: Path | None = None) -> tuple[DocumentationSource, ...]:
    """Discover project, Ruflo, and RuVector authoritative sources.

    Environment variables make the lookup portable while the sibling/global
    checkout fallbacks support the documented local development layout.
    """

    root = (repo_root or Path.cwd()).resolve()
    sources: list[DocumentationSource] = []
    project_docs = root
    if (project_docs / "docs").is_dir():
        sources.append(
            DocumentationSource(
                "verdict-core-docs",
                "verdict-core",
                "https://github.com/mrnicholasbcarter-code/verdict-core",
                _git_commit(root),
                project_docs,
            )
        )

    ruflo_value = os.getenv("VERDICT_RUFLO_ROOT")
    candidates = [Path(ruflo_value).expanduser()] if ruflo_value else []
    candidates.extend(root.parents[index] / "ruflo" for index in range(min(3, len(root.parents))))
    ruflo_root = next((candidate.resolve() for candidate in candidates if candidate.is_dir()), None)
    if ruflo_root:
        sources.append(
            DocumentationSource(
                "ruflo", "ruflo", DEFAULT_RUFLO_REPOSITORY, _git_commit(ruflo_root), ruflo_root
            )
        )

    ruvector_value = os.getenv("VERDICT_RUVECTOR_ROOT")
    ruvector_root = Path(ruvector_value).expanduser().resolve() if ruvector_value else None
    if ruvector_root and ruvector_root.is_dir():
        ruvector_commit = _git_commit(ruvector_root)
        if ruvector_commit == "unknown":
            sources.append(
                DocumentationSource(
                    "ruvector",
                    "ruvector",
                    DEFAULT_RUVECTOR_REPOSITORY,
                    os.getenv("VERDICT_RUVECTOR_REF", "main"),
                    api_base=DEFAULT_RUVECTOR_API,
                )
            )
            return tuple(sources)
        sources.append(
            DocumentationSource(
                "ruvector", "ruvector", DEFAULT_RUVECTOR_REPOSITORY, ruvector_commit, ruvector_root
            )
        )
    else:
        sources.append(
            DocumentationSource(
                "ruvector",
                "ruvector",
                DEFAULT_RUVECTOR_REPOSITORY,
                os.getenv("VERDICT_RUVECTOR_REF", "main"),
                api_base=DEFAULT_RUVECTOR_API,
            )
        )
    return tuple(sources)


def run_documentation_preflight(
    *,
    repo_root: Path | None = None,
    memory_path: Path | None = None,
    fix: bool = False,
    sources: Iterable[DocumentationSource] | None = None,
    now: float | None = None,
    fetch: Callable[[str], bytes] | None = None,
) -> DocumentationPreflightReport:
    """Check and optionally repair authoritative documentation memory.

    ``fix=False`` performs a read-only verification.  ``fix=True`` fetches
    absent/stale content and atomically appends verified records to the shared
    memory plane.  A network or provenance failure is never converted into a
    ready result.
    """

    current = time.time() if now is None else now
    selected = tuple(discover_sources(repo_root) if sources is None else sources)
    inventory_count = ingested = skipped = stale = missing = unverifiable = 0
    errors: list[str] = []
    commits: dict[str, str] = {}
    inventory_details: dict[str, tuple[dict[str, Any], ...]] = {}
    target_memory_path = memory_path or shared_memory_path()
    # A read-only diagnostic must not initialize a missing shared database.
    # Use an in-memory plane until --fix explicitly authorizes persistence.
    plane = MemoryPlane(target_memory_path if fix or target_memory_path.exists() else ":memory:")
    try:
        resolved_sources: list[DocumentationSource] = []
        for source in selected:
            try:
                source = _resolve_source(source, fetch=fetch)
            except Exception as exc:
                unverifiable += 1
                errors.append(f"{source.source_id}:resolve:{type(exc).__name__}:{exc}")
                resolved_sources.append(source)
                continue
            resolved_sources.append(source)
            try:
                entries = _inventory(source, fetch=fetch)
                commits[source.source_id] = source.ref
            except Exception as exc:
                unverifiable += 1
                errors.append(f"{source.source_id}:inventory:{type(exc).__name__}:{exc}")
                continue
            inventory_count += len(entries)
            source_details: list[dict[str, Any]] = []
            for entry in entries:
                key = _manifest_key(entry)
                manifest = _manifest_for(plane, key)
                detail: dict[str, Any] = {
                    "path": entry.relative_path,
                    "repository": entry.source.repository,
                    "commit": entry.source.ref,
                    "source_url": entry.raw_url or _local_url(entry.source, entry.relative_path),
                    "tree_sha": entry.tree_sha,
                }
                if manifest:
                    detail.update(
                        {
                            "raw_hash": (manifest.provenance or {}).get("raw_hash"),
                            "document_hash": (manifest.provenance or {}).get("document_hash"),
                            "fresh_until": manifest.expires_at,
                            "verified": manifest.authority_verified,
                        }
                    )
                if manifest and _manifest_is_fresh(manifest, entry, current, fetch=fetch):
                    source_details.append(detail)
                    skipped += 1
                    continue
                if manifest:
                    stale += 1
                else:
                    missing += 1
                if not fix:
                    continue
                try:
                    payload = _read_entry(entry, fetch=fetch)
                    raw_hash = hashlib.sha256(payload).hexdigest()
                    text = _normalize(payload)
                    if not text:
                        raise ValueError("empty document")
                    records = _records_for_entry(entry, text, raw_hash, current)
                    for record in records:
                        plane.put_verified(record)
                    manifest_record = _manifest_record(entry, raw_hash, text, current)
                    plane.put_verified(manifest_record)
                    detail.update(
                        {
                            "raw_hash": raw_hash,
                            "document_hash": (manifest_record.provenance or {}).get(
                                "document_hash"
                            ),
                            "fresh_until": manifest_record.expires_at,
                            "verified": True,
                        }
                    )
                    ingested += 1
                except Exception as exc:
                    unverifiable += 1
                    errors.append(
                        f"{source.source_id}:{entry.relative_path}:{type(exc).__name__}:{exc}"
                    )
                source_details.append(detail)
            inventory_details[source.source_id] = tuple(source_details)
        duplicate_projections = _duplicate_projections(inventory_details)
        problems = stale + missing + unverifiable
        status = "ready" if problems == 0 else "blocked"
        if fix and (missing + stale) and unverifiable == 0:
            # A repair is ready only when every inventory item now has a fresh,
            # verified manifest; this also catches partial fetches.
            status = "ready" if _all_fresh(plane, resolved_sources, current, fetch) else "blocked"
        return DocumentationPreflightReport(
            status=status,
            sources=len(selected),
            inventory=inventory_count,
            ingested=ingested,
            skipped_fresh=skipped,
            stale=stale,
            missing=missing,
            unverifiable=unverifiable,
            errors=tuple(errors),
            source_commits=commits,
            inventory_details=inventory_details,
            duplicate_projections=duplicate_projections,
        )
    finally:
        plane.close()


def require_documentation_preflight(**kwargs: Any) -> DocumentationPreflightReport:
    """Run repair-enabled preflight or raise before implementation starts."""

    report = run_documentation_preflight(fix=True, **kwargs)
    if not report.passed:
        raise DocumentationPreflightError(json.dumps(report.to_dict(), sort_keys=True))
    return report


def _inventory(
    source: DocumentationSource, *, fetch: Callable[[str], bytes] | None
) -> list[DocumentationEntry]:
    if source.root:
        tracked_tree = _git_tree(source) if len(source.ref) == 40 else None
        entries: list[DocumentationEntry] = []
        paths = (
            [
                (source.root / relative, Path(relative), blob_sha)
                for _path, relative, blob_sha in (tracked_tree or [])
            ]
            if tracked_tree is not None
            else [(path, path.relative_to(source.root), None) for path in source.root.rglob("*.md")]
        )
        for path, relative_path, blob_sha in sorted(paths, key=lambda item: item[1].as_posix()):
            if path.is_file() and _is_adr_path(relative_path, source.ecosystem):
                relative = relative_path.as_posix()
                entries.append(
                    DocumentationEntry(
                        source, relative, path, _local_url(source, relative), blob_sha
                    )
                )
        return entries
    if not source.api_base:
        raise ValueError("source has no local root or remote API")
    api_url = f"{source.api_base}/git/trees/{source.ref}?recursive=1"
    payload = json.loads(_download(api_url, fetch).decode("utf-8"))
    if payload.get("truncated"):
        raise ValueError("remote documentation tree is truncated")
    entries = []
    for item in payload.get("tree", []):
        item_path = str(item.get("path", ""))
        if (
            item.get("type") == "blob"
            and item_path.endswith(".md")
            and _is_adr_path(Path(item_path), source.ecosystem)
        ):
            raw_url = _raw_url(source, item_path)
            entries.append(
                DocumentationEntry(source, item_path, None, raw_url, str(item.get("sha", "")))
            )
    return sorted(entries, key=lambda item: item.relative_path)


def _resolve_source(
    source: DocumentationSource, *, fetch: Callable[[str], bytes] | None
) -> DocumentationSource:
    if source.root:
        if source.ref == "unknown":
            raise ValueError("local source does not resolve to an immutable Git commit")
        return source
    if not source.api_base or _is_git_sha(source.ref):
        return source
    ref_url = f"{source.api_base}/git/ref/heads/{source.ref}"
    payload = json.loads(_download(ref_url, fetch).decode("utf-8"))
    commit = str(payload.get("object", {}).get("sha", ""))
    if len(commit) != 40:
        raise ValueError("remote source did not return a 40-character commit")
    return replace(source, ref=commit)


def _is_git_sha(value: str) -> bool:
    return len(value) == 40 and all(char in "0123456789abcdefABCDEF" for char in value)


def _is_adr_path(path: Path, ecosystem: str = "") -> bool:
    """Return whether a Markdown path is an authoritative ADR projection.

    Ruflo's canonical ADR roots are explicit so nearby references in reports,
    READMEs, and examples are not silently ingested. RuVector has additional
    component ADR projections whose filenames carry the ADR identifier; those
    are authoritative when they are not generic agent/tool names.
    """
    parts = {part.lower() for part in path.parts[:-1]}
    if path.name.lower() == "readme.md":
        return False
    if bool(parts & {"adr", "adrs"}) or "implementation/adrs" in path.as_posix().lower():
        return True
    if ecosystem != "ruvector":
        return False
    name = path.name.lower()
    if name == "adr-architect.md":
        return False
    return name.startswith("adr-") or name.startswith("adr_")


def _read_entry(entry: DocumentationEntry, *, fetch: Callable[[str], bytes] | None) -> bytes:
    if entry.local_path:
        if (
            entry.source.root
            and (entry.source.root / ".git").exists()
            and len(entry.source.ref) == 40
        ):
            try:
                result = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(entry.source.root),
                        "show",
                        f"{entry.source.ref}:{entry.relative_path}",
                    ],
                    capture_output=True,
                    check=True,
                    timeout=5,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise ValueError(f"local authoritative document unavailable: {exc}") from exc
            payload = result.stdout
        else:
            payload = entry.local_path.read_bytes()
        _verify_tree_sha(payload, entry.tree_sha)
        return payload
    if not entry.raw_url:
        raise ValueError("entry has no readable source")
    payload = _download(entry.raw_url, fetch)
    _verify_tree_sha(payload, entry.tree_sha)
    return payload


def _verify_tree_sha(payload: bytes, tree_sha: str | None) -> None:
    """Verify Git's blob object SHA when the source inventory provides one."""

    if not tree_sha:
        return
    digest = hashlib.sha1(
        b"blob " + str(len(payload)).encode("ascii") + b"\0" + payload
    ).hexdigest()  # nosec B324: Git blob compatibility hash
    if digest != tree_sha:
        raise ValueError("source blob SHA does not match fetched content")


def _git_tree(source: DocumentationSource) -> list[tuple[Path, str, str]] | None:
    """Read the immutable committed file tree for a local Git source."""

    if not source.root:
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(source.root), "ls-tree", "-r", "--full-tree", source.ref],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    tree: list[tuple[Path, str, str]] = []
    for line in result.stdout.splitlines():
        metadata, relative = line.split("\t", 1)
        fields = metadata.split()
        if len(fields) != 3 or fields[1] != "blob" or len(fields[2]) != 40:
            continue
        tree.append((source.root / relative, relative, fields[2]))
    return tree


def _download(url: str, fetch: Callable[[str], bytes] | None) -> bytes:
    if fetch:
        return fetch(url)
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "verdict-core"}
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:  # nosec B310: allowlisted HTTPS source
            return cast(bytes, response.read())
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ValueError(f"authoritative fetch failed: {exc}") from exc


def _records_for_entry(
    entry: DocumentationEntry, text: str, raw_hash: str, now: float
) -> list[MemoryRecord]:
    chunks = _chunks(text, 1_200)
    result: list[MemoryRecord] = []
    for index, chunk in enumerate(chunks):
        chunk_hash = hashlib.sha256(chunk.encode("utf-8")).hexdigest()
        provenance = _provenance(entry, raw_hash, text, now, index, len(chunks))
        identity = ":".join(
            (
                entry.source.source_id,
                entry.relative_path,
                entry.source.ref,
                entry.tree_sha or "",
                raw_hash,
                str(index),
            )
        )
        result.append(
            MemoryRecord(
                # The resolved ref/tree identity is part of the record ID so
                # an unchanged document at a new authoritative commit still
                # refreshes provenance instead of being mistaken for an
                # idempotent duplicate.  Keep raw_hash in the identity as a
                # guard for test/local sources whose content may change
                # without a corresponding Git tree SHA.
                record_id=hashlib.sha256(identity.encode()).hexdigest(),
                namespace="authoritative-docs",
                key=f"{entry.source.source_id}:{entry.relative_path}#chunk-{index:04d}",
                content=chunk,
                source=f"{entry.source.ecosystem}:authoritative",
                trust="upstream-documented",
                scope="shared",
                metadata={
                    "document_hash": provenance["document_hash"],
                    "raw_hash": raw_hash,
                    "chunk_index": index,
                    "chunk_count": len(chunks),
                },
                expires_at=now + entry.source.freshness_seconds,
                authority="authoritative",
                authority_verified=True,
                confidence=1.0,
                provenance={**provenance, "chunk_hash": chunk_hash},
                content_hash=chunk_hash,
            )
        )
    return result


def _manifest_record(
    entry: DocumentationEntry, raw_hash: str, text: str, now: float
) -> MemoryRecord:
    provenance = _provenance(entry, raw_hash, text, now, 0, 1)
    content = json.dumps({"path": entry.relative_path, **provenance}, sort_keys=True)
    identity = ":".join(
        (
            "manifest",
            entry.source.source_id,
            entry.relative_path,
            entry.source.ref,
            entry.tree_sha or "",
            raw_hash,
        )
    )
    return MemoryRecord(
        record_id=hashlib.sha256(identity.encode()).hexdigest(),
        namespace="documentation-manifest",
        key=_manifest_key(entry),
        content=content,
        source=f"{entry.source.ecosystem}:authoritative",
        trust="upstream-documented",
        scope="shared",
        metadata={
            "raw_hash": raw_hash,
            "document_hash": provenance["document_hash"],
            "commit": entry.source.ref,
        },
        expires_at=now + entry.source.freshness_seconds,
        authority="authoritative",
        authority_verified=True,
        confidence=1.0,
        provenance=provenance,
    )


def _provenance(
    entry: DocumentationEntry, raw_hash: str, text: str, now: float, index: int, count: int
) -> dict[str, Any]:
    return {
        "preflight_version": DOCUMENT_PREFLIGHT_VERSION,
        "ecosystem": entry.source.ecosystem,
        "repository": entry.source.repository,
        "source_url": entry.raw_url or _local_url(entry.source, entry.relative_path),
        "ref": entry.source.ref,
        "commit": entry.source.ref,
        "relative_path": entry.relative_path,
        "raw_hash": raw_hash,
        "document_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "retrieved_at": now,
        "fresh_until": now + entry.source.freshness_seconds,
        "chunk_index": index,
        "chunk_count": count,
        "tree_sha": entry.tree_sha,
    }


def _manifest_key(entry: DocumentationEntry) -> str:
    return f"{entry.source.source_id}:{entry.relative_path}"


def _duplicate_projections(
    inventory_details: dict[str, tuple[dict[str, Any], ...]],
) -> tuple[dict[str, Any], ...]:
    """Report same-blob path projections without collapsing retrieval paths."""

    by_sha: dict[tuple[str, str], list[str]] = {}
    for source_id, entries in inventory_details.items():
        for entry in entries:
            tree_sha = entry.get("tree_sha")
            if tree_sha:
                by_sha.setdefault((source_id, str(tree_sha)), []).append(str(entry["path"]))
    return tuple(
        {"source": source_id, "tree_sha": tree_sha, "paths": sorted(paths)}
        for (source_id, tree_sha), paths in sorted(by_sha.items())
        if len(paths) > 1
    )


def _manifest_is_fresh(
    record: MemoryRecord,
    entry: DocumentationEntry,
    now: float,
    *,
    fetch: Callable[[str], bytes] | None,
) -> bool:
    data = record.provenance or {}
    if not (
        record.authority_verified
        and record.expires_at is not None
        and record.expires_at > now
        and data.get("commit") == entry.source.ref
        and data.get("repository") == entry.source.repository
        and isinstance(data.get("raw_hash"), str)
    ):
        return False
    try:
        expected_hash = cast(str, data["raw_hash"])
        return hashlib.sha256(_read_entry(entry, fetch=fetch)).hexdigest() == expected_hash
    except (OSError, ValueError, TypeError):
        return False


def _all_fresh(
    plane: MemoryPlane,
    sources: Iterable[DocumentationSource],
    now: float,
    fetch: Callable[[str], bytes] | None,
) -> bool:
    # Re-inventory after repair. This is intentionally strict: a partial fetch
    # cannot unlock implementation.
    for source in sources:
        try:
            entries = _inventory(source, fetch=fetch)
        except Exception:
            return False
        for entry in entries:
            manifest = _manifest_for(plane, _manifest_key(entry))
            if manifest is None or not _manifest_is_fresh(manifest, entry, now, fetch=fetch):
                return False
    return True


def _git_commit(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=3,
        )
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _manifest_for(plane: MemoryPlane, key: str) -> MemoryRecord | None:
    """Read a manifest without applying wall-clock expiry to injected clocks."""

    history = plane.history("documentation-manifest", key, scope="shared")
    active = [record for record in history if record.status == "active"]
    return active[-1] if active else None


def _local_url(source: DocumentationSource, relative: str) -> str:
    return f"{source.repository}/blob/{source.ref}/{relative}"


def _raw_url(source: DocumentationSource, relative: str) -> str:
    repository = source.repository.rstrip("/")
    if repository.startswith("https://github.com/"):
        return (
            repository.replace("https://github.com/", "https://raw.githubusercontent.com/", 1)
            + f"/{source.ref}/{relative}"
        )
    raise ValueError("remote source repository is not an allowlisted GitHub HTTPS source")


def _normalize(payload: bytes) -> str:
    text = payload.decode("utf-8").lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def _chunks(text: str, size: int) -> list[str]:
    paragraphs = [part for part in text.split("\n\n") if part]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        for start in range(0, len(paragraph), size):
            piece = paragraph[start : start + size]
            if current and len(current) + 2 + len(piece) > size:
                chunks.append(current)
                current = ""
            current = piece if not current else f"{current}\n\n{piece}"
    if current:
        chunks.append(current)
    return chunks


__all__ = [
    "DOCUMENT_PREFLIGHT_VERSION",
    "DocumentationEntry",
    "DocumentationPreflightError",
    "DocumentationPreflightReport",
    "DocumentationSource",
    "discover_sources",
    "require_documentation_preflight",
    "run_documentation_preflight",
    "shared_memory_path",
]
