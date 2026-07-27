"""Deterministic, offline ingestion for Markdown and plain-text documents.

The adapter emits mappings shaped like :class:`verdict.memory_plane.MemoryRecord`
without importing or mutating ``MemoryPlane``.  It intentionally has no write,
network, model, or provider integration: callers decide whether and how the
returned records are committed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from verdict.memory_plane import MemoryRecord

DOCUMENT_ADAPTER_VERSION = "1"
MEMORY_SCHEMA_VERSION = 2
DEFAULT_MAX_FILE_BYTES = 1_048_576
DEFAULT_CHUNK_SIZE = 1_200

_SUPPORTED_SUFFIXES = {".md", ".markdown", ".txt"}
_QUARANTINE_DIRECTORIES = {
    ".cache",
    ".git",
    "__pycache__",
    "build",
    "cache",
    "caches",
    "coverage",
    "dist",
    "generated",
    "gen",
    "node_modules",
    "out",
    "target",
    "temp",
    "temporary",
    "tmp",
    "third-party",
    "third_party",
    "vendor",
    "vendors",
}
_QUARANTINE_FILENAMES = {"cache", "generated", "gen", "temp", "temporary", "tmp", "vendor"}
_GENERATED_SUFFIX = re.compile(r"(?:[._-](?:generated|gen|tmp|temp))$", re.IGNORECASE)


@dataclass(frozen=True)
class DocumentIngestionPolicy:
    """Boundaries applied before a document is read.

    Roots are explicit absolute directories.  Symlink roots, input paths, and
    descendants are rejected.  The configured size applies to each file and
    the chunk size applies to normalized Unicode characters.
    """

    allowed_roots: tuple[Path, ...]
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    chunk_size: int = DEFAULT_CHUNK_SIZE

    def __post_init__(self) -> None:
        if not self.allowed_roots:
            raise ValueError("at least one allowlisted root is required")
        if self.max_file_bytes <= 0:
            raise ValueError("max_file_bytes must be positive")
        if self.chunk_size <= 0:
            raise ValueError("chunk_size must be positive")

        roots: list[Path] = []
        for raw_root in self.allowed_roots:
            root = Path(raw_root).expanduser()
            if not root.is_absolute():
                raise ValueError("allowlisted roots must be absolute")
            if root.is_symlink():
                raise ValueError("allowlisted roots cannot be symlinks")
            if not root.exists() or not root.is_dir():
                raise ValueError(f"allowlisted root is not a directory: {root}")
            roots.append(root.resolve())
        object.__setattr__(self, "allowed_roots", tuple(sorted(set(roots), key=str)))


@dataclass(frozen=True)
class DocumentIssue:
    """A stable per-path outcome included in machine-readable reports."""

    path: str
    category: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "category": self.category, "reason": self.reason}


@dataclass(frozen=True)
class DocumentIngestionReport:
    """Machine-readable summary of one deterministic ingestion pass."""

    operation: str = "document-ingest"
    adapter_version: str = DOCUMENT_ADAPTER_VERSION
    dry_run: bool = False
    status: str = "ok"
    paths_seen: int = 0
    files_seen: int = 0
    files_accepted: int = 0
    chunks_emitted: int = 0
    skipped: int = 0
    quarantined: int = 0
    rejected: int = 0
    bytes_read: int = 0
    issues: tuple[DocumentIssue, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "adapter_version": self.adapter_version,
            "dry_run": self.dry_run,
            "status": self.status,
            "paths_seen": self.paths_seen,
            "files_seen": self.files_seen,
            "files_accepted": self.files_accepted,
            "chunks_emitted": self.chunks_emitted,
            "skipped": self.skipped,
            "quarantined": self.quarantined,
            "rejected": self.rejected,
            "bytes_read": self.bytes_read,
            "issues": [issue.to_dict() for issue in self.issues],
        }

    def to_json(self) -> str:
        """Serialize the report canonically for logs, automation, or replay."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class DocumentIngestionResult:
    """Records and the report produced by one ingestion pass."""

    records: tuple[dict[str, Any], ...]
    report: DocumentIngestionReport

    @property
    def memory_records(self) -> tuple[MemoryRecord, ...]:
        """Return emitted mappings as strict durable memory records."""
        return tuple(MemoryRecord(**record) for record in self.records)

    def to_dict(self) -> dict[str, Any]:
        return {
            "records": [dict(record) for record in self.records],
            "report": self.report.to_dict(),
        }


class DocumentIngestor:
    """Read allowlisted Markdown/text files and emit MemoryRecord mappings."""

    def __init__(self, policy: DocumentIngestionPolicy):
        self.policy = policy

    def ingest(
        self,
        paths: Iterable[str | os.PathLike[str]],
        *,
        namespace: str = "documents",
        scope: str = "default",
        source: str = "document",
        dry_run: bool = False,
    ) -> DocumentIngestionResult:
        """Ingest files or directories without changing local or remote state.

        ``dry_run`` is represented in the report and has no side effects.  The
        records are returned in both modes so a caller can inspect exactly
        what would be imported before passing them to a memory store.
        """
        _validate_label(namespace, "namespace")
        _validate_label(scope, "scope")
        _validate_label(source, "source")
        requested = list(paths)
        issues: list[DocumentIssue] = []
        records: list[dict[str, Any]] = []
        files_seen = files_accepted = skipped = quarantined = rejected = bytes_read = 0

        candidates: list[tuple[int, Path, Path]] = []
        for raw_path in requested:
            path = Path(raw_path).expanduser()
            root_info = self._root_for(path)
            if root_info is None:
                issues.append(
                    self._issue(path, "rejected", "path is outside the allowlisted roots")
                )
                rejected += 1
                continue
            root_index, root, candidate = root_info
            if _contains_symlink(root, candidate):
                issues.append(self._issue(candidate, "rejected", "symlink paths are not allowed"))
                rejected += 1
                continue
            if not candidate.exists():
                issues.append(self._issue(candidate, "rejected", "path does not exist"))
                rejected += 1
                continue
            if candidate.is_dir():
                for child in _walk_files(candidate):
                    candidates.append((root_index, root, child))
            else:
                candidates.append((root_index, root, candidate))

        candidates.sort(key=lambda item: (item[0], item[2].relative_to(item[1]).as_posix()))
        for root_index, root, path in candidates:
            files_seen += 1
            relative = path.relative_to(root).as_posix()
            if _is_quarantined(relative):
                issues.append(DocumentIssue(relative, "quarantined", _quarantine_reason(relative)))
                quarantined += 1
                continue
            if path.is_symlink() or _contains_symlink(root, path):
                issues.append(DocumentIssue(relative, "rejected", "symlink paths are not allowed"))
                rejected += 1
                continue
            if not _is_supported(path):
                issues.append(DocumentIssue(relative, "skipped", "unsupported document format"))
                skipped += 1
                continue
            try:
                payload = _read_document(path, self.policy.max_file_bytes)
            except _DocumentReadError as exc:
                issues.append(DocumentIssue(relative, "rejected", str(exc)))
                rejected += 1
                continue
            bytes_read += len(payload)
            try:
                text = _normalize_text(payload)
            except UnicodeDecodeError:
                issues.append(DocumentIssue(relative, "rejected", "document is not valid UTF-8"))
                rejected += 1
                continue
            if not text:
                issues.append(DocumentIssue(relative, "skipped", "document is empty"))
                skipped += 1
                continue
            document_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            chunks = _chunk_text(text, self.policy.chunk_size)
            format_name = "markdown" if path.suffix.lower() in {".md", ".markdown"} else "text"
            for index, chunk in enumerate(chunks):
                records.append(
                    _record_for_chunk(
                        chunk,
                        namespace=namespace,
                        scope=scope,
                        source=source,
                        relative_path=relative,
                        root_index=root_index,
                        document_hash=document_hash,
                        format_name=format_name,
                        chunk_index=index,
                        chunk_count=len(chunks),
                    )
                )
            files_accepted += 1

        report_status = "rejected" if rejected else "quarantined" if quarantined else "ok"
        report = DocumentIngestionReport(
            dry_run=dry_run,
            status=report_status,
            paths_seen=len(requested),
            files_seen=files_seen,
            files_accepted=files_accepted,
            chunks_emitted=len(records),
            skipped=skipped,
            quarantined=quarantined,
            rejected=rejected,
            bytes_read=bytes_read,
            issues=tuple(issues),
        )
        return DocumentIngestionResult(tuple(records), report)

    def _root_for(self, raw_path: Path) -> tuple[int, Path, Path] | None:
        if not raw_path.is_absolute():
            return None
        candidate = Path(os.path.abspath(raw_path))
        matches: list[tuple[int, Path]] = []
        for index, root in enumerate(self.policy.allowed_roots):
            try:
                candidate.relative_to(root)
            except ValueError:
                continue
            matches.append((index, root))
        if not matches:
            return None
        index, root = max(matches, key=lambda item: len(item[1].parts))
        return index, root, candidate

    @staticmethod
    def _issue(path: Path, category: str, reason: str) -> DocumentIssue:
        return DocumentIssue(str(path), category, reason)


class _DocumentReadError(ValueError):
    pass


def _walk_files(directory: Path) -> Iterator[Path]:
    for current, directories, filenames in os.walk(directory, followlinks=False):
        current_path = Path(current)
        directories[:] = sorted(directories)
        filenames = sorted(filenames)
        for name in filenames:
            yield current_path / name
        for name in list(directories):
            child = current_path / name
            if child.is_symlink():
                yield child
                directories.remove(name)


def _contains_symlink(root: Path, candidate: Path) -> bool:
    try:
        relative_parts = candidate.relative_to(root).parts
    except ValueError:
        return True
    current = root
    for part in relative_parts:
        current /= part
        if current.is_symlink():
            return True
    return False


def _read_document(path: Path, max_bytes: int) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as handle:
            payload = handle.read(max_bytes + 1)
    except (OSError, ValueError) as exc:
        raise _DocumentReadError(f"cannot read document: {exc}") from exc
    if len(payload) > max_bytes:
        raise _DocumentReadError("document exceeds max_file_bytes")
    return payload


def _normalize_text(payload: bytes) -> str:
    text = payload.decode("utf-8")
    text = text.lstrip("\ufeff")
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def _chunk_text(text: str, chunk_size: int) -> list[str]:
    paragraphs = [paragraph for paragraph in re.split(r"\n\s*\n", text) if paragraph]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        pieces = [
            paragraph[index : index + chunk_size] for index in range(0, len(paragraph), chunk_size)
        ]
        for piece in pieces:
            if not current:
                current = piece
            elif len(current) + 2 + len(piece) <= chunk_size:
                current = f"{current}\n\n{piece}"
            else:
                chunks.append(current)
                current = piece
    if current:
        chunks.append(current)
    return chunks


def _record_for_chunk(
    content: str,
    *,
    namespace: str,
    scope: str,
    source: str,
    relative_path: str,
    root_index: int,
    document_hash: str,
    format_name: str,
    chunk_index: int,
    chunk_count: int,
) -> dict[str, Any]:
    content_digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    identity = "\0".join(
        (str(root_index), relative_path, document_hash, str(chunk_index), content_digest)
    )
    record_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    provenance = {
        "source": source,
        "adapter": "memory-document",
        "adapter_version": DOCUMENT_ADAPTER_VERSION,
        "schema_version": MEMORY_SCHEMA_VERSION,
        "root_index": root_index,
        "relative_path": relative_path,
        "document_hash": document_hash,
        "format": format_name,
        "chunk_index": chunk_index,
        "chunk_count": chunk_count,
    }
    return {
        "record_id": record_id,
        "namespace": namespace,
        "key": f"{relative_path}#chunk-{chunk_index:04d}",
        "content": content,
        "source": source,
        "trust": "local-observation",
        "scope": scope,
        "metadata": {
            "adapter": "memory-document",
            "adapter_version": DOCUMENT_ADAPTER_VERSION,
            "document_hash": document_hash,
            "format": format_name,
            "relative_path": relative_path,
            "chunk_index": chunk_index,
            "chunk_count": chunk_count,
        },
        "created_at": 0.0,
        "updated_at": 0.0,
        "expires_at": None,
        "supersedes": None,
        "authority": "unverified",
        "authority_verified": False,
        "confidence": 1.0,
        "sensitivity": "standard",
        "provenance": provenance,
        "content_hash": content_digest,
        "schema_version": MEMORY_SCHEMA_VERSION,
        "status": "active",
    }


def _is_supported(path: Path) -> bool:
    return path.suffix.lower() in _SUPPORTED_SUFFIXES


def _is_quarantined(relative_path: str) -> bool:
    parts = Path(relative_path).parts
    directories = {part.lower() for part in parts[:-1]}
    if directories & _QUARANTINE_DIRECTORIES:
        return True
    stem = Path(parts[-1]).stem.lower()
    return stem in _QUARANTINE_FILENAMES or bool(_GENERATED_SUFFIX.search(stem))


def _quarantine_reason(relative_path: str) -> str:
    parts = Path(relative_path).parts
    directories = {part.lower() for part in parts[:-1]}
    matches = sorted(directories & _QUARANTINE_DIRECTORIES)
    if matches:
        return f"path contains quarantined directory: {matches[0]}"
    return "path appears temporary, generated, or vendor-managed"


def _validate_label(value: str, name: str) -> None:
    if not value or len(value) > 128 or any(ord(char) < 0x20 for char in value):
        raise ValueError(f"invalid {name}")


__all__ = [
    "DEFAULT_CHUNK_SIZE",
    "DEFAULT_MAX_FILE_BYTES",
    "DOCUMENT_ADAPTER_VERSION",
    "DocumentIngestionPolicy",
    "DocumentIngestionReport",
    "DocumentIngestionResult",
    "DocumentIngestor",
    "DocumentIssue",
]
