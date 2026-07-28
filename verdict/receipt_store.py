"""Durable, privacy-safe SQLite receipt store and memory manifest ledger (#117).

Provides append-only persistence for decision, context, execution, verification,
and outcome receipts as well as memory import/export manifests.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from time import time
from typing import Any, Literal

ReceiptType = Literal["decision", "context", "execution", "verification", "outcome", "manifest"]


@dataclass(frozen=True)
class ReceiptRecord:
    """A immutable, privacy-safe receipt stored in the ledger."""

    receipt_id: str
    timestamp: float
    receipt_type: ReceiptType
    scope: str
    payload: dict[str, Any]
    content_hash: str
    sensitivity: str
    provenance: dict[str, Any]


REDACT_KEYS = {
    "api_key",
    "apikey",
    "secret",
    "password",
    "token",
    "credential",
    "auth",
    "private_key",
    "account_id",
}


def redact_sensitive_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Recursively redact keys matching sensitive keywords."""
    redacted: dict[str, Any] = {}
    for key, value in data.items():
        key_lower = key.lower()
        if any(rk in key_lower for rk in REDACT_KEYS):
            redacted[key] = "[REDACTED]"
        elif isinstance(value, dict):
            redacted[key] = redact_sensitive_dict(value)
        elif isinstance(value, list):
            redacted[key] = [
                redact_sensitive_dict(item) if isinstance(item, dict) else item for item in value
            ]
        else:
            redacted[key] = value
    return redacted


class ReceiptStore:
    """Thread-safe SQLite append-only receipt store."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.db_path = str(db_path)
        # Keep connection open if in-memory DB
        self._shared_conn: sqlite3.Connection | None = None
        if self.db_path == ":memory:":
            self._shared_conn = sqlite3.connect(":memory:")
            self._shared_conn.row_factory = sqlite3.Row
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        if self._shared_conn:
            return self._shared_conn
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        conn = self._get_connection()
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
                provenance_json TEXT NOT NULL
            );
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_receipts_type ON receipts(receipt_type);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_receipts_scope ON receipts(scope);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_receipts_timestamp ON receipts(timestamp);")
        conn.commit()
        if not self._shared_conn:
            conn.close()

    def put_receipt(
        self,
        receipt_type: ReceiptType,
        scope: str,
        payload: dict[str, Any],
        sensitivity: str = "internal",
        provenance: dict[str, Any] | None = None,
        receipt_id: str | None = None,
    ) -> ReceiptRecord:
        """Store a new immutable receipt in the store with automatic redaction."""
        now = time()
        clean_payload = redact_sensitive_dict(payload)
        prov = provenance or {"source": "verdict_receipt_store", "version": "1.0"}
        payload_str = json.dumps(clean_payload, sort_keys=True)
        content_hash = hashlib.sha256(payload_str.encode("utf-8")).hexdigest()

        r_id = (
            receipt_id
            or hashlib.sha256(f"{receipt_type}:{scope}:{now}:{content_hash}".encode()).hexdigest()[
                :16
            ]
        )

        record = ReceiptRecord(
            receipt_id=r_id,
            timestamp=now,
            receipt_type=receipt_type,
            scope=scope,
            payload=clean_payload,
            content_hash=content_hash,
            sensitivity=sensitivity,
            provenance=prov,
        )

        conn = self._get_connection()
        try:
            conn.execute(
                """
                INSERT INTO receipts (
                    receipt_id, timestamp, receipt_type, scope,
                    payload_json, content_hash, sensitivity, provenance_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.receipt_id,
                    record.timestamp,
                    record.receipt_type,
                    record.scope,
                    json.dumps(record.payload, sort_keys=True),
                    record.content_hash,
                    record.sensitivity,
                    json.dumps(record.provenance, sort_keys=True),
                ),
            )
            conn.commit()
        finally:
            if not self._shared_conn:
                conn.close()

        return record

    def get_receipt(self, receipt_id: str) -> ReceiptRecord | None:
        """Fetch a single receipt by ID."""
        conn = self._get_connection()
        try:
            cursor = conn.execute("SELECT * FROM receipts WHERE receipt_id = ?", (receipt_id,))
            row = cursor.fetchone()
            if not row:
                return None

            return ReceiptRecord(
                receipt_id=row["receipt_id"],
                timestamp=row["timestamp"],
                receipt_type=row["receipt_type"],
                scope=row["scope"],
                payload=json.loads(row["payload_json"]),
                content_hash=row["content_hash"],
                sensitivity=row["sensitivity"],
                provenance=json.loads(row["provenance_json"]),
            )
        finally:
            if not self._shared_conn:
                conn.close()

    def query_receipts(
        self, receipt_type: ReceiptType | None = None, scope: str | None = None, limit: int = 100
    ) -> list[ReceiptRecord]:
        """Query receipts filtered by type or scope."""
        query = "SELECT * FROM receipts WHERE 1=1"
        params: list[Any] = []

        if receipt_type:
            query += " AND receipt_type = ?"
            params.append(receipt_type)
        if scope:
            query += " AND scope = ?"
            params.append(scope)

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        results: list[ReceiptRecord] = []
        conn = self._get_connection()
        try:
            cursor = conn.execute(query, params)
            for row in cursor.fetchall():
                results.append(
                    ReceiptRecord(
                        receipt_id=row["receipt_id"],
                        timestamp=row["timestamp"],
                        receipt_type=row["receipt_type"],
                        scope=row["scope"],
                        payload=json.loads(row["payload_json"]),
                        content_hash=row["content_hash"],
                        sensitivity=row["sensitivity"],
                        provenance=json.loads(row["provenance_json"]),
                    )
                )
        finally:
            if not self._shared_conn:
                conn.close()

        return results

    def export_manifest(self) -> dict[str, Any]:
        """Export a portable manifest of all receipts."""
        receipts = self.query_receipts(limit=10000)
        items = [
            {
                "receipt_id": r.receipt_id,
                "timestamp": r.timestamp,
                "receipt_type": r.receipt_type,
                "scope": r.scope,
                "content_hash": r.content_hash,
                "sensitivity": r.sensitivity,
            }
            for r in receipts
        ]
        digest = hashlib.sha256(json.dumps(items, sort_keys=True).encode("utf-8")).hexdigest()
        return {
            "version": "1.0",
            "receipt_count": len(items),
            "manifest_digest": digest,
            "receipts": items,
        }


__all__ = ["REDACT_KEYS", "ReceiptRecord", "ReceiptStore", "ReceiptType", "redact_sensitive_dict"]
