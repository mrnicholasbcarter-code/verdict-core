"""SQLite durable outbox for asynchronous shared-memory mirroring."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from verdict.memory_capture_policy import CaptureDecision, MemoryCapturePolicy
from verdict.shared_memory import SharedMemoryEnvelope, redact_secrets

if TYPE_CHECKING:
    from verdict.memory_plane import MemoryRecord

OutboxState = Literal["pending", "acked", "dead_letter"]


@dataclass(frozen=True)
class OutboxEvent:
    idempotency_key: str
    envelope: SharedMemoryEnvelope
    state: OutboxState
    attempts: int
    next_attempt_at: float
    created_at: float
    updated_at: float
    error_code: str | None = None
    safe_error: str | None = None
    external_id: str | None = None


class MemoryOutbox:
    """Durable queue co-located with a MemoryPlane SQLite database."""

    def __init__(
        self,
        path: str | Path,
        *,
        project: str,
        tenant: str = "default",
        source_harness: str = "verdict",
        source_agent: str = "unknown",
        policy: MemoryCapturePolicy | None = None,
        clock: Any = time.time,
    ) -> None:
        if not project.strip() or not tenant.strip():
            raise ValueError("project and tenant are required")
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.project = project
        self.tenant = tenant
        self.source_harness = source_harness
        self.source_agent = source_agent
        self.policy = policy or MemoryCapturePolicy()
        self._clock = clock
        with sqlite3.connect(self.path, timeout=10) as db:
            self.ensure_schema(db)

    @staticmethod
    def ensure_schema(db: sqlite3.Connection) -> None:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS memory_mirror_outbox (
                idempotency_key TEXT PRIMARY KEY,
                envelope_json TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('pending', 'acked', 'dead_letter')),
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                error_code TEXT,
                safe_error TEXT,
                external_id TEXT
            );
            CREATE INDEX IF NOT EXISTS memory_mirror_due
                ON memory_mirror_outbox(state, next_attempt_at, created_at);
            """
        )

    def capture_decision(self, record: MemoryRecord) -> CaptureDecision:
        return self.policy.evaluate(record)

    def envelope_for(self, record: MemoryRecord) -> SharedMemoryEnvelope:
        return SharedMemoryEnvelope(
            content=record.content,
            project=self.project,
            scope=record.scope,
            tenant=self.tenant,
            memory_kind=record.namespace,
            source_harness=self.source_harness,
            source_agent=self.source_agent,
            created_at=record.created_at,
            observed_at=record.updated_at or record.created_at,
            expires_at=record.expires_at,
            sensitivity=record.sensitivity,
            trust=record.trust,
            authority="shared-memory-advisory",
            authority_verified=False,
            provenance={
                **(record.provenance or {}),
                "local_record_id": record.record_id,
                "local_source": record.source,
                "local_content_hash": record.content_hash,
            },
            metadata={"local_key": record.key},
            content_hash=record.content_hash,
        )

    def enqueue_in_transaction(
        self, db: sqlite3.Connection, record: MemoryRecord
    ) -> OutboxEvent | None:
        """Insert an eligible event using the caller's active SQLite transaction."""
        decision = self.capture_decision(record)
        if not decision.eligible:
            return None
        envelope = self.envelope_for(record)
        now = float(self._clock())
        payload = json.dumps(
            envelope.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        db.execute(
            "INSERT OR IGNORE INTO memory_mirror_outbox "
            "(idempotency_key,envelope_json,state,attempts,next_attempt_at,created_at,updated_at) "
            "VALUES (?,?,'pending',0,?,?,?)",
            (envelope.idempotency_key, payload, now, now, now),
        )
        row = db.execute(
            "SELECT * FROM memory_mirror_outbox WHERE idempotency_key=?",
            (envelope.idempotency_key,),
        ).fetchone()
        return self._from_row(row) if row is not None else None

    def due(self, *, limit: int = 20, now: float | None = None) -> tuple[OutboxEvent, ...]:
        if limit <= 0:
            return ()
        cutoff = float(self._clock() if now is None else now)
        with sqlite3.connect(self.path, timeout=10) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT * FROM memory_mirror_outbox WHERE state='pending' "
                "AND next_attempt_at<=? ORDER BY created_at,idempotency_key LIMIT ?",
                (cutoff, limit),
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def get(self, idempotency_key: str) -> OutboxEvent | None:
        with sqlite3.connect(self.path, timeout=10) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT * FROM memory_mirror_outbox WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def mark_acked(self, event: OutboxEvent, external_id: str) -> None:
        now = float(self._clock())
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute(
                "UPDATE memory_mirror_outbox SET state='acked',attempts=attempts+1,"
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
                "UPDATE memory_mirror_outbox SET attempts=attempts+1,next_attempt_at=?,"
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
                "UPDATE memory_mirror_outbox SET state='dead_letter',attempts=attempts+1,"
                "updated_at=?,error_code=?,safe_error=? "
                "WHERE idempotency_key=? AND state='pending'",
                (now, error_code, redact_secrets(safe_error), event.idempotency_key),
            )

    def count(self, state: OutboxState | None = None) -> int:
        with sqlite3.connect(self.path, timeout=10) as db:
            if state is None:
                row = db.execute("SELECT count(*) FROM memory_mirror_outbox").fetchone()
            else:
                row = db.execute(
                    "SELECT count(*) FROM memory_mirror_outbox WHERE state=?", (state,)
                ).fetchone()
        return int(row[0]) if row is not None else 0

    @staticmethod
    def _from_row(row: sqlite3.Row) -> OutboxEvent:
        return OutboxEvent(
            idempotency_key=str(row["idempotency_key"]),
            envelope=SharedMemoryEnvelope.from_dict(json.loads(row["envelope_json"])),
            state=row["state"],
            attempts=int(row["attempts"]),
            next_attempt_at=float(row["next_attempt_at"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            error_code=row["error_code"],
            safe_error=row["safe_error"],
            external_id=row["external_id"],
        )


__all__ = ["MemoryOutbox", "OutboxEvent", "OutboxState"]
