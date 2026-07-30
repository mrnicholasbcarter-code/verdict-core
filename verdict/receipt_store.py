"""Durable, privacy-safe SQLite receipt storage.

The store is deliberately small and replaceable: SQLite is the local default,
while the record and lifecycle semantics do not depend on SQLite-specific
callers.  Existing records are never updated.  Lifecycle changes are linked
records, and deletion is represented by a tombstone record.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from time import time
from typing import Any, Literal
from uuid import uuid4

ReceiptType = Literal[
    "decision", "context", "execution", "verification", "outcome", "manifest", "tombstone"
]
_RECEIPT_TYPES = frozenset(
    {"decision", "context", "execution", "verification", "outcome", "manifest", "tombstone"}
)
_SCHEMA_VERSION = 2
_REDACTED = "[REDACTED]"
_DEFAULT_PROVENANCE = {"source": "verdict_receipt_store", "version": "2.0"}
_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "secret",
    "password",
    "passwd",
    "token",
    "credential",
    "authorization",
    "auth",
    "account_id",
    "private_key",
    "access_key",
    "client_secret",
)
_RAW_KEY_PARTS = (
    "prompt",
    "messages",
    "tool_arg",
    "arguments",
    "completion",
    "output",
    "transcript",
    "request_body",
    "response_body",
)
_SECRET_TEXT = re.compile(
    r"(?i)(bearer\s+|(?:api[_-]?key|secret|password|token)\s*[:=]\s*)[^\s,;]+"
)
_URL_WITH_CREDENTIALS = re.compile(r"(?i)(https?://)([^/@\s]+:[^/@\s]+)@")
_URL_QUERY = re.compile(r"(?i)(https?://[^\s/?#]+(?:/[^\s?#]*)?)(\?[^\s#]+)")
_INIT_LOCK = threading.Lock()


class ReceiptStoreError(RuntimeError):
    """Base class for durable receipt errors."""


class ReceiptConflictError(ReceiptStoreError):
    """Raised when an idempotency key or terminal event conflicts."""


class ReceiptIntegrityError(ReceiptStoreError):
    """Raised when a record or chain fails verification."""


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _validate_scope(scope: str) -> str:
    if not isinstance(scope, str) or not scope.strip() or len(scope) > 512:
        raise ValueError("a non-empty bounded receipt scope is required")
    if any(ord(char) < 32 for char in scope):
        raise ValueError("receipt scope contains control characters")
    return scope


def _allowed(path: str, key: str, allowlist: set[str]) -> bool:
    return path in allowlist or key in allowlist


def _redact_value(value: Any, *, path: str, allowlist: set[str]) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("receipt payload object keys must be strings")
            name = str(key)
            child_path = f"{path}.{name}" if path else name
            lower = name.lower().replace("-", "_")
            protected = any(part in lower for part in _SENSITIVE_KEY_PARTS)
            raw = any(part in lower for part in _RAW_KEY_PARTS)
            if (protected or raw) and not _allowed(child_path, name, allowlist):
                result[name] = _REDACTED
            else:
                result[name] = _redact_value(item, path=child_path, allowlist=allowlist)
        return result
    if isinstance(value, list):
        return [
            _redact_value(item, path=f"{path}[{index}]", allowlist=allowlist)
            for index, item in enumerate(value)
        ]
    if isinstance(value, tuple):
        return [
            _redact_value(item, path=f"{path}[{index}]", allowlist=allowlist)
            for index, item in enumerate(value)
        ]
    if isinstance(value, str) and not _allowed(path, path.rsplit(".", 1)[-1], allowlist):
        value = _SECRET_TEXT.sub(r"\1[REDACTED]", value)
        value = _URL_WITH_CREDENTIALS.sub(r"\1[REDACTED]@", value)
        value = _URL_QUERY.sub(r"\1?[REDACTED]", value)
    return value


def redact_sensitive_dict(data: dict[str, Any], *, allowlist: Iterable[str] = ()) -> dict[str, Any]:
    """Recursively redact credentials and raw content unless explicitly allowed.

    ``allowlist`` contains exact dotted field paths (or field names for
    compatibility).  It is intentionally a field-level mechanism; callers
    must opt in to retaining prompts, arguments, outputs, or transcripts.
    """

    if not isinstance(data, dict):
        raise TypeError("receipt payload must be a JSON object")
    result = _redact_value(data, path="", allowlist=set(allowlist))
    assert isinstance(result, dict)
    return result


@dataclass(frozen=True)
class ReceiptRecord:
    """An immutable receipt or linked lifecycle record."""

    receipt_id: str
    timestamp: float
    receipt_type: ReceiptType
    scope: str
    payload: dict[str, Any]
    content_hash: str
    sensitivity: str
    provenance: dict[str, Any]
    parent_receipt_id: str | None = None
    sequence: int = 0
    previous_hash: str | None = None
    record_hash: str = ""
    event_id: str | None = None
    event_type: str | None = None
    terminal_outcome: str | None = None
    idempotency_key: str | None = None
    tombstone_for: str | None = None
    key_ref: str | None = None

    @property
    def is_tombstone(self) -> bool:
        return self.receipt_type == "tombstone" or self.tombstone_for is not None


class ReceiptStore:
    """Thread-safe, restart-safe SQLite receipt ledger."""

    def __init__(self, db_path: str | Path = ":memory:", *, strict_scope: bool = False) -> None:
        self.db_path = str(db_path)
        self.strict_scope = strict_scope
        self._lock = threading.RLock()
        self._shared_conn: sqlite3.Connection | None = None
        if self.db_path == ":memory:":
            self._shared_conn = sqlite3.connect(":memory:", check_same_thread=False, timeout=30)
            self._shared_conn.row_factory = sqlite3.Row
            self._shared_conn.execute("PRAGMA busy_timeout = 30000")
        else:
            path = Path(self.db_path).expanduser().resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            self.db_path = str(path)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        if self._shared_conn is not None:
            return self._shared_conn
        conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def _init_db(self) -> None:
        conn = self._get_connection()
        try:
            with _INIT_LOCK, self._lock:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("PRAGMA synchronous = NORMAL")
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS receipt_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                current = conn.execute(
                    "SELECT value FROM receipt_meta WHERE key = 'schema_version'"
                ).fetchone()
                if current is None:
                    existing_table = conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'receipts'"
                    ).fetchone()
                    if existing_table is not None:
                        self._migrate(conn, 1)
                    else:
                        self._create_v2(conn)
                    conn.execute(
                        "INSERT OR IGNORE INTO receipt_meta(key, value) VALUES ('schema_version', ?)",
                        (str(_SCHEMA_VERSION),),
                    )
                elif int(current[0]) < _SCHEMA_VERSION:
                    self._migrate(conn, int(current[0]))
                    conn.execute(
                        "UPDATE receipt_meta SET value = ? WHERE key = 'schema_version'",
                        (str(_SCHEMA_VERSION),),
                    )
                conn.commit()
        finally:
            if self._shared_conn is None:
                conn.close()

    @staticmethod
    def _create_v2(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS receipts (
                receipt_id TEXT PRIMARY KEY,
                timestamp REAL NOT NULL,
                receipt_type TEXT NOT NULL,
                scope TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                sensitivity TEXT NOT NULL,
                provenance_json TEXT NOT NULL,
                parent_receipt_id TEXT,
                sequence INTEGER NOT NULL,
                previous_hash TEXT,
                record_hash TEXT NOT NULL,
                event_id TEXT,
                event_type TEXT,
                terminal_outcome TEXT,
                idempotency_key TEXT,
                tombstone_for TEXT,
                key_ref TEXT
            )
            """
        )
        for sql in (
            "CREATE INDEX IF NOT EXISTS idx_receipts_scope_sequence ON receipts(scope, sequence)",
            "CREATE INDEX IF NOT EXISTS idx_receipts_scope_type ON receipts(scope, receipt_type)",
            "CREATE INDEX IF NOT EXISTS idx_receipts_parent ON receipts(parent_receipt_id)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_receipts_scope_idempotency ON receipts(scope, idempotency_key) WHERE idempotency_key IS NOT NULL",
        ):
            conn.execute(sql)

    def _migrate(self, conn: sqlite3.Connection, version: int) -> None:
        """Migrate the original v1 table without rewriting its facts."""
        if version != 1:
            raise ReceiptStoreError(f"unsupported receipt schema version: {version}")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(receipts)").fetchall()}
        additions = {
            "parent_receipt_id": "TEXT",
            "sequence": "INTEGER NOT NULL DEFAULT 0",
            "previous_hash": "TEXT",
            "record_hash": "TEXT NOT NULL DEFAULT ''",
            "event_id": "TEXT",
            "event_type": "TEXT",
            "terminal_outcome": "TEXT",
            "idempotency_key": "TEXT",
            "tombstone_for": "TEXT",
            "key_ref": "TEXT",
        }
        for name, declaration in additions.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE receipts ADD COLUMN {name} {declaration}")
        rows = conn.execute(
            "SELECT receipt_id, timestamp, receipt_type, payload_json, scope, sensitivity, provenance_json, parent_receipt_id, event_id, event_type, terminal_outcome, idempotency_key, tombstone_for, key_ref FROM receipts ORDER BY scope, timestamp, receipt_id"
        ).fetchall()
        previous: dict[str, str] = {}
        sequence_by_scope: dict[str, int] = {}
        for row in rows:
            scope = str(row[4])
            sequence_by_scope[scope] = sequence_by_scope.get(scope, 0) + 1
            sequence = sequence_by_scope[scope]
            payload = json.loads(row[3])
            content_hash = hashlib.sha256(_canonical(payload).encode()).hexdigest()
            record_hash = self._record_hash(
                row[0],
                row[1],
                row[2],
                content_hash,
                scope,
                sequence,
                previous.get(scope),
                sensitivity=row[5],
                provenance=json.loads(row[6]),
                parent_receipt_id=row[7],
                event_id=row[8],
                event_type=row[9],
                terminal_outcome=row[10],
                idempotency_key=row[11],
                tombstone_for=row[12],
                key_ref=row[13],
            )
            conn.execute(
                "UPDATE receipts SET content_hash = ?, sequence = ?, previous_hash = ?, record_hash = ? WHERE receipt_id = ?",
                (content_hash, sequence, previous.get(scope), record_hash, row[0]),
            )
            previous[scope] = record_hash
        self._create_v2(conn)

    @staticmethod
    def _record_hash(
        receipt_id: str,
        timestamp: float,
        receipt_type: str,
        content_hash: str,
        scope: str,
        sequence: int,
        previous_hash: str | None,
        *,
        sensitivity: str = "",
        provenance: dict[str, Any] | None = None,
        parent_receipt_id: str | None = None,
        event_id: str | None = None,
        event_type: str | None = None,
        terminal_outcome: str | None = None,
        tombstone_for: str | None = None,
        key_ref: str | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        return hashlib.sha256(
            _canonical(
                [
                    receipt_id,
                    timestamp,
                    receipt_type,
                    content_hash,
                    scope,
                    sequence,
                    previous_hash,
                    sensitivity,
                    provenance or {},
                    parent_receipt_id,
                    event_id,
                    event_type,
                    terminal_outcome,
                    tombstone_for,
                    key_ref,
                    idempotency_key,
                ]
            ).encode()
        ).hexdigest()

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> ReceiptRecord:
        return ReceiptRecord(
            receipt_id=row["receipt_id"],
            timestamp=row["timestamp"],
            receipt_type=row["receipt_type"],
            scope=row["scope"],
            payload=json.loads(row["payload_json"]),
            content_hash=row["content_hash"],
            sensitivity=row["sensitivity"],
            provenance=json.loads(row["provenance_json"]),
            parent_receipt_id=row["parent_receipt_id"],
            sequence=row["sequence"],
            previous_hash=row["previous_hash"],
            record_hash=row["record_hash"],
            event_id=row["event_id"],
            event_type=row["event_type"],
            terminal_outcome=row["terminal_outcome"],
            idempotency_key=row["idempotency_key"],
            tombstone_for=row["tombstone_for"],
            key_ref=row["key_ref"],
        )

    def _assert_integrity(self, *, scope: str | None) -> None:
        result = self.verify_integrity(scope=scope)
        if not result["valid"]:
            raise ReceiptIntegrityError("receipt integrity verification failed")

    def _scope_clause(self, scope: str | None, params: list[Any]) -> str:
        if scope is None:
            if self.strict_scope:
                raise ValueError("receipt scope is required")
            return ""
        params.append(_validate_scope(scope))
        return " AND scope = ?"

    def _next_sequence(self, conn: sqlite3.Connection, scope: str) -> tuple[int, str | None]:
        row = conn.execute(
            "SELECT sequence, record_hash FROM receipts WHERE scope = ? ORDER BY sequence DESC LIMIT 1",
            (scope,),
        ).fetchone()
        return (int(row[0]) + 1, row[1]) if row else (1, None)

    def _insert(
        self,
        conn: sqlite3.Connection,
        *,
        receipt_type: ReceiptType,
        scope: str,
        payload: dict[str, Any],
        sensitivity: str,
        provenance: dict[str, Any],
        receipt_id: str,
        timestamp: float,
        parent_receipt_id: str | None = None,
        event_id: str | None = None,
        event_type: str | None = None,
        terminal_outcome: str | None = None,
        idempotency_key: str | None = None,
        tombstone_for: str | None = None,
        key_ref: str | None = None,
    ) -> ReceiptRecord:
        if receipt_type not in _RECEIPT_TYPES:
            raise ValueError(f"unsupported receipt type: {receipt_type}")
        content_hash = hashlib.sha256(_canonical(payload).encode()).hexdigest()
        sequence, previous_hash = self._next_sequence(conn, scope)
        record_hash = self._record_hash(
            receipt_id,
            timestamp,
            receipt_type,
            content_hash,
            scope,
            sequence,
            previous_hash,
            sensitivity=sensitivity,
            provenance=provenance,
            parent_receipt_id=parent_receipt_id,
            event_id=event_id,
            event_type=event_type,
            terminal_outcome=terminal_outcome,
            tombstone_for=tombstone_for,
            key_ref=key_ref,
            idempotency_key=idempotency_key,
        )
        conn.execute(
            """
            INSERT INTO receipts (
                receipt_id, timestamp, receipt_type, scope, payload_json, content_hash,
                sensitivity, provenance_json, parent_receipt_id, sequence, previous_hash,
                record_hash, event_id, event_type, terminal_outcome, idempotency_key,
                tombstone_for, key_ref
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                receipt_id,
                timestamp,
                receipt_type,
                scope,
                _canonical(payload),
                content_hash,
                sensitivity,
                _canonical(provenance),
                parent_receipt_id,
                sequence,
                previous_hash,
                record_hash,
                event_id,
                event_type,
                terminal_outcome,
                idempotency_key,
                tombstone_for,
                key_ref,
            ),
        )
        return ReceiptRecord(
            receipt_id,
            timestamp,
            receipt_type,
            scope,
            payload,
            content_hash,
            sensitivity,
            provenance,
            parent_receipt_id,
            sequence,
            previous_hash,
            record_hash,
            event_id,
            event_type,
            terminal_outcome,
            idempotency_key,
            tombstone_for,
            key_ref,
        )

    def put_receipt(
        self,
        receipt_type: ReceiptType,
        scope: str,
        payload: dict[str, Any],
        sensitivity: str = "internal",
        provenance: dict[str, Any] | None = None,
        receipt_id: str | None = None,
        *,
        parent_receipt_id: str | None = None,
        event_id: str | None = None,
        event_type: str | None = None,
        terminal_outcome: str | None = None,
        idempotency_key: str | None = None,
        allowlist: Iterable[str] = (),
        key_ref: str | None = None,
    ) -> ReceiptRecord:
        """Append a redacted fact, atomically and safely across restarts."""
        scope = _validate_scope(scope)
        clean_payload = redact_sensitive_dict(payload, allowlist=allowlist)
        prov = redact_sensitive_dict(dict(provenance or _DEFAULT_PROVENANCE))
        if not isinstance(sensitivity, str) or not sensitivity or len(sensitivity) > 64:
            raise ValueError("receipt sensitivity must be a bounded non-empty string")
        r_id = receipt_id or f"rcpt-{uuid4().hex}"
        conn = self._get_connection()
        try:
            with self._lock:
                conn.execute("BEGIN IMMEDIATE")
                if idempotency_key is not None:
                    existing = conn.execute(
                        "SELECT * FROM receipts WHERE scope = ? AND idempotency_key = ?",
                        (scope, idempotency_key),
                    ).fetchone()
                    if existing is not None:
                        record = self._row_to_record(existing)
                        if record.payload == clean_payload and record.receipt_type == receipt_type:
                            conn.commit()
                            return record
                        raise ReceiptConflictError(
                            "idempotency key conflicts with an existing receipt"
                        )
                try:
                    record = self._insert(
                        conn,
                        receipt_type=receipt_type,
                        scope=scope,
                        payload=clean_payload,
                        sensitivity=sensitivity,
                        provenance=prov,
                        receipt_id=r_id,
                        timestamp=time(),
                        parent_receipt_id=parent_receipt_id,
                        event_id=event_id,
                        event_type=event_type,
                        terminal_outcome=terminal_outcome,
                        idempotency_key=idempotency_key,
                        key_ref=key_ref,
                    )
                except sqlite3.IntegrityError as exc:
                    conn.rollback()
                    raise ReceiptConflictError("receipt id already exists") from exc
                conn.commit()
                return record
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            if self._shared_conn is None:
                conn.close()

    def append_event(
        self,
        parent_receipt_id: str,
        *,
        scope: str,
        payload: dict[str, Any],
        event_id: str,
        event_type: str,
        terminal_outcome: str | None = None,
    ) -> ReceiptRecord:
        """Append a lifecycle event with terminal idempotency/conflict checks."""
        scope = _validate_scope(scope)
        clean_payload = redact_sensitive_dict(payload)
        if not event_id or not event_type:
            raise ValueError("event_id and event_type are required")
        conn = self._get_connection()
        try:
            with self._lock:
                conn.execute("BEGIN IMMEDIATE")
                parent = conn.execute(
                    "SELECT * FROM receipts WHERE receipt_id = ? AND scope = ?",
                    (parent_receipt_id, scope),
                ).fetchone()
                if parent is None:
                    raise KeyError(parent_receipt_id)
                existing = conn.execute(
                    "SELECT * FROM receipts WHERE scope = ? AND event_id = ?", (scope, event_id)
                ).fetchone()
                if existing is not None:
                    record = self._row_to_record(existing)
                    if (
                        record.payload == clean_payload
                        and record.parent_receipt_id == parent_receipt_id
                    ):
                        conn.commit()
                        return record
                    raise ReceiptConflictError("event id conflicts with an existing event")
                if terminal_outcome is not None:
                    terminal = conn.execute(
                        "SELECT terminal_outcome FROM receipts WHERE parent_receipt_id = ? AND scope = ? AND terminal_outcome IS NOT NULL ORDER BY sequence LIMIT 1",
                        (parent_receipt_id, scope),
                    ).fetchone()
                    if terminal is not None:
                        if terminal[0] == terminal_outcome:
                            conn.commit()
                            return self._row_to_record(
                                conn.execute(
                                    "SELECT * FROM receipts WHERE parent_receipt_id = ? AND scope = ? AND terminal_outcome IS NOT NULL ORDER BY sequence LIMIT 1",
                                    (parent_receipt_id, scope),
                                ).fetchone()
                            )
                        raise ReceiptConflictError(
                            "terminal outcome conflicts with prior terminal event"
                        )
                elif (
                    conn.execute(
                        "SELECT 1 FROM receipts WHERE parent_receipt_id = ? AND scope = ? AND terminal_outcome IS NOT NULL LIMIT 1",
                        (parent_receipt_id, scope),
                    ).fetchone()
                    is not None
                ):
                    raise ReceiptConflictError(
                        "cannot append a lifecycle event after terminalization"
                    )
                record = self._insert(
                    conn,
                    receipt_type="execution",
                    scope=scope,
                    payload=clean_payload,
                    sensitivity="internal",
                    provenance=dict(_DEFAULT_PROVENANCE),
                    receipt_id=f"evt-{uuid4().hex}",
                    timestamp=time(),
                    parent_receipt_id=parent_receipt_id,
                    event_id=event_id,
                    event_type=event_type,
                    terminal_outcome=terminal_outcome,
                )
                conn.commit()
                return record
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            if self._shared_conn is None:
                conn.close()

    def _fetch_one(self, query: str, params: tuple[Any, ...]) -> ReceiptRecord | None:
        conn = self._get_connection()
        try:
            row = conn.execute(query, params).fetchone()
            return self._row_to_record(row) if row is not None else None
        finally:
            if self._shared_conn is None:
                conn.close()

    def get_receipt(
        self, receipt_id: str, *, scope: str | None = None, include_tombstones: bool = False
    ) -> ReceiptRecord | None:
        """Fetch one record; a supplied scope always constrains the lookup."""
        self._assert_integrity(scope=scope)
        params: list[Any] = [receipt_id]
        clause = self._scope_clause(scope, params)
        tombstone_clause = (
            ""
            if include_tombstones
            else " AND NOT EXISTS (SELECT 1 FROM receipts t WHERE t.tombstone_for = receipts.receipt_id)"
        )
        return self._fetch_one(
            f"SELECT * FROM receipts WHERE receipt_id = ?{clause}{tombstone_clause}", tuple(params)
        )

    def query_receipts(
        self,
        receipt_type: ReceiptType | None = None,
        scope: str | None = None,
        limit: int = 100,
        *,
        include_tombstones: bool = False,
        parent_receipt_id: str | None = None,
    ) -> list[ReceiptRecord]:
        """Query records, filtering tombstoned facts by default."""
        if limit < 1 or limit > 100_000:
            raise ValueError("receipt query limit must be between 1 and 100000")
        self._assert_integrity(scope=scope)
        query = "SELECT * FROM receipts WHERE 1=1"
        params: list[Any] = []
        if receipt_type is not None:
            query += " AND receipt_type = ?"
            params.append(receipt_type)
        query += self._scope_clause(scope, params)
        if parent_receipt_id is not None:
            query += " AND parent_receipt_id = ?"
            params.append(parent_receipt_id)
        if not include_tombstones:
            query += " AND NOT EXISTS (SELECT 1 FROM receipts t WHERE t.tombstone_for = receipts.receipt_id)"
        query += " ORDER BY sequence DESC LIMIT ?"
        params.append(limit)
        conn = self._get_connection()
        try:
            return [self._row_to_record(row) for row in conn.execute(query, params).fetchall()]
        finally:
            if self._shared_conn is None:
                conn.close()

    def tombstone(self, receipt_id: str, *, scope: str, reason: str = "retention") -> ReceiptRecord:
        """Append a deletion marker without mutating the original fact."""
        if self.get_receipt(receipt_id, scope=scope, include_tombstones=True) is None:
            raise KeyError(receipt_id)
        return self._tombstone(receipt_id, scope=scope, reason=reason)

    def apply_retention(
        self, *, scope: str, max_age_seconds: float, now: float | None = None
    ) -> list[str]:
        """Tombstone old records and return the affected stable IDs."""
        if max_age_seconds < 0:
            raise ValueError("retention age must not be negative")
        cutoff = (time() if now is None else now) - max_age_seconds
        records = [
            record
            for record in self.query_receipts(scope=scope, limit=100_000, include_tombstones=False)
            if record.timestamp < cutoff
        ]
        removed: list[str] = []
        for record in records:
            if not record.is_tombstone:
                self._tombstone(record.receipt_id, scope=scope, reason="retention")
                removed.append(record.receipt_id)
        return removed

    def _tombstone(self, receipt_id: str, *, scope: str, reason: str) -> ReceiptRecord:
        scope = _validate_scope(scope)
        conn = self._get_connection()
        try:
            with self._lock:
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute(
                    "SELECT * FROM receipts WHERE tombstone_for = ? AND scope = ?",
                    (receipt_id, scope),
                ).fetchone()
                if existing is not None:
                    conn.commit()
                    return self._row_to_record(existing)
                record = self._insert(
                    conn,
                    receipt_type="tombstone",
                    scope=scope,
                    payload={"target_receipt_id": receipt_id, "reason": reason},
                    sensitivity="internal",
                    provenance=dict(_DEFAULT_PROVENANCE),
                    receipt_id=f"tomb-{uuid4().hex}",
                    timestamp=time(),
                    parent_receipt_id=receipt_id,
                    tombstone_for=receipt_id,
                )
                conn.commit()
                return record
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            if self._shared_conn is None:
                conn.close()

    def verify_integrity(self, *, scope: str | None = None) -> dict[str, Any]:
        """Verify payload hashes, record hashes, and per-scope hash chains."""
        records = self._query_receipts_unchecked(
            scope=scope, limit=100_000, include_tombstones=True
        )
        by_scope: dict[str, list[ReceiptRecord]] = {}
        for record in records:
            by_scope.setdefault(record.scope, []).append(record)
        errors: list[str] = []
        checked = 0
        for _record_scope, scoped in by_scope.items():
            previous: str | None = None
            for record in sorted(scoped, key=lambda item: item.sequence):
                checked += 1
                content_hash = hashlib.sha256(_canonical(record.payload).encode()).hexdigest()
                if content_hash != record.content_hash:
                    errors.append(f"{record.receipt_id}:content_hash")
                if record.sequence < 1 or record.previous_hash != previous:
                    errors.append(f"{record.receipt_id}:chain_link")
                expected = self._record_hash(
                    record.receipt_id,
                    record.timestamp,
                    record.receipt_type,
                    record.content_hash,
                    record.scope,
                    record.sequence,
                    record.previous_hash,
                    sensitivity=record.sensitivity,
                    provenance=record.provenance,
                    parent_receipt_id=record.parent_receipt_id,
                    event_id=record.event_id,
                    event_type=record.event_type,
                    terminal_outcome=record.terminal_outcome,
                    tombstone_for=record.tombstone_for,
                    key_ref=record.key_ref,
                    idempotency_key=record.idempotency_key,
                )
                if expected != record.record_hash:
                    errors.append(f"{record.receipt_id}:record_hash")
                previous = record.record_hash
        return {"valid": not errors, "checked": checked, "errors": errors, "scope": scope}

    def _query_receipts_unchecked(
        self, *, scope: str | None, limit: int, include_tombstones: bool
    ) -> list[ReceiptRecord]:
        """Internal doctor-only query that can inspect all scopes."""
        query = "SELECT * FROM receipts WHERE 1=1"
        params: list[Any] = []
        if scope is not None:
            query += " AND scope = ?"
            params.append(_validate_scope(scope))
        if not include_tombstones:
            query += " AND NOT EXISTS (SELECT 1 FROM receipts t WHERE t.tombstone_for = receipts.receipt_id)"
        query += " ORDER BY sequence DESC LIMIT ?"
        params.append(limit)
        conn = self._get_connection()
        try:
            return [self._row_to_record(row) for row in conn.execute(query, params).fetchall()]
        finally:
            if self._shared_conn is None:
                conn.close()

    def doctor(self, *, scope: str | None = None) -> dict[str, Any]:
        """Run integrity and SQLite health checks without modifying facts."""
        conn = self._get_connection()
        try:
            integrity = self.verify_integrity(scope=scope)
            quick_check = str(conn.execute("PRAGMA quick_check").fetchone()[0])
            return {
                "schema_version": _SCHEMA_VERSION,
                "sqlite_quick_check": quick_check,
                "integrity": integrity,
                "repairable": quick_check != "ok" or not bool(integrity["valid"]),
            }
        finally:
            if self._shared_conn is None:
                conn.close()

    def backup(self, destination: str | Path) -> Path:
        """Create a consistent SQLite backup before migration or repair."""
        target = Path(destination).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        if self.db_path == ":memory:":
            raise ValueError("in-memory receipt stores cannot be backed up")
        conn = self._get_connection()
        try:
            with sqlite3.connect(str(target)) as backup_conn:
                conn.backup(backup_conn)
        finally:
            if self._shared_conn is None:
                conn.close()
        return target

    def migrate(self, *, backup_path: str | Path | None = None) -> Path | None:
        """Run startup migrations after making an optional pre-migration backup."""
        if backup_path is None:
            return None
        return self.backup(backup_path)

    def export_manifest(
        self, *, scope: str | None = None, include_payload: bool = True
    ) -> dict[str, Any]:
        """Export deterministic, versioned records suitable for import/replay."""
        records = sorted(
            self.query_receipts(scope=scope, limit=100_000, include_tombstones=True),
            key=lambda item: (item.scope, item.sequence, item.receipt_id),
        )
        items: list[dict[str, Any]] = []
        for record in records:
            item: dict[str, Any] = {
                "receipt_id": record.receipt_id,
                "timestamp": record.timestamp,
                "receipt_type": record.receipt_type,
                "scope": record.scope,
                "content_hash": record.content_hash,
                "sensitivity": record.sensitivity,
                "provenance": record.provenance,
                "parent_receipt_id": record.parent_receipt_id,
                "sequence": record.sequence,
                "previous_hash": record.previous_hash,
                "record_hash": record.record_hash,
                "event_id": record.event_id,
                "event_type": record.event_type,
                "terminal_outcome": record.terminal_outcome,
                "idempotency_key": record.idempotency_key,
                "tombstone_for": record.tombstone_for,
                "key_ref": record.key_ref,
            }
            if include_payload:
                item["payload"] = record.payload
            items.append(item)
        digest = hashlib.sha256(_canonical(items).encode()).hexdigest()
        # Preserve the public 1.0 marker used by the initial helper while the
        # record body carries the v2 append-only fields. Import accepts both
        # markers and validates the full canonical record set.
        return {
            "version": "1.0",
            "format_version": "2.0",
            "receipt_count": len(items),
            "manifest_digest": digest,
            "receipts": items,
        }

    def import_manifest(self, manifest: dict[str, Any]) -> int:
        """Import a manifest idempotently after validating its digest and chain."""
        if not isinstance(manifest, dict) or str(manifest.get("version", "")).split(".")[0] not in {
            "1",
            "2",
        }:
            raise ReceiptStoreError("unsupported receipt manifest version")
        items = manifest.get("receipts")
        if not isinstance(items, list):
            raise ReceiptStoreError("manifest receipts must be a list")
        if hashlib.sha256(_canonical(items).encode()).hexdigest() != manifest.get(
            "manifest_digest"
        ):
            raise ReceiptIntegrityError("manifest digest mismatch")
        inserted = 0
        conn = self._get_connection()
        try:
            with self._lock:
                conn.execute("BEGIN IMMEDIATE")
                for item in sorted(
                    items, key=lambda row: (str(row.get("scope")), int(row.get("sequence", 0)))
                ):
                    if "payload" not in item:
                        raise ReceiptStoreError("manifest does not contain importable payloads")
                    receipt_id = str(item["receipt_id"])
                    scope = _validate_scope(str(item["scope"]))
                    existing = conn.execute(
                        "SELECT * FROM receipts WHERE receipt_id = ? AND scope = ?",
                        (receipt_id, scope),
                    ).fetchone()
                    if existing is not None:
                        if self._row_to_record(existing).record_hash != item.get("record_hash"):
                            raise ReceiptConflictError("import conflicts with existing receipt")
                        continue
                    payload = item["payload"]
                    if not isinstance(payload, dict):
                        raise ReceiptStoreError("manifest payload must be an object")
                    record = self._insert(
                        conn,
                        receipt_type=item["receipt_type"],
                        scope=scope,
                        payload=payload,
                        sensitivity=str(item.get("sensitivity", "internal")),
                        provenance=redact_sensitive_dict(
                            item.get("provenance", dict(_DEFAULT_PROVENANCE))
                        ),
                        receipt_id=receipt_id,
                        timestamp=float(item["timestamp"]),
                        parent_receipt_id=item.get("parent_receipt_id"),
                        event_id=item.get("event_id"),
                        event_type=item.get("event_type"),
                        terminal_outcome=item.get("terminal_outcome"),
                        idempotency_key=item.get("idempotency_key"),
                        tombstone_for=item.get("tombstone_for"),
                        key_ref=item.get("key_ref"),
                    )
                    for field in ("content_hash", "record_hash", "sequence", "previous_hash"):
                        if getattr(record, field) != item.get(field):
                            raise ReceiptIntegrityError(
                                f"imported record mismatch: {receipt_id}:{field}"
                            )
                    inserted += 1
                conn.commit()
                return inserted
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            if self._shared_conn is None:
                conn.close()

    def replay(self, *, scope: str, root_receipt_id: str | None = None) -> dict[str, Any]:
        """Replay local facts and linked events without model or network calls."""
        integrity = self.verify_integrity(scope=scope)
        if not integrity["valid"]:
            raise ReceiptIntegrityError("cannot replay an invalid receipt chain")
        records = self.query_receipts(scope=scope, limit=100_000, include_tombstones=True)
        if root_receipt_id is not None:
            records = [
                item
                for item in records
                if item.receipt_id == root_receipt_id or item.parent_receipt_id == root_receipt_id
            ]
        return {
            "scope": scope,
            "receipt_count": len(records),
            "receipts": [
                {
                    "receipt_id": item.receipt_id,
                    "receipt_type": item.receipt_type,
                    "payload": item.payload,
                }
                for item in sorted(records, key=lambda item: item.sequence)
            ],
            "integrity": integrity,
        }

    def export(self, *, scope: str, include_payload: bool = True) -> dict[str, Any]:
        """Scope-explicit alias for deterministic manifest export."""
        return self.export_manifest(scope=scope, include_payload=include_payload)

    def import_bundle(self, bundle: dict[str, Any], *, scope: str) -> int:
        """Import only a manifest whose records all belong to the authorized scope."""
        _validate_scope(scope)
        records = bundle.get("receipts") if isinstance(bundle, dict) else None
        if not isinstance(records, list) or any(item.get("scope") != scope for item in records):
            raise ValueError("receipt import scope mismatch")
        return self.import_manifest(bundle)


REDACT_KEYS = set(_SENSITIVE_KEY_PARTS)

__all__ = [
    "REDACT_KEYS",
    "ReceiptConflictError",
    "ReceiptIntegrityError",
    "ReceiptRecord",
    "ReceiptStore",
    "ReceiptStoreError",
    "ReceiptType",
    "redact_sensitive_dict",
]
