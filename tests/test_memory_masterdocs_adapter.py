"""Tests for MasterDocsAdapter and sqlite canonicalization into MemoryPlane."""

import sqlite3
from pathlib import Path

from verdict.memory_masterdocs_adapter import MasterDocsAdapter
from verdict.memory_plane import MemoryPlane


def test_masterdocs_adapter_ingestion(tmp_path: Path) -> None:
    db_file = tmp_path / "test_masterdocs.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute("CREATE TABLE documents (id TEXT, path TEXT, content TEXT);")
    conn.execute("INSERT INTO documents VALUES ('1', 'docs/readme.md', 'Header content');")
    conn.execute("INSERT INTO documents VALUES ('2', '/tmp/unsafe.md', 'Unsafe content');")
    conn.commit()
    conn.close()

    plane = MemoryPlane(":memory:")
    adapter = MasterDocsAdapter(allowlisted_roots=(tmp_path,))

    report = adapter.canonicalize_db(db_file, plane, allow_tmp=False)
    assert report.total_found == 2
    assert report.ingested == 1
    assert report.quarantined == 1
    assert len(report.content_hashes) == 1

    records = plane.search("Header")
    assert len(records) == 1
    assert records[0].key == "doc:1"
    assert records[0].namespace == "masterdocs"
