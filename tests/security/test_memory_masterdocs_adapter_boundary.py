"""Scope boundary tests for ``verdict.memory_masterdocs_adapter`` (T020)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from tests.security.fixtures import SECRET_KEYED_VALUES
from verdict.memory_masterdocs_adapter import MasterDocsAdapter
from verdict.memory_plane import MemoryPlane


def test_masterdocs_records_do_not_cross_scope_boundary(tmp_path: Path) -> None:
    secret = SECRET_KEYED_VALUES["api_key"]
    database = tmp_path / "masterdocs.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE documents (path TEXT, content TEXT)")
        connection.execute(
            "INSERT INTO documents(path, content) VALUES (?, ?)",
            ("boundary.md", f"Synthetic boundary fixture: {secret}"),
        )
        connection.commit()

    result = MasterDocsAdapter(allowlisted_roots=(tmp_path,)).canonicalize_db_records(
        database, allow_legacy_sqlite=True
    )
    assert result.report.status == "ok"
    assert result.memory_records

    with MemoryPlane(tmp_path / "plane.db") as plane:
        MasterDocsAdapter(allowlisted_roots=(tmp_path,)).import_result(result, plane)
        record = result.memory_records[0]
        assert plane.get(record.namespace, record.key, scope="default") is not None
        assert plane.get(record.namespace, record.key, scope="unauthorized") is None
        assert plane.records(scope="unauthorized") == []
