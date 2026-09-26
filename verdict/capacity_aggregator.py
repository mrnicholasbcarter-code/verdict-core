"""Bounded, secret-safe, versioned aggregator JSON capacity adapter (capacity evidence).

External tools (quota-cli / CodexBar-compatible) emit JSON. Verdict validates
schema, bounds size, rejects secrets, and never shells with interpolation.
Credentials are never passed unless the caller explicitly supplies a pre-read
payload (tests use fixtures).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from verdict.capacity_models import (
    Balance,
    CapacityEvidenceError,
    CapacityFailureClass,
    CapacityObservationError,
    CapacityPool,
    CapacitySignal,
    CapacitySnapshot,
    ConnectionIdentity,
    DiagnoseReport,
    EvidenceAuthority,
    RefreshPolicy,
    _reject_secrets,
    _utc,
)

AGGREGATOR_CONTRACT_VERSION = "1"
_MAX_JSON_BYTES = 256_000
_MAX_POOLS = 64
_MAX_BALANCES = 32


def _parse_dt(raw: Any) -> datetime | None:
    if raw is None:
        return None
    return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))


def validate_aggregator_document(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Schema-validate aggregator JSON (versioned, bounded, secret-safe)."""

    _reject_secrets(payload, "aggregator")
    version = str(payload.get("contract_version", ""))
    if version != AGGREGATOR_CONTRACT_VERSION:
        raise CapacityEvidenceError(
            f"aggregator contract_version must be '{AGGREGATOR_CONTRACT_VERSION}'"
        )
    if "provider_id" not in payload or "account_id" not in payload:
        raise CapacityEvidenceError("aggregator document missing provider_id/account_id")
    pools = payload.get("pools", [])
    balances = payload.get("balances", [])
    if not isinstance(pools, list) or not isinstance(balances, list):
        raise CapacityEvidenceError("pools/balances must be arrays")
    if len(pools) > _MAX_POOLS or len(balances) > _MAX_BALANCES:
        raise CapacityEvidenceError("aggregator document exceeds pool/balance bounds")
    return payload


class AggregatorJsonCapacityAdapter:
    """Read-only external JSON capacity source."""

    def __init__(
        self,
        adapter_id: str = "aggregator.json",
        *,
        documents: Sequence[Mapping[str, Any]] = (),
        last_good: Mapping[str, CapacitySnapshot] | None = None,
    ) -> None:
        self._adapter_id = adapter_id
        self._documents: list[Mapping[str, Any]] = []
        for doc in documents:
            self._documents.append(validate_aggregator_document(doc))
        self._last_good: dict[str, CapacitySnapshot] = dict(last_good or {})

    @property
    def adapter_id(self) -> str:
        return self._adapter_id

    def discover(self) -> Sequence[ConnectionIdentity]:
        return tuple(
            ConnectionIdentity(
                provider_id=str(doc["provider_id"]),
                account_id=str(doc["account_id"]),
                adapter_id=self._adapter_id,
                access_mode=str(doc.get("access_mode", "unknown")),  # type: ignore[arg-type]
                workspace_id=(
                    None if doc.get("workspace_id") is None else str(doc["workspace_id"])
                ),
            )
            for doc in self._documents
        )

    def capabilities(self) -> Mapping[CapacitySignal, bool]:
        return {
            CapacitySignal.QUOTA_WINDOWS: True,
            CapacitySignal.BALANCES: True,
            CapacitySignal.RESET_TIMES: True,
            CapacitySignal.COOLDOWNS: True,
            CapacitySignal.RETRY_AFTER: False,
            CapacitySignal.SHARED_POOLS: True,
            CapacitySignal.HEALTH: False,
        }

    def refresh_policy(self) -> RefreshPolicy:
        return RefreshPolicy(ttl_seconds=90, prefer_reset_at=True, honor_retry_after=False)

    def diagnose(self) -> DiagnoseReport:
        return DiagnoseReport(
            adapter_id=self._adapter_id,
            available=bool(self._documents),
            status="ok" if self._documents else "no_documents",
            details={"document_count": len(self._documents)},
        )

    def observe_capacity(
        self, identity: ConnectionIdentity | None = None, *, now: datetime | None = None
    ) -> CapacitySnapshot:
        moment = _utc(now)
        if identity is None:
            discovered = self.discover()
            if not discovered:
                return CapacitySnapshot(
                    identity=ConnectionIdentity(
                        provider_id="unknown", account_id="none", adapter_id=self._adapter_id
                    ),
                    source_kind="aggregator",
                    authority=EvidenceAuthority.AGGREGATOR,
                    observed_at=moment,
                    errors=(
                        CapacityObservationError(
                            failure_class=CapacityFailureClass.UNSUPPORTED,
                            message="no aggregator documents",
                        ),
                    ),
                    notes=("unknown",),
                )
            identity = discovered[0]

        doc = next(
            (
                item
                for item in self._documents
                if str(item["account_id"]) == identity.account_id
                and str(item["provider_id"]) == identity.provider_id
            ),
            None,
        )
        if doc is None:
            # Preserve last-good separately from current failure.
            last = self._last_good.get(identity.account_id)
            error = CapacityObservationError(
                failure_class=CapacityFailureClass.PARSE_ERROR,
                message="aggregator document missing for identity",
            )
            if last is not None:
                return CapacitySnapshot(
                    identity=identity,
                    source_kind="aggregator",
                    authority=EvidenceAuthority.AGGREGATOR,
                    observed_at=moment,
                    pools=last.pools,
                    balances=last.balances,
                    fresh_until=last.fresh_until,
                    confidence=None,
                    errors=(error,),
                    notes=("last_good_retained",),
                )
            return CapacitySnapshot(
                identity=identity,
                source_kind="aggregator",
                authority=EvidenceAuthority.AGGREGATOR,
                observed_at=moment,
                errors=(error,),
                notes=("unknown",),
            )

        snapshot = CapacitySnapshot(
            identity=identity,
            source_kind="aggregator",
            authority=EvidenceAuthority.AGGREGATOR,
            observed_at=_parse_dt(doc.get("observed_at")) or moment,
            pools=tuple(CapacityPool.from_dict(item) for item in doc.get("pools", ())),
            balances=tuple(Balance.from_dict(item) for item in doc.get("balances", ())),
            fresh_until=_parse_dt(doc.get("fresh_until")) or (moment + timedelta(seconds=90)),
            confidence=doc.get("confidence", 0.7),
            notes=("aggregator",),
        )
        self._last_good[identity.account_id] = snapshot
        return snapshot

    @classmethod
    def from_json_bytes(
        cls, raw: bytes, *, adapter_id: str = "aggregator.json"
    ) -> AggregatorJsonCapacityAdapter:
        if len(raw) > _MAX_JSON_BYTES:
            raise CapacityEvidenceError("aggregator JSON exceeds size bound")
        payload = json.loads(raw.decode("utf-8"))
        documents = payload if isinstance(payload, list) else [payload]
        return cls(adapter_id, documents=documents)

    @classmethod
    def from_path(
        cls, path: Path, *, adapter_id: str = "aggregator.json"
    ) -> AggregatorJsonCapacityAdapter:
        raw = path.read_bytes()
        return cls.from_json_bytes(raw, adapter_id=adapter_id)


__all__ = [
    "AGGREGATOR_CONTRACT_VERSION",
    "AggregatorJsonCapacityAdapter",
    "validate_aggregator_document",
]
