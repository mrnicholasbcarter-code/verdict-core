"""Tests for CodeGraphAdapter ingestion into MemoryPlane."""

import sqlite3
from pathlib import Path

from verdict.memory_graph_adapter import CodeGraphAdapter
from verdict.memory_plane import MemoryPlane


def test_code_graph_adapter_ingest_dict() -> None:
    plane = MemoryPlane(":memory:")
    adapter = CodeGraphAdapter()

    graph_data = {
        "nodes": [
            {
                "id": "fn1",
                "name": "parse_config",
                "kind": "function",
                "file": "verdict/config.py",
                "line": 42,
            },
            {
                "id": "cls1",
                "name": "MemoryPlane",
                "kind": "class",
                "file": "verdict/memory_plane.py",
                "line": 10,
            },
        ],
        "edges": [],
    }

    report = adapter.ingest_dict(graph_data, plane)
    assert report.nodes_processed == 2
    assert report.records_created == 2

    records = plane.search("parse_config")
    assert len(records) == 1
    assert records[0].namespace == "code_graph"
    assert records[0].key == "function:parse_config"


def test_code_graph_adapter_ingest_sqlite(tmp_path: Path) -> None:
    db_file = tmp_path / "code_graph.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute("CREATE TABLE nodes (id TEXT, name TEXT, kind TEXT, file_path TEXT, line INT);")
    conn.execute(
        "INSERT INTO nodes VALUES ('1', 'MemoryRecord', 'class', 'verdict/memory_plane.py', 15);"
    )
    conn.commit()
    conn.close()

    plane = MemoryPlane(":memory:")
    adapter = CodeGraphAdapter()

    report = adapter.ingest_sqlite(db_file, plane, allow_legacy_sqlite=True)
    assert report.nodes_processed == 1
    assert report.records_created == 1

    records = plane.search("MemoryRecord")
    assert len(records) == 1
    assert records[0].namespace == "code_graph"


def test_private_sqlite_requires_explicit_legacy_opt_in(tmp_path: Path) -> None:
    db_file = tmp_path / "code_graph.db"
    db_file.touch()
    try:
        CodeGraphAdapter().ingest_sqlite(db_file, MemoryPlane(":memory:"))
    except ValueError as exc:
        assert "private" in str(exc)
    else:
        raise AssertionError("private SQLite input was accepted without opt-in")
