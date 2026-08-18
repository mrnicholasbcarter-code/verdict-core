"""Code Review Graph adapter for MemoryPlane.

Converts code entities, call chains, architectural bridges, and hotspots
from Code Review Graph outputs or database into canonical MemoryRecord shapes
to enable cross-tool and cross-session code graph recall.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from verdict.memory_plane import MemoryPlane, MemoryRecord

CODE_GRAPH_ADAPTER_VERSION = "1"


@dataclass(frozen=True)
class CodeGraphIngestionReport:
    """Report generated during Code Review Graph ingestion."""

    nodes_processed: int
    edges_processed: int
    records_created: int
    content_hashes: tuple[str, ...]


class CodeGraphAdapter:
    """Adapter for graph exports, with explicit legacy SQLite compatibility."""

    def ingest_dict(
        self, graph_data: dict[str, Any], plane: MemoryPlane
    ) -> CodeGraphIngestionReport:
        """Ingest in-memory dictionary of nodes and edges into MemoryPlane."""
        nodes = graph_data.get("nodes", [])
        edges = graph_data.get("edges", [])

        records_created = 0
        hashes: list[str] = []

        for node in nodes:
            name = node.get("name") or node.get("id")
            if not name:
                continue
            kind = str(node.get("kind", "entity"))
            file_path = str(node.get("file", "unknown"))

            payload = {
                "name": name,
                "kind": kind,
                "file": file_path,
                "line": node.get("line"),
                "details": node.get("details", {}),
            }
            content_str = json.dumps(payload, sort_keys=True)
            content_hash = hashlib.sha256(content_str.encode("utf-8")).hexdigest()

            record = MemoryRecord(
                record_id=f"rec_cg_{kind}_{name}",
                namespace="code_graph",
                key=f"{kind.lower()}:{name}",
                content=content_str,
                source=f"code_review_graph:{file_path}",
                content_hash=content_hash,
                authority="code_review_graph",
                confidence=1.0,
                sensitivity="public",
                provenance={
                    "source": f"code_review_graph:{file_path}",
                    "adapter": "code-graph-manifest",
                    "adapter_version": CODE_GRAPH_ADAPTER_VERSION,
                    "schema_version": 2,
                    "file": file_path,
                    "kind": kind,
                    "content_hash": content_hash,
                },
            )
            plane.put(record)
            records_created += 1
            hashes.append(content_hash)

        return CodeGraphIngestionReport(
            nodes_processed=len(nodes),
            edges_processed=len(edges),
            records_created=records_created,
            content_hashes=tuple(hashes),
        )

    def ingest_sqlite(
        self,
        db_path: str | Path,
        plane: MemoryPlane,
        limit: int = 1000,
        allow_legacy_sqlite: bool = False,
    ) -> CodeGraphIngestionReport:
        """Ingest an explicitly approved local/exported SQLite artifact."""
        if not allow_legacy_sqlite:
            raise ValueError(
                "private Code Review Graph SQLite input is disabled; use a validated "
                "manifest or set allow_legacy_sqlite=True for an exported local artifact"
            )
        path = Path(db_path).resolve()
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"code_graph_db_not_found:{path}")

        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row

        nodes_processed = 0
        records_created = 0
        hashes: list[str] = []

        try:
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table';")
            tables = {row["name"] for row in cursor.fetchall()}

            if "nodes" in tables:
                c = conn.execute(
                    "SELECT id, name, kind, file_path, line FROM nodes LIMIT ?", (limit,)
                )
                for row_raw in c.fetchall():
                    row = dict(row_raw)
                    nodes_processed += 1
                    node_id = str(row.get("id") or row.get("name"))
                    kind = str(row.get("kind") or "symbol")
                    name = str(row.get("name") or node_id)
                    file_p = str(row.get("file_path") or "unknown")

                    payload = {"id": node_id, "name": name, "kind": kind, "file": file_p}
                    content_str = json.dumps(payload, sort_keys=True)
                    content_hash = hashlib.sha256(content_str.encode("utf-8")).hexdigest()

                    record = MemoryRecord(
                        record_id=f"rec_cg_{kind}_{name}",
                        namespace="code_graph",
                        key=f"{kind.lower()}:{name}",
                        content=content_str,
                        source=f"code_graph_db:{path.name}:{file_p}",
                        content_hash=content_hash,
                        authority="code_review_graph",
                        confidence=1.0,
                        sensitivity="public",
                        provenance={
                            "source": f"code_graph_db:{path.name}:{file_p}",
                            "adapter": "code-graph-sqlite-legacy",
                            "adapter_version": CODE_GRAPH_ADAPTER_VERSION,
                            "schema_version": 2,
                            "file": file_p,
                            "db_name": path.name,
                            "content_hash": content_hash,
                        },
                    )
                    plane.put(record)
                    records_created += 1
                    hashes.append(content_hash)
        finally:
            conn.close()

        return CodeGraphIngestionReport(
            nodes_processed=nodes_processed,
            edges_processed=0,
            records_created=records_created,
            content_hashes=tuple(hashes),
        )


__all__ = ["CODE_GRAPH_ADAPTER_VERSION", "CodeGraphAdapter", "CodeGraphIngestionReport"]
