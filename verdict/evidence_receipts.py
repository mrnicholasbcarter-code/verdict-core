"""Versioned, privacy-safe portable evidence receipts (#115)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any

from verdict.capability_passports import (
    EVIDENCE_AUTHORITY_SCHEMA_VERSION,
    CapabilityEvidence,
    CapabilityPassportError,
    CapabilityStatus,
    EvidenceAuthority,
    RouteIdentity,
    _parse_datetime,
    _strict_mapping,
    _string_tuple,
    _utc_datetime,
)

_METADATA_KEYS = frozenset(
    {
        "attempt",
        "correlation_id",
        "cost",
        "failure_class",
        "fallback_count",
        "latency_ms",
        "policy_version",
        "quality_outcome",
        "request_id",
        "route_key",
        "status_code",
        "task_fingerprint",
        "token_counts",
        "transport_outcome",
        "verification_status",
    }
)
_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "password",
        "secret",
        "token",
        "private_key",
        "raw_prompt",
        "raw_completion",
        "messages",
        "tool_arguments",
        "raw_tool_arguments",
    }
)


class ReceiptKind(str, Enum):
    DECISION = "decision"
    CONTEXT = "context"
    EXECUTION = "execution"
    VERIFICATION = "verification"
    OUTCOME = "outcome"


@dataclass(frozen=True)
class EvidenceItem:
    authority: EvidenceAuthority
    source: str
    method: str
    adapter_version: str
    observed_at: datetime
    expires_at: datetime
    scope: str
    confidence: float
    evidence_digest: str
    limitations: tuple[str, ...] = ()
    sample_count: int | None = None

    def __post_init__(self) -> None:
        normalized = CapabilityEvidence(
            status=CapabilityStatus.UNKNOWN,
            source=self.source,
            observed_at=self.observed_at,
            expires_at=self.expires_at,
            confidence=self.confidence,
            evidence_digest=self.evidence_digest,
            limitations=self.limitations,
            authority=self.authority,
            method=self.method,
            adapter_version=self.adapter_version,
            scope=self.scope,
            sample_count=self.sample_count,
        )
        for name in (
            "authority",
            "source",
            "method",
            "adapter_version",
            "observed_at",
            "expires_at",
            "scope",
            "confidence",
            "evidence_digest",
            "limitations",
            "sample_count",
        ):
            object.__setattr__(self, name, getattr(normalized, name))

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "authority": self.authority.value,
            "source": self.source,
            "method": self.method,
            "adapter_version": self.adapter_version,
            "observed_at": _format_datetime(self.observed_at),
            "expires_at": _format_datetime(self.expires_at),
            "scope": self.scope,
            "confidence": self.confidence,
            "evidence_digest": self.evidence_digest,
            "limitations": list(self.limitations),
        }
        if self.sample_count is not None:
            payload["sample_count"] = self.sample_count
        return payload


@dataclass(frozen=True)
class EvidenceReceipt:
    """Append-only receipt metadata with exact requested/selected/actual routes."""

    receipt_id: str
    kind: ReceiptKind
    scope: str
    occurred_at: datetime
    evidence: tuple[EvidenceItem, ...]
    requested_alias: str | None = None
    selected_route: RouteIdentity | None = None
    actual_route: RouteIdentity | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    parent_receipt_ids: tuple[str, ...] = ()
    extensions: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = EVIDENCE_AUTHORITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != EVIDENCE_AUTHORITY_SCHEMA_VERSION:
            raise CapabilityPassportError("receipt schema_version must be '1'")
        if not isinstance(self.receipt_id, str) or not self.receipt_id.strip():
            raise CapabilityPassportError("receipt_id must be non-empty")
        try:
            object.__setattr__(self, "kind", ReceiptKind(self.kind))
        except ValueError as exc:
            raise CapabilityPassportError("receipt kind is invalid") from exc
        if not isinstance(self.scope, str) or not self.scope.strip():
            raise CapabilityPassportError("receipt scope must be non-empty")
        object.__setattr__(self, "occurred_at", _utc_datetime(self.occurred_at, "occurred_at"))
        if self.requested_alias is not None and not self.requested_alias.strip():
            raise CapabilityPassportError("receipt.requested_alias must be non-empty")
        for name in ("selected_route", "actual_route"):
            if getattr(self, name) is not None and not isinstance(
                getattr(self, name), RouteIdentity
            ):
                raise CapabilityPassportError(f"receipt.{name} must be a route identity")
        evidence = tuple(self.evidence)
        if not evidence or any(not isinstance(item, EvidenceItem) for item in evidence):
            raise CapabilityPassportError("receipt evidence must contain EvidenceItem values")
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "payload", _metadata(self.payload, "payload", allowlist=True))
        object.__setattr__(
            self, "parent_receipt_ids", _string_tuple(self.parent_receipt_ids, "parent_receipt_ids")
        )
        object.__setattr__(self, "extensions", _metadata(self.extensions, "extensions"))

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "kind": self.kind.value,
            "scope": self.scope,
            "occurred_at": _format_datetime(self.occurred_at),
            "evidence": [item.to_dict() for item in self.evidence],
            "payload": dict(self.payload),
            "parent_receipt_ids": list(self.parent_receipt_ids),
            "extensions": dict(self.extensions),
        }
        if self.requested_alias is not None:
            payload["requested_alias"] = self.requested_alias
        if self.selected_route is not None:
            payload["selected_route"] = self.selected_route.to_dict()
        if self.actual_route is not None:
            payload["actual_route"] = self.actual_route.to_dict()
        return payload

    @property
    def digest(self) -> str:
        canonical = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> EvidenceReceipt:
        payload = _strict_mapping(
            value,
            required={
                "schema_version",
                "receipt_id",
                "kind",
                "scope",
                "occurred_at",
                "evidence",
                "payload",
                "parent_receipt_ids",
                "extensions",
            },
            optional={"requested_alias", "selected_route", "actual_route"},
            field_name="evidence_receipt",
        )
        if not isinstance(payload["evidence"], list):
            raise CapabilityPassportError("evidence_receipt.evidence must be an array")
        return cls(
            schema_version=payload["schema_version"],
            receipt_id=payload["receipt_id"],
            kind=payload["kind"],
            scope=payload["scope"],
            occurred_at=_parse_datetime(payload["occurred_at"], "occurred_at"),
            evidence=tuple(_parse_item(item) for item in payload["evidence"]),
            requested_alias=payload.get("requested_alias"),
            selected_route=_parse_route(payload.get("selected_route")),
            actual_route=_parse_route(payload.get("actual_route")),
            payload=payload["payload"],
            parent_receipt_ids=tuple(payload["parent_receipt_ids"]),
            extensions=payload["extensions"],
        )


def _parse_route(value: Any) -> RouteIdentity | None:
    return RouteIdentity.from_dict(value) if value is not None else None


def _parse_item(value: Any) -> EvidenceItem:
    if not isinstance(value, Mapping):
        raise CapabilityPassportError("evidence_item must be an object")
    required = {
        "authority",
        "source",
        "method",
        "adapter_version",
        "observed_at",
        "expires_at",
        "scope",
        "confidence",
        "evidence_digest",
        "limitations",
    }
    unknown = set(value) - required - {"sample_count"}
    missing = required - set(value)
    if missing:
        raise CapabilityPassportError(
            f"evidence_item missing field(s): {', '.join(sorted(missing))}"
        )
    if unknown:
        raise CapabilityPassportError(
            f"evidence_item has unknown field(s): {', '.join(sorted(map(str, unknown)))}"
        )
    payload = dict(value)
    return EvidenceItem(
        authority=payload["authority"],
        source=payload["source"],
        method=payload["method"],
        adapter_version=payload["adapter_version"],
        observed_at=datetime.fromisoformat(str(payload["observed_at"]).replace("Z", "+00:00")),
        expires_at=datetime.fromisoformat(str(payload["expires_at"]).replace("Z", "+00:00")),
        scope=payload["scope"],
        confidence=payload["confidence"],
        evidence_digest=payload["evidence_digest"],
        limitations=tuple(payload["limitations"]),
        sample_count=payload.get("sample_count"),
    )


def _metadata(value: Any, field_name: str, *, allowlist: bool = False) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CapabilityPassportError(f"{field_name} must be an object")
    _reject_sensitive(value, field_name)
    unknown = set(value) - _METADATA_KEYS
    if allowlist and unknown:
        raise CapabilityPassportError(
            f"{field_name} has unknown field(s): {', '.join(sorted(map(str, unknown)))}"
        )
    return MappingProxyType({str(key): _json_copy(child) for key, child in value.items()})


def _reject_sensitive(value: Any, field_name: str) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).lower().replace("-", "_") in _SENSITIVE_KEYS:
                raise CapabilityPassportError(f"{field_name} contains sensitive field {key!r}")
            _reject_sensitive(child, field_name)
    elif isinstance(value, list):
        for child in value:
            _reject_sensitive(child, field_name)


def _json_copy(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_copy(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_json_copy(child) for child in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise CapabilityPassportError("receipt metadata must be JSON-compatible")


def _format_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = ["EvidenceItem", "EvidenceReceipt", "ReceiptKind"]
