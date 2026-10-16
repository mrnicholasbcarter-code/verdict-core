"""Bounded durable SQLite outbox for capture/replay (operational, not retrieval)."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from verdict_memory.contracts import (
    IdempotencyConflictError,
    LifecycleEvent,
    ProviderErrorCode,
    redact_mapping,
    redact_secrets,
)
from verdict_memory.policy import MemoryCapturePolicy

OutboxState = Literal["pending", "acked", "dead_letter"]

# Bound chosen to match existing Verdict outbox due-query limit semantics (20).
DEFAULT_DUE_LIMIT = 20
# Soft queue bound: refuse new enqueue when pending count exceeds this.
# Rationale: reuse existing policy "bounded queue" without inventing SLA;
# 10_000 pending is far above normal harness churn and protects disk.
DEFAULT_MAX_PENDING = 10_000


@dataclass(frozen=True)
class OutboxEvent:
    idempotency_key: str
    event: LifecycleEvent
    state: OutboxState
    attempts: int
    next_attempt_at: float
    created_at: float
    updated_at: float
    error_code: str | None = None
    safe_error: str | None = None
    external_id: str | None = None


class MemoryOutbox:
    """Durable queue. Same logical source event enqueues once (INSERT OR IGNORE)."""

    def __init__(
        self,
        path: str | Path,
        *,
        policy: MemoryCapturePolicy | None = None,
        clock: Any = time.time,
        max_pending: int = DEFAULT_MAX_PENDING,
    ) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.policy = policy or MemoryCapturePolicy()
        self._clock = clock
        self.max_pending = max_pending
        with sqlite3.connect(self.path, timeout=10) as db:
            self.ensure_schema(db)

    @staticmethod
    def ensure_schema(db: sqlite3.Connection) -> None:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS memory_lifecycle_outbox (
                idempotency_key TEXT PRIMARY KEY,
                event_json TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('pending', 'acked', 'dead_letter')),
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                error_code TEXT,
                safe_error TEXT,
                external_id TEXT
            );
            CREATE INDEX IF NOT EXISTS memory_lifecycle_due
                ON memory_lifecycle_outbox(state, next_attempt_at, created_at);
            """
        )

    def enqueue(self, event: LifecycleEvent) -> OutboxEvent | None:
        """Enqueue once per idempotency_key.

        When ``identity.source_event_id`` is set, the key is stable across
        created_at/observed_at rewrites. Same key + same content digest returns
        the existing row (original source timestamp preserved). Same key +
        different content digest raises ``IdempotencyConflictError`` — never a
        silent second memory or silent payload replace.
        """
        decision = self.policy.evaluate(event)
        if not decision.eligible:
            return None
        pending = self.count("pending")
        if pending >= self.max_pending:
            raise RuntimeError(f"{ProviderErrorCode.QUEUE_FULL.value}: pending queue full")
        now = float(self._clock())
        payload_obj = event.to_dict()
        # Redact content + nested metadata/payload/provenance BEFORE durable bytes.
        if isinstance(payload_obj.get("content"), str):
            payload_obj["content"] = redact_secrets(payload_obj["content"]) or ""
        for nested_key in ("metadata", "payload", "provenance"):
            if nested_key in payload_obj:
                payload_obj[nested_key] = redact_mapping(payload_obj.get(nested_key) or {})
        # content fingerprint for conflict detection (excludes observed_at)
        content_fp = event.content_fingerprint()
        payload_obj.setdefault("metadata", {})
        if isinstance(payload_obj["metadata"], dict):
            payload_obj["metadata"] = {
                **payload_obj["metadata"],
                "content_fingerprint": content_fp,
            }
        payload = json.dumps(payload_obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        with sqlite3.connect(self.path, timeout=10) as db:
            db.row_factory = sqlite3.Row
            existing = db.execute(
                "SELECT * FROM memory_lifecycle_outbox WHERE idempotency_key=?",
                (event.idempotency_key,),
            ).fetchone()
            if existing is not None:
                existing_event = LifecycleEvent.from_dict(json.loads(existing["event_json"]))
                existing_fp = (existing_event.metadata or {}).get("content_fingerprint") or existing_event.digest()
                if existing_fp != content_fp:
                    raise IdempotencyConflictError(
                        str(event.identity.source_event_id or event.idempotency_key),
                        str(existing_fp),
                        content_fp,
                    )
                # Same logical content: return existing row unchanged (preserve created_at).
                return self._from_row(existing)
            db.execute(
                "INSERT INTO memory_lifecycle_outbox "
                "(idempotency_key,event_json,state,attempts,next_attempt_at,created_at,updated_at) "
                "VALUES (?,?,'pending',0,?,?,?)",
                (event.idempotency_key, payload, now, now, now),
            )
            row = db.execute(
                "SELECT * FROM memory_lifecycle_outbox WHERE idempotency_key=?",
                (event.idempotency_key,),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    
    def get(self, idempotency_key: str) -> OutboxEvent | None:
        with sqlite3.connect(self.path, timeout=10) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT * FROM memory_lifecycle_outbox WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def due(self, *, limit: int = DEFAULT_DUE_LIMIT, now: float | None = None) -> tuple[OutboxEvent, ...]:
        if limit <= 0:
            return ()
        cutoff = float(self._clock() if now is None else now)
        with sqlite3.connect(self.path, timeout=10) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT * FROM memory_lifecycle_outbox WHERE state='pending' "
                "AND next_attempt_at<=? ORDER BY created_at,idempotency_key LIMIT ?",
                (cutoff, limit),
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def mark_acked(self, event: OutboxEvent, external_id: str) -> None:
        now = float(self._clock())
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute(
                "UPDATE memory_lifecycle_outbox SET state='acked',attempts=attempts+1,"
                "updated_at=?,error_code=NULL,safe_error=NULL,external_id=? "
                "WHERE idempotency_key=? AND state='pending'",
                (now, external_id, event.idempotency_key),
            )

    def mark_retry(
        self, event: OutboxEvent, *, error_code: str, safe_error: str, delay_seconds: float
    ) -> None:
        now = float(self._clock())
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute(
                "UPDATE memory_lifecycle_outbox SET attempts=attempts+1,next_attempt_at=?,"
                "updated_at=?,error_code=?,safe_error=? WHERE idempotency_key=? AND state='pending'",
                (
                    now + max(0.0, delay_seconds),
                    now,
                    error_code,
                    redact_secrets(safe_error),
                    event.idempotency_key,
                ),
            )

    def mark_dead_letter(self, event: OutboxEvent, *, error_code: str, safe_error: str) -> None:
        now = float(self._clock())
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute(
                "UPDATE memory_lifecycle_outbox SET state='dead_letter',attempts=attempts+1,"
                "updated_at=?,error_code=?,safe_error=? "
                "WHERE idempotency_key=? AND state='pending'",
                (now, error_code, redact_secrets(safe_error), event.idempotency_key),
            )

    def count(self, state: OutboxState | None = None) -> int:
        with sqlite3.connect(self.path, timeout=10) as db:
            if state is None:
                row = db.execute("SELECT count(*) FROM memory_lifecycle_outbox").fetchone()
            else:
                row = db.execute(
                    "SELECT count(*) FROM memory_lifecycle_outbox WHERE state=?", (state,)
                ).fetchone()
        return int(row[0]) if row is not None else 0

    @staticmethod
    def _from_row(row: sqlite3.Row) -> OutboxEvent:
        return OutboxEvent(
            idempotency_key=str(row["idempotency_key"]),
            event=LifecycleEvent.from_dict(json.loads(row["event_json"])),
            state=row["state"],
            attempts=int(row["attempts"]),
            next_attempt_at=float(row["next_attempt_at"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            error_code=row["error_code"],
            safe_error=row["safe_error"],
            external_id=row["external_id"],
        )


__all__ = ["DEFAULT_DUE_LIMIT", "DEFAULT_MAX_PENDING", "MemoryOutbox", "OutboxEvent", "OutboxState"]
