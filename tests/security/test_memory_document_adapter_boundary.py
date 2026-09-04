"""Scope boundary tests for ``verdict.memory_document_adapter`` (T020)."""

from __future__ import annotations

from pathlib import Path

from tests.security.fixtures import SECRET_KEYED_VALUES
from verdict.memory_document_adapter import DocumentIngestionPolicy, DocumentIngestor
from verdict.memory_plane import MemoryPlane


def test_document_records_do_not_cross_scope_boundary(tmp_path: Path) -> None:
    secret = SECRET_KEYED_VALUES["api_key"]
    document = tmp_path / "boundary.md"
    document.write_text(f"Synthetic boundary fixture: {secret}", encoding="utf-8")
    result = DocumentIngestor(
        DocumentIngestionPolicy(allowed_roots=(tmp_path,), chunk_size=4096)
    ).ingest((document,), scope="tenant-a")

    assert result.report.status == "ok"
    assert result.memory_records
    with MemoryPlane(tmp_path / "plane.db") as plane:
        for record in result.memory_records:
            plane.put(record)
        record = result.memory_records[0]
        assert plane.get(record.namespace, record.key, scope="tenant-a") is not None
        assert plane.get(record.namespace, record.key, scope="tenant-b") is None
        assert plane.records(scope="tenant-b") == []
