"""Local-first, provenance-aware memory storage.

The memory plane is deliberately boring: SQLite is the durable source of truth,
and FTS5 is only an index.  Ruflo, RuVector, OpenViking, and hosted embedding
providers may be adapters above this boundary; none is required for writes or
recall.  Records are scoped and provenance-bearing so retrieval cannot silently
be promoted to routing authority.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class MemoryRecord:
    """A durable memory item with explicit trust and retention metadata."""

    record_id: str
    namespace: str
    key: str
    content: str
    source: str
    trust: str = "local-observation"
    scope: str = "default"
    metadata: dict[str, Any] | None = None
    created_at: float = 0.0
    expires_at: float | None = None
    supersedes: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MemoryPlane:
    """SQLite-backed memory store with deterministic lexical retrieval."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._initialize()

    def _initialize(self) -> None:
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS memory_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS memories (
                record_id TEXT PRIMARY KEY,
                namespace TEXT NOT NULL,
                key TEXT NOT NULL,
                content TEXT NOT NULL,
                source TEXT NOT NULL,
                trust TEXT NOT NULL,
                scope TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL,
                supersedes TEXT,
                UNIQUE(namespace, scope, key)
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                record_id UNINDEXED, namespace, key, content, source, trust, scope,
                tokenize='unicode61'
            );
            INSERT OR IGNORE INTO memory_meta(key, value) VALUES ('schema_version', '1');
            """
        )

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> MemoryPlane:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def put(self, record: MemoryRecord) -> MemoryRecord:
        """Insert or replace a scoped key and keep the FTS index in sync."""
        if not record.record_id or not record.namespace or not record.key:
            raise ValueError("record_id, namespace, and key are required")
        if not record.source or not record.content:
            raise ValueError("source and content are required")
        created_at = record.created_at or time.time()
        normalized = MemoryRecord(
            **{**record.to_dict(), "created_at": created_at, "metadata": record.metadata or {}}
        )
        self._db.execute("BEGIN IMMEDIATE")
        try:
            previous = self._db.execute(
                "SELECT record_id FROM memories WHERE namespace=? AND scope=? AND key=?",
                (normalized.namespace, normalized.scope, normalized.key),
            ).fetchone()
            self._db.execute(
                """INSERT INTO memories
                (record_id, namespace, key, content, source, trust, scope, metadata_json,
                 created_at, expires_at, supersedes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(namespace, scope, key) DO UPDATE SET
                  record_id=excluded.record_id, content=excluded.content, source=excluded.source,
                  trust=excluded.trust, metadata_json=excluded.metadata_json,
                  created_at=excluded.created_at, expires_at=excluded.expires_at,
                  supersedes=excluded.supersedes""",
                (
                    normalized.record_id, normalized.namespace, normalized.key,
                    normalized.content, normalized.source, normalized.trust, normalized.scope,
                    json.dumps(normalized.metadata, sort_keys=True, separators=(",", ":")),
                    normalized.created_at, normalized.expires_at, normalized.supersedes,
                ),
            )
            if previous:
                self._db.execute("DELETE FROM memory_fts WHERE record_id=?", (previous["record_id"],))
            self._db.execute(
                "INSERT INTO memory_fts(record_id, namespace, key, content, source, trust, scope) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (normalized.record_id, normalized.namespace, normalized.key, normalized.content,
                 normalized.source, normalized.trust, normalized.scope),
            )
            self._db.execute("COMMIT")
        except Exception:
            self._db.execute("ROLLBACK")
            raise
        return normalized

    def get(self, namespace: str, key: str, *, scope: str = "default") -> MemoryRecord | None:
        row = self._db.execute(
            "SELECT * FROM memories WHERE namespace=? AND scope=? AND key=?", (namespace, scope, key)
        ).fetchone()
        if row is None or (row["expires_at"] is not None and row["expires_at"] <= time.time()):
            return None
        return self._from_row(row)

    def search(self, query: str, *, namespace: str | None = None, scope: str = "default", limit: int = 10) -> list[MemoryRecord]:
        """Search lexical evidence; expired or cross-scope records never leak."""
        if not query.strip() or limit <= 0:
            return []
        match = " OR ".join(f'"{term.replace(chr(34), "")}"' for term in query.split() if term)
        clauses = ["f.scope = ?", "(m.expires_at IS NULL OR m.expires_at > ?)"]
        params: list[Any] = [scope, time.time()]
        if namespace:
            clauses.append("m.namespace = ?")
            params.append(namespace)
        params.extend([match, min(limit, 100)])
        rows = self._db.execute(
            f"""SELECT m.* FROM memory_fts f JOIN memories m ON m.record_id=f.record_id
                WHERE {' AND '.join(clauses)} AND memory_fts MATCH ?
                ORDER BY bm25(memory_fts), m.created_at DESC LIMIT ?""",
            params,
        ).fetchall()
        return [self._from_row(row) for row in rows]

    def delete(self, namespace: str, key: str, *, scope: str = "default") -> bool:
        row = self._db.execute(
            "SELECT record_id FROM memories WHERE namespace=? AND scope=? AND key=?", (namespace, scope, key)
        ).fetchone()
        if row is None:
            return False
        self._db.execute("DELETE FROM memory_fts WHERE record_id=?", (row["record_id"],))
        self._db.execute("DELETE FROM memories WHERE record_id=?", (row["record_id"],))
        return True

    def health(self) -> dict[str, Any]:
        """Return non-sensitive local health metadata."""
        count = self._db.execute("SELECT count(*) FROM memories").fetchone()[0]
        version = self._db.execute("SELECT value FROM memory_meta WHERE key='schema_version'").fetchone()[0]
        return {"backend": "sqlite", "schema_version": int(version), "records": count, "fts": True}

    @staticmethod
    def _from_row(row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(
            record_id=row["record_id"], namespace=row["namespace"], key=row["key"],
            content=row["content"], source=row["source"], trust=row["trust"], scope=row["scope"],
            metadata=json.loads(row["metadata_json"]), created_at=row["created_at"],
            expires_at=row["expires_at"], supersedes=row["supersedes"],
        )


__all__ = ["SCHEMA_VERSION", "MemoryPlane", "MemoryRecord"]
