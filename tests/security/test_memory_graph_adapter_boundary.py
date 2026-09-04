"""Scope boundary tests for ``verdict.memory_graph_adapter`` (T020)."""

from __future__ import annotations

import tempfile
from pathlib import Path

from tests.security.fixtures import SECRET_KEYED_VALUES
from verdict.memory_graph_adapter import CodeGraphAdapter
from verdict.memory_plane import MemoryPlane


def test_graph_records_do_not_cross_scope_boundary() -> None:
    secret = SECRET_KEYED_VALUES["api_key"]
    with tempfile.TemporaryDirectory() as tmp, MemoryPlane(Path(tmp) / "plane.db") as plane:
        report = CodeGraphAdapter().ingest_dict(
            {
                "nodes": [
                    {
                        "id": "boundary-node",
                        "name": "boundary_node",
                        "kind": "function",
                        "file": "src/boundary.py",
                        "details": {"note": secret},
                    }
                ],
                "edges": [],
            },
            plane,
        )

        assert report.records_created == 1
        record = plane.get("code_graph", "function:boundary_node", scope="default")
        assert record is not None
        assert secret in record.content
        assert plane.get("code_graph", record.key, scope="unauthorized") is None
        assert plane.records(namespace="code_graph", scope="unauthorized") == []
