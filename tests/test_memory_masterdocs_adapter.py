"""Safety and determinism tests for the legacy MasterDocs migration boundary."""

import hashlib
import json
import sqlite3
from pathlib import Path

from verdict.memory_masterdocs_adapter import MasterDocsAdapter
from verdict.memory_plane import MemoryPlane


def make_db(path: Path, rows: list[tuple[str, str]], *, table: str = "documents") -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(f"CREATE TABLE {table} (id TEXT, path TEXT, content TEXT);")
        connection.executemany(
            f"INSERT INTO {table} VALUES (?, ?, ?)",
            [
                (str(index), source_path, content)
                for index, (source_path, content) in enumerate(rows)
            ],
        )


def test_masterdocs_ingestion_is_canonical_provenance_safe_and_deduplicated(tmp_path: Path) -> None:
    db_file = tmp_path / "test_masterdocs.db"
    make_db(
        db_file,
        [
            ("docs/readme.md", "# Header\n\ncontent"),
            ("docs/copy.md", "# Header\n\ncontent"),
            ("/tmp/unsafe.md", "Unsafe content"),
        ],
    )
    adapter = MasterDocsAdapter(allowlisted_roots=(tmp_path,), chunk_size=200)

    result = adapter.canonicalize_db_records(
        db_file, allow_legacy_sqlite=True, ingest_timestamp=123.0
    )

    assert result.report.status == "partial"
    assert result.report.total_found == 3
    assert result.report.documents_accepted == 1
    assert result.report.chunks_emitted == 1
    assert result.report.duplicates == 1
    assert result.report.quarantined == 1
    assert result.report.quarantined_paths == ("/tmp/unsafe.md",)
    record = result.records[0]
    assert record["key"] == "docs/readme.md#chunk-0000"
    assert record["trust"] == "imported-unverified"
    assert record["authority_verified"] is False
    assert record["provenance"] == {
        "source": "masterdocs-sqlite-legacy",
        "adapter": "masterdocs-sqlite",
        "adapter_version": "2",
        "schema_version": "1",
        "source_root_id": "root-0",
        "relative_path": "docs/readme.md",
        "source_paths": ["docs/copy.md", "docs/readme.md"],
        "language": "markdown",
        "extractor_version": "2",
        "chunker_version": "1",
        "document_hash": hashlib.sha256(b"# Header\n\ncontent").hexdigest(),
        "chunk_hash": record["content_hash"],
        "byte_start": 0,
        "byte_end": len(b"# Header\n\ncontent"),
        "ingest_timestamp": 123.0,
    }

    with MemoryPlane(":memory:") as plane:
        report = adapter.canonicalize_db(
            db_file, plane, allow_legacy_sqlite=True, ingest_timestamp=123.0
        )
        assert report.ingested == 1
        assert plane.search("Header")[0].key == record["key"]


def test_same_source_and_timestamp_produce_identical_records_and_report(tmp_path: Path) -> None:
    db_file = tmp_path / "masterdocs.db"
    make_db(db_file, [("guide.md", "one\n\ntwo")])
    adapter = MasterDocsAdapter(allowlisted_roots=(tmp_path,), chunk_size=3)

    first = adapter.canonicalize_db_records(
        db_file, allow_legacy_sqlite=True, ingest_timestamp=42.0
    )
    second = adapter.canonicalize_db_records(
        db_file, allow_legacy_sqlite=True, ingest_timestamp=42.0
    )

    assert first.records == second.records
    assert first.report.to_dict() == second.report.to_dict()
    assert first.report.manifest_hash
    assert json.loads(first.report.to_json()) == first.report.to_dict()


def test_default_private_sqlite_boundary_is_explicitly_unavailable(tmp_path: Path) -> None:
    db_file = tmp_path / "masterdocs.db"
    db_file.touch()

    result = MasterDocsAdapter().canonicalize_db_records(db_file)

    assert result.records == ()
    assert result.report.status == "unavailable"
    assert "validated manifest" in result.report.issues[0].reason


def test_corpus_fts_is_not_treated_as_canonical_chunks(tmp_path: Path) -> None:
    db_file = tmp_path / "masterdocs.db"
    make_db(db_file, [("docs.md", "unsafe legacy shape")], table="corpus_fts")

    result = MasterDocsAdapter(allowlisted_roots=(tmp_path,)).canonicalize_db_records(
        db_file, allow_legacy_sqlite=True
    )

    assert result.records == ()
    assert result.report.status == "rejected"
    assert "explicit versioned migration" in result.report.issues[0].reason


def test_invalid_schema_and_corrupt_database_are_rejected_without_writes(tmp_path: Path) -> None:
    invalid_schema = tmp_path / "invalid.db"
    with sqlite3.connect(invalid_schema) as connection:
        connection.execute("CREATE TABLE documents (id TEXT, body TEXT)")
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"not sqlite")
    adapter = MasterDocsAdapter(allowlisted_roots=(tmp_path,))

    schema_result = adapter.canonicalize_db_records(invalid_schema, allow_legacy_sqlite=True)
    corrupt_result = adapter.canonicalize_db_records(corrupt, allow_legacy_sqlite=True)

    assert schema_result.report.status == "rejected"
    assert corrupt_result.report.status == "rejected"
    assert schema_result.records == corrupt_result.records == ()


def test_empty_source_is_explicitly_not_healthy(tmp_path: Path) -> None:
    db_file = tmp_path / "empty.db"
    make_db(db_file, [])

    result = MasterDocsAdapter(allowlisted_roots=(tmp_path,)).canonicalize_db_records(
        db_file, allow_legacy_sqlite=True
    )

    assert result.records == ()
    assert result.report.status == "empty"


def test_symlink_source_path_is_quarantined(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    link = tmp_path / "link.md"
    link.symlink_to(outside)
    db_file = tmp_path / "masterdocs.db"
    make_db(db_file, [(str(link), "link content")])

    result = MasterDocsAdapter(allowlisted_roots=(tmp_path,)).canonicalize_db_records(
        db_file, allow_legacy_sqlite=True
    )

    assert result.records == ()
    assert result.report.quarantined == 1
