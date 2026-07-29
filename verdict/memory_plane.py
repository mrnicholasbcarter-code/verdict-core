"""Local-first, provenance-aware durable memory.

SQLite is the source of truth and FTS5 is only a rebuildable lexical index.
External memory systems are optional adapters; they are never required for
local operation and never provide Verdict policy authority.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal, cast

SCHEMA_VERSION = 2
RecordStatus = Literal["active", "superseded", "tombstone"]


@dataclass(frozen=True)
class MemoryRecord:
    """A durable memory item with explicit provenance and trust metadata."""

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
    authority: str = "unverified"
    authority_verified: bool = False
    confidence: float = 0.0
    sensitivity: str = "standard"
    provenance: dict[str, Any] | None = None
    updated_at: float = 0.0
    content_hash: str = ""
    schema_version: int = SCHEMA_VERSION
    status: RecordStatus = "active"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MemorySearchResult:
    """A deterministic retrieval result with advisory ranking metadata."""

    record: MemoryRecord
    score: float
    rank: int


class MemoryPlane:
    """SQLite/WAL memory store with append-only history and FTS5 retrieval."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(
            self.path, timeout=10, isolation_level=None, check_same_thread=False
        )
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA busy_timeout=10000")
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._initialize()

    def _initialize(self) -> None:
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS memory_meta (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
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
                updated_at REAL NOT NULL,
                expires_at REAL,
                supersedes TEXT,
                authority TEXT NOT NULL,
                authority_verified INTEGER NOT NULL,
                confidence REAL NOT NULL,
                sensitivity TEXT NOT NULL,
                provenance_json TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('active', 'superseded', 'tombstone'))
            );
            CREATE INDEX IF NOT EXISTS memories_active_key
                ON memories(namespace, scope, key, status, created_at DESC);
            CREATE INDEX IF NOT EXISTS memories_content_hash ON memories(content_hash);
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                record_id UNINDEXED, namespace, key, content, source, trust, scope,
                tokenize='unicode61'
            );
            INSERT OR IGNORE INTO memory_meta(key, value) VALUES ('schema_version', '2');
            """
        )
        version = int(
            self._db.execute("SELECT value FROM memory_meta WHERE key='schema_version'").fetchone()[
                0
            ]
        )
        if version != SCHEMA_VERSION:
            raise RuntimeError(f"unsupported memory schema version: {version}")

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> MemoryPlane:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def put(self, record: MemoryRecord) -> MemoryRecord:
        """Append a record and supersede the previous active value for its key."""
        return self._put(record, allow_authority=False)

    def put_verified(self, record: MemoryRecord) -> MemoryRecord:
        """Append a record after a caller has verified its authority boundary.

        Ordinary callers cannot self-assert ``authority_verified``.  Trusted
        adapters such as documentation preflight use this explicit method,
        which still validates the content hash and provenance before storage.
        """
        if not record.authority_verified:
            raise ValueError("verified memory records must declare authority_verified")
        if record.authority in {"", "unverified"}:
            raise ValueError("verified memory records require an authority")
        return self._put(record, allow_authority=True)

    def repair_verified(self, record: MemoryRecord) -> MemoryRecord:
        """Repair a previously persisted verified record after source revalidation.

        A normal ``put`` is intentionally idempotent by ``record_id`` and
        content hash.  That is insufficient when a privileged database writer
        has tampered with metadata, provenance, expiry, or even the stored
        content.  The documentation adapter uses this narrowly scoped method
        only after re-reading and re-verifying the authoritative source.
        """
        if not record.authority_verified:
            raise ValueError("verified memory records must declare authority_verified")
        if record.authority in {"", "unverified"}:
            raise ValueError("verified memory records require an authority")
        normalized = self._normalize(record, allow_authority=True)
        self._db.execute("BEGIN IMMEDIATE")
        try:
            existing = self._db.execute(
                "SELECT record_id FROM memories WHERE record_id=?", (normalized.record_id,)
            ).fetchone()
            if existing is None:
                self._db.execute("COMMIT")
                return self.put_verified(normalized)

            conflicting = self._db.execute(
                "SELECT record_id FROM memories WHERE namespace=? AND scope=? AND key=? "
                "AND status='active' AND record_id<>? ORDER BY created_at DESC LIMIT 1",
                (normalized.namespace, normalized.scope, normalized.key, normalized.record_id),
            ).fetchone()
            if conflicting:
                self._db.execute(
                    "UPDATE memories SET status='superseded', updated_at=? WHERE record_id=?",
                    (normalized.updated_at, conflicting["record_id"]),
                )
                self._db.execute(
                    "DELETE FROM memory_fts WHERE record_id=?", (conflicting["record_id"],)
                )
                normalized = replace(
                    normalized, supersedes=normalized.supersedes or conflicting["record_id"]
                )

            values = (
                normalized.namespace,
                normalized.key,
                normalized.content,
                normalized.source,
                normalized.trust,
                normalized.scope,
                _json(normalized.metadata),
                normalized.created_at,
                normalized.updated_at,
                normalized.expires_at,
                normalized.supersedes,
                normalized.authority,
                int(normalized.authority_verified),
                normalized.confidence,
                normalized.sensitivity,
                _json(normalized.provenance),
                normalized.content_hash,
                normalized.schema_version,
                normalized.status,
                normalized.record_id,
            )
            self._db.execute(
                "UPDATE memories SET namespace=?, key=?, content=?, source=?, trust=?, "
                "scope=?, metadata_json=?, created_at=?, updated_at=?, expires_at=?, "
                "supersedes=?, authority=?, authority_verified=?, confidence=?, "
                "sensitivity=?, provenance_json=?, content_hash=?, schema_version=?, "
                "status=? WHERE record_id=?",
                values,
            )
            self._db.execute("DELETE FROM memory_fts WHERE record_id=?", (normalized.record_id,))
            if normalized.status == "active":
                self._db.execute(
                    "INSERT INTO memory_fts(record_id, namespace, key, content, source, trust, scope) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        normalized.record_id,
                        normalized.namespace,
                        normalized.key,
                        normalized.content,
                        normalized.source,
                        normalized.trust,
                        normalized.scope,
                    ),
                )
            self._db.execute("COMMIT")
            row = self._db.execute(
                "SELECT * FROM memories WHERE record_id=?", (normalized.record_id,)
            ).fetchone()
            if row is None:
                raise RuntimeError("verified record repair did not persist")
            return self._from_row(row)
        except Exception:
            self._db.execute("ROLLBACK")
            raise

    def _put(self, record: MemoryRecord, *, allow_authority: bool) -> MemoryRecord:
        normalized = self._normalize(record, allow_authority=allow_authority)
        self._db.execute("BEGIN IMMEDIATE")
        try:
            existing_id = self._db.execute(
                "SELECT record_id FROM memories WHERE record_id=?", (normalized.record_id,)
            ).fetchone()
            if existing_id:
                existing = self._db.execute(
                    "SELECT * FROM memories WHERE record_id=?", (normalized.record_id,)
                ).fetchone()
                if existing["content_hash"] == normalized.content_hash:
                    self._db.execute("COMMIT")
                    return self._from_row(existing)
                raise ValueError("record_id already exists with different content")
            previous = self._active_row(normalized.namespace, normalized.scope, normalized.key)
            supersedes = normalized.supersedes or (previous["record_id"] if previous else None)
            if previous:
                self._db.execute(
                    "UPDATE memories SET status='superseded', updated_at=? WHERE record_id=?",
                    (normalized.updated_at, previous["record_id"]),
                )
                self._db.execute(
                    "DELETE FROM memory_fts WHERE record_id=?", (previous["record_id"],)
                )
            stored = replace(normalized, supersedes=supersedes)
            self._insert(stored)
            self._db.execute(
                "INSERT INTO memory_fts(record_id, namespace, key, content, source, trust, scope) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    stored.record_id,
                    stored.namespace,
                    stored.key,
                    stored.content,
                    stored.source,
                    stored.trust,
                    stored.scope,
                ),
            )
            self._db.execute("COMMIT")
            return stored
        except Exception:
            self._db.execute("ROLLBACK")
            raise

    def supersede(
        self, namespace: str, key: str, replacement: MemoryRecord, *, scope: str = "default"
    ) -> MemoryRecord:
        """Append a replacement linked to the currently active record."""
        if (
            replacement.namespace != namespace
            or replacement.key != key
            or replacement.scope != scope
        ):
            raise ValueError("replacement identity does not match the target")
        return self.put(replacement)

    def get(self, namespace: str, key: str, *, scope: str = "default") -> MemoryRecord | None:
        row = self._active_row(namespace, scope, key)
        if row is None or self._expired(row):
            return None
        return self._from_row(row)

    def history(self, namespace: str, key: str, *, scope: str = "default") -> list[MemoryRecord]:
        rows = self._db.execute(
            "SELECT * FROM memories WHERE namespace=? AND scope=? AND key=? "
            "ORDER BY created_at ASC, record_id ASC",
            (namespace, scope, key),
        ).fetchall()
        return [self._from_row(row) for row in rows]

    def search(
        self, query: str, *, namespace: str | None = None, scope: str = "default", limit: int = 10
    ) -> list[MemoryRecord]:
        """Return active, unexpired records in deterministic lexical order."""
        return [
            item.record
            for item in self.search_ranked(query, namespace=namespace, scope=scope, limit=limit)
        ]

    def search_ranked(
        self, query: str, *, namespace: str | None = None, scope: str = "default", limit: int = 10
    ) -> list[MemorySearchResult]:
        if not query.strip() or limit <= 0:
            return []
        terms = [term.replace('"', "") for term in query.split() if term.replace('"', "")]
        if not terms:
            return []
        match = " OR ".join(f'"{term}"' for term in terms)
        clauses = ["f.scope=?", "m.status='active'", "(m.expires_at IS NULL OR m.expires_at>?)"]
        params: list[Any] = [scope, time.time()]
        if namespace is not None:
            clauses.append("m.namespace=?")
            params.append(namespace)
        params.extend([match, min(limit, 100)])
        query = (  # nosec B608: clauses are fixed internal predicates; values are bound below.
            "SELECT m.*, bm25(memory_fts) AS rank_score FROM memory_fts f "  # nosec B608
            "JOIN memories m ON m.record_id=f.record_id WHERE "
            + " AND ".join(clauses)
            + " AND memory_fts MATCH ? ORDER BY rank_score ASC, m.created_at DESC, "
            "m.record_id ASC LIMIT ?"
        )
        rows = self._db.execute(query, params).fetchall()
        return [
            MemorySearchResult(self._from_row(row), float(row["rank_score"]), index + 1)
            for index, row in enumerate(rows)
        ]

    def list_namespaces(self, *, scope: str = "default") -> list[str]:
        rows = self._db.execute(
            "SELECT DISTINCT namespace FROM memories WHERE scope=? AND status='active' ORDER BY namespace",
            (scope,),
        ).fetchall()
        return [str(row[0]) for row in rows]

    def records(
        self,
        *,
        namespace: str | None = None,
        scope: str | None = "default",
        include_history: bool = False,
    ) -> list[MemoryRecord]:
        """Return canonical records for durable adapter/audit inspection."""
        clauses: list[str] = []
        params: list[Any] = []
        if scope is not None:
            clauses.append("scope=?")
            params.append(scope)
        if namespace is not None:
            clauses.append("namespace=?")
            params.append(namespace)
        if not include_history:
            clauses.append("status='active'")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        query = "SELECT * FROM memories" + where + " ORDER BY namespace, key, created_at, record_id"  # nosec B608
        rows = self._db.execute(query, params).fetchall()
        return [self._from_row(row) for row in rows]

    def export_records(
        self, *, scope: str = "default", include_history: bool = False
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM memories WHERE scope=?"
        if not include_history:
            query += " AND status='active'"
        query += " ORDER BY namespace, key, created_at, record_id"
        return [
            self._from_row(row).to_dict() for row in self._db.execute(query, (scope,)).fetchall()
        ]

    def import_records(self, records: Iterable[Mapping[str, Any]]) -> tuple[int, int]:
        """Import canonical records idempotently; return (written, duplicates)."""
        written = duplicates = 0
        for raw in records:
            record = MemoryRecord(**dict(raw))
            before = self._db.execute(
                "SELECT 1 FROM memories WHERE record_id=?", (record.record_id,)
            ).fetchone()
            self.put(record)
            if before:
                duplicates += 1
            else:
                written += 1
        return written, duplicates

    def status(self, *, scope: str = "default") -> dict[str, Any]:
        total = self._db.execute(
            "SELECT count(*) FROM memories WHERE scope=? AND status='active'", (scope,)
        ).fetchone()[0]
        expired = self._db.execute(
            "SELECT count(*) FROM memories WHERE scope=? AND status='active' AND expires_at IS NOT NULL AND expires_at<=?",
            (scope, time.time()),
        ).fetchone()[0]
        return {
            "state": "ready",
            "backend": "sqlite",
            "schema_version": SCHEMA_VERSION,
            "scope": scope,
            "records": total,
            "expired": expired,
            "semantic": "unavailable",
        }

    def health(self) -> dict[str, Any]:
        """Return non-sensitive local health metadata."""
        return self.status()

    def _normalize(self, record: MemoryRecord, *, allow_authority: bool = False) -> MemoryRecord:
        if (
            not record.record_id
            or not record.namespace
            or not record.key
            or not record.source
            or not record.content
        ):
            raise ValueError("record_id, namespace, key, source, and content are required")
        if not 0.0 <= record.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        now = time.time()
        created = record.created_at or now
        updated = record.updated_at or now
        provenance = dict(record.provenance or {})
        provenance.setdefault("source", record.source)
        provenance.setdefault("observed_at", created)
        provenance.setdefault("schema_version", SCHEMA_VERSION)
        digest = hashlib.sha256(record.content.encode("utf-8")).hexdigest()
        if record.content_hash and record.content_hash != digest:
            raise ValueError("content_hash does not match content")
        return replace(
            record,
            metadata=dict(record.metadata or {}),
            provenance=provenance,
            created_at=created,
            updated_at=updated,
            content_hash=digest,
            schema_version=SCHEMA_VERSION,
            authority_verified=record.authority_verified if allow_authority else False,
        )

    def _insert(self, record: MemoryRecord) -> None:
        self._db.execute(
            "INSERT INTO memories VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.record_id,
                record.namespace,
                record.key,
                record.content,
                record.source,
                record.trust,
                record.scope,
                _json(record.metadata),
                record.created_at,
                record.updated_at,
                record.expires_at,
                record.supersedes,
                record.authority,
                int(record.authority_verified),
                record.confidence,
                record.sensitivity,
                _json(record.provenance),
                record.content_hash,
                record.schema_version,
                record.status,
            ),
        )

    def _active_row(self, namespace: str, scope: str, key: str) -> sqlite3.Row | None:
        row = self._db.execute(
            "SELECT * FROM memories WHERE namespace=? AND scope=? AND key=? AND status='active' "
            "ORDER BY created_at DESC, record_id DESC LIMIT 1",
            (namespace, scope, key),
        ).fetchone()
        return cast(sqlite3.Row | None, row)

    @staticmethod
    def _expired(row: sqlite3.Row) -> bool:
        return row["expires_at"] is not None and row["expires_at"] <= time.time()

    @staticmethod
    def _from_row(row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(
            record_id=row["record_id"],
            namespace=row["namespace"],
            key=row["key"],
            content=row["content"],
            source=row["source"],
            trust=row["trust"],
            scope=row["scope"],
            metadata=json.loads(row["metadata_json"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            expires_at=row["expires_at"],
            supersedes=row["supersedes"],
            authority=row["authority"],
            authority_verified=bool(row["authority_verified"]),
            confidence=row["confidence"],
            sensitivity=row["sensitivity"],
            provenance=json.loads(row["provenance_json"]),
            content_hash=row["content_hash"],
            schema_version=row["schema_version"],
            status=row["status"],
        )


def _json(value: Mapping[str, Any] | None) -> str:
    return json.dumps(value or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


__all__ = ["SCHEMA_VERSION", "MemoryPlane", "MemoryRecord", "MemorySearchResult"]
