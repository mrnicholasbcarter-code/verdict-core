from __future__ import annotations

import json
from pathlib import Path

import pytest

from verdict.memory_document_adapter import DocumentIngestionPolicy, DocumentIngestor


def ingestor(root: Path, **kwargs: object) -> DocumentIngestor:
    return DocumentIngestor(DocumentIngestionPolicy((root,), **kwargs))


def test_ingestion_is_deterministic_and_emits_memory_record_mappings(tmp_path: Path) -> None:
    document = tmp_path / "docs" / "guide.md"
    document.parent.mkdir()
    document.write_bytes(b"\xef\xbb\xbf# Title\r\n\r\nfirst paragraph  \r\n\r\nsecond paragraph")
    reader = ingestor(tmp_path, chunk_size=30)

    first = reader.ingest([document], namespace="docs", scope="repo", source="fixture")
    second = reader.ingest([document], namespace="docs", scope="repo", source="fixture")

    assert first.to_dict() == second.to_dict()
    assert first.report.to_dict() == {
        "operation": "document-ingest",
        "adapter_version": "1",
        "dry_run": False,
        "status": "ok",
        "paths_seen": 1,
        "files_seen": 1,
        "files_accepted": 1,
        "chunks_emitted": 2,
        "skipped": 0,
        "quarantined": 0,
        "rejected": 0,
        "bytes_read": len(document.read_bytes()),
        "issues": [],
    }
    assert first.records[0]["content"] == "# Title\n\nfirst paragraph"
    assert first.records[0]["content_hash"]
    assert first.records[0]["provenance"] == {
        "source": "fixture",
        "adapter": "memory-document",
        "adapter_version": "1",
        "schema_version": 2,
        "root_index": 0,
        "relative_path": "docs/guide.md",
        "document_hash": first.records[0]["metadata"]["document_hash"],
        "format": "markdown",
        "chunk_index": 0,
        "chunk_count": 2,
    }
    assert first.records[0]["authority_verified"] is False
    assert first.memory_records[0].content == first.records[0]["content"]


def test_directory_ingestion_orders_documents_and_chunks_long_lines(tmp_path: Path) -> None:
    (tmp_path / "z.txt").write_text("z", encoding="utf-8")
    (tmp_path / "a.txt").write_text("a" * 7, encoding="utf-8")
    result = ingestor(tmp_path, chunk_size=3).ingest([tmp_path])

    assert [record["key"] for record in result.records] == [
        "a.txt#chunk-0000",
        "a.txt#chunk-0001",
        "a.txt#chunk-0002",
        "z.txt#chunk-0000",
    ]
    assert result.records[0]["content"] == "aaa"
    assert result.records[2]["provenance"]["chunk_count"] == 3


def test_policy_quarantines_temp_generated_and_vendor_paths(tmp_path: Path) -> None:
    (tmp_path / "vendor").mkdir()
    (tmp_path / "generated").mkdir()
    (tmp_path / "temp").mkdir()
    (tmp_path / "vendor" / "vendor.md").write_text("vendor", encoding="utf-8")
    (tmp_path / "generated" / "api.md").write_text("generated", encoding="utf-8")
    (tmp_path / "temp" / "notes.txt").write_text("temporary", encoding="utf-8")
    (tmp_path / "keep.md").write_text("keep", encoding="utf-8")

    result = ingestor(tmp_path).ingest([tmp_path])

    assert result.report.quarantined == 3
    assert result.report.files_accepted == 1
    assert [issue.category for issue in result.report.issues] == [
        "quarantined",
        "quarantined",
        "quarantined",
    ]


def test_rejects_unsafe_paths_symlinks_oversize_and_invalid_utf8(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    link = tmp_path / "link.md"
    link.symlink_to(outside)
    oversized = tmp_path / "large.txt"
    oversized.write_text("123456", encoding="utf-8")
    invalid = tmp_path / "invalid.md"
    invalid.write_bytes(b"ok\xff")

    result = ingestor(tmp_path, max_file_bytes=5).ingest([outside, link, oversized, invalid])

    assert result.records == ()
    assert result.report.rejected == 4
    assert {issue.reason for issue in result.report.issues} == {
        "path is outside the allowlisted roots",
        "symlink paths are not allowed",
        "document exceeds max_file_bytes",
        "document is not valid UTF-8",
    }


def test_dry_run_is_machine_readable_and_does_not_write(tmp_path: Path) -> None:
    document = tmp_path / "readme.txt"
    document.write_text("offline", encoding="utf-8")

    result = ingestor(tmp_path).ingest([document], dry_run=True)
    encoded = result.report.to_json()

    assert json.loads(encoded) == result.report.to_dict()
    assert result.report.dry_run is True
    assert result.report.chunks_emitted == 1
    assert not (tmp_path / "memory.db").exists()


def test_unsupported_and_empty_documents_are_reported_without_failure(tmp_path: Path) -> None:
    (tmp_path / "image.png").write_bytes(b"not a document")
    (tmp_path / "empty.md").write_text("\n\n", encoding="utf-8")

    result = ingestor(tmp_path).ingest([tmp_path])

    assert result.records == ()
    assert result.report.skipped == 2
    assert sorted(issue.reason for issue in result.report.issues) == sorted(
        ["unsupported document format", "document is empty"]
    )


def test_policy_requires_explicit_existing_absolute_roots(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        DocumentIngestionPolicy((Path("relative"),))
    with pytest.raises(ValueError, match="directory"):
        DocumentIngestionPolicy((tmp_path / "missing",))
