"""Versioned contracts emitted by the legacy MasterDocs importer."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from verdict.memory_plane import MemoryRecord

MASTERDOCS_ADAPTER_VERSION = "2"
MASTERDOCS_SCHEMA_VERSION = "1"
DEFAULT_MAX_CONTENT_BYTES = 1_048_576
DEFAULT_CHUNK_SIZE = 1_200


@dataclass(frozen=True)
class MasterDocsIssue:
    """A stable, non-sensitive outcome for one source row or path."""

    path: str
    category: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "category": self.category, "reason": self.reason}


@dataclass(frozen=True)
class MasterDocsIngestionReport:
    """Machine-readable evidence for one legacy migration pass."""

    operation: str = "masterdocs-ingest"
    adapter_version: str = MASTERDOCS_ADAPTER_VERSION
    schema_version: str = MASTERDOCS_SCHEMA_VERSION
    status: str = "ok"
    total_found: int = 0
    documents_seen: int = 0
    documents_accepted: int = 0
    chunks_emitted: int = 0
    ingested: int = 0
    quarantined: int = 0
    rejected: int = 0
    duplicates: int = 0
    skipped: int = 0
    bytes_read: int = 0
    ingest_timestamp: float = 0.0
    content_hashes: tuple[str, ...] = ()
    quarantined_paths: tuple[str, ...] = ()
    issues: tuple[MasterDocsIssue, ...] = ()
    manifest_hash: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "adapter_version": self.adapter_version,
            "schema_version": self.schema_version,
            "status": self.status,
            "total_found": self.total_found,
            "documents_seen": self.documents_seen,
            "documents_accepted": self.documents_accepted,
            "chunks_emitted": self.chunks_emitted,
            "ingested": self.ingested,
            "quarantined": self.quarantined,
            "rejected": self.rejected,
            "duplicates": self.duplicates,
            "skipped": self.skipped,
            "bytes_read": self.bytes_read,
            "ingest_timestamp": self.ingest_timestamp,
            "content_hashes": list(self.content_hashes),
            "quarantined_paths": list(self.quarantined_paths),
            "issues": [issue.to_dict() for issue in self.issues],
            "manifest_hash": self.manifest_hash,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class MasterDocsIngestionResult:
    """Canonical records and their source report before optional plane import."""

    records: tuple[dict[str, Any], ...]
    report: MasterDocsIngestionReport

    @property
    def memory_records(self) -> tuple[MemoryRecord, ...]:
        return tuple(MemoryRecord(**record) for record in self.records)

    def to_dict(self, *, include_records: bool = True) -> dict[str, Any]:
        """Return the canonical import bundle and its bounded report."""
        payload: dict[str, Any] = {"report": self.report.to_dict()}
        if include_records:
            payload["records"] = [dict(record) for record in self.records]
        return payload


__all__ = [
    "DEFAULT_CHUNK_SIZE",
    "DEFAULT_MAX_CONTENT_BYTES",
    "MASTERDOCS_ADAPTER_VERSION",
    "MASTERDOCS_SCHEMA_VERSION",
    "MasterDocsIngestionReport",
    "MasterDocsIngestionResult",
    "MasterDocsIssue",
]
