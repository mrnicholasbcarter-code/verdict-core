"""Scope boundary tests for ``verdict.memory_session_adapter`` (T020)."""

from __future__ import annotations

import json
from pathlib import Path

from tests.security.fixtures import SECRET_KEYED_VALUES
from verdict.memory_plane import MemoryPlane
from verdict.memory_session_adapter import SessionAdapter


def test_session_records_do_not_cross_scope_boundary(tmp_path: Path) -> None:
    secret = SECRET_KEYED_VALUES["api_key"]
    source = tmp_path / "session.jsonl"
    source.write_text(
        json.dumps(
            {"role": "assistant", "content": {"note": f"Synthetic boundary fixture: {secret}"}}
        )
        + "\n",
        encoding="utf-8",
    )

    result = SessionAdapter().import_file(
        source, project="boundary-project", session_id="session-a", format="jsonl"
    )
    assert result.report.status == "ok"
    assert result.memory_records
    assert secret not in result.memory_records[0].content

    with MemoryPlane(tmp_path / "plane.db") as plane:
        for record in result.memory_records:
            plane.put(record)
        record = result.memory_records[0]
        assert plane.get(record.namespace, record.key, scope=record.scope) is not None
        assert plane.get(record.namespace, record.key, scope="unauthorized-session") is None
        assert plane.search("Synthetic", scope="unauthorized-session") == []
        assert plane.records(scope="unauthorized-session") == []
