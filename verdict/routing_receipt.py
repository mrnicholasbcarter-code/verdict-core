"""Routing receipt: one versioned routing receipt with the full decision/evidence chain.

``RoutingReceiptV1`` is the durable explanation surface for a live Verdict
route. It is evidence, not a new routing authority. Persistence uses the
existing :class:`~verdict.receipt_store.ReceiptStore` by default.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

from verdict.receipt_store import (
    ReceiptConflictError,
    ReceiptRecord,
    ReceiptStore,
    ReceiptStoreError,
    redact_sensitive_dict,
)

ROUTING_RECEIPT_SCHEMA_VERSION = "routing-receipt/v1"
DIGEST_PREFIX = "sha256:"
EvidenceKind = Literal["estimated", "observed", "unknown"]
ReceiptState = Literal["in_progress", "finalized", "failed", "superseded"]

# Volatile / presentation-only fields excluded from the canonical digest.
DIGEST_EXCLUDED = frozenset(
    {
        "created_at",
        "updated_at",
        "started_at",
        "finished_at",
        "persisted_at",
        "wall_clock",
        "human_summary",
    }
)

# ── Reason-code registry (stable machine contract) ──────────────────────────

REASON_PROVIDER_INACTIVE = "provider_inactive"
REASON_PROVIDER_UNREACHABLE = "provider_unreachable"
REASON_AUTH_FAILED = "auth_failed"
REASON_QUOTA_OR_CAPACITY_DENIED = "quota_or_capacity_denied"
REASON_REQUIRED_CAPABILITY_MISSING = "required_capability_missing"
REASON_REQUIRED_TOOL_MISSING = "required_tool_missing"
REASON_CONTEXT_BUDGET_INFEASIBLE = "context_budget_infeasible"
REASON_OUTPUT_BUDGET_INFEASIBLE = "output_budget_infeasible"
REASON_SPEND_POLICY_DENIED = "spend_policy_denied"
REASON_SECURITY_POLICY_DENIED = "security_policy_denied"
REASON_QUALITY_UNKNOWN = "quality_unknown"
REASON_NOT_SHORTLISTED = "not_shortlisted"
REASON_PROBE_FAILED = "probe_failed"
REASON_STALE_EVIDENCE = "stale_evidence"
REASON_SELECTED = "selected"
REASON_IDENTITY_MISMATCH = "identity_mismatch"
REASON_EXECUTION_FAILED = "execution_failed"
REASON_VERIFICATION_FAILED = "verification_failed"

# Reused free-tier / pool codes (already machine-stable in-repo).
REASON_OPAQUE_AUTO = "opaque_auto"
REASON_NOT_FREE_TIER = "not_free_tier"
REASON_INACTIVE_UNCONNECTED = "inactive_unconnected"
REASON_METADATA_GHOST = "metadata_ghost"
REASON_CAPABILITY_MISMATCH = "capability_mismatch"
REASON_REQUIRED_UNKNOWN = "required_unknown"
REASON_UNMAPPED = "unmapped"
REASON_STALE = "stale"
REASON_PAID_FALLBACK = "paid_fallback"
REASON_SPEND_POLICY_EXCLUDES_PAID = "spend_policy_excludes_paid"
REASON_SPEND_POLICY_REQUIRES_FRONTIER = "spend_policy_requires_frontier"
REASON_HARD_HEALTH = "hard_health"
REASON_POLICY_EXCLUDED = "policy_excluded"
REASON_CANDIDATE_POOL_NOT_SHORTLISTED = "candidate_pool_not_shortlisted"

REASON_CODES: frozenset[str] = frozenset(
    {
        REASON_PROVIDER_INACTIVE,
        REASON_PROVIDER_UNREACHABLE,
        REASON_AUTH_FAILED,
        REASON_QUOTA_OR_CAPACITY_DENIED,
        REASON_REQUIRED_CAPABILITY_MISSING,
        REASON_REQUIRED_TOOL_MISSING,
        REASON_CONTEXT_BUDGET_INFEASIBLE,
        REASON_OUTPUT_BUDGET_INFEASIBLE,
        REASON_SPEND_POLICY_DENIED,
        REASON_SECURITY_POLICY_DENIED,
        REASON_QUALITY_UNKNOWN,
        REASON_NOT_SHORTLISTED,
        REASON_PROBE_FAILED,
        REASON_STALE_EVIDENCE,
        REASON_SELECTED,
        REASON_IDENTITY_MISMATCH,
        REASON_EXECUTION_FAILED,
        REASON_VERIFICATION_FAILED,
        REASON_OPAQUE_AUTO,
        REASON_NOT_FREE_TIER,
        REASON_INACTIVE_UNCONNECTED,
        REASON_METADATA_GHOST,
        REASON_CAPABILITY_MISMATCH,
        REASON_REQUIRED_UNKNOWN,
        REASON_UNMAPPED,
        REASON_STALE,
        REASON_PAID_FALLBACK,
        REASON_SPEND_POLICY_EXCLUDES_PAID,
        REASON_SPEND_POLICY_REQUIRES_FRONTIER,
        REASON_HARD_HEALTH,
        REASON_POLICY_EXCLUDED,
        REASON_CANDIDATE_POOL_NOT_SHORTLISTED,
    }
)

# Map legacy drop codes onto the routing receipt registry when they already match.
_LEGACY_REASON_ALIASES: dict[str, str] = {
    "inactive_unconnected": REASON_PROVIDER_INACTIVE,
    "stale": REASON_STALE_EVIDENCE,
    "capability_mismatch": REASON_REQUIRED_CAPABILITY_MISSING,
    "candidate_pool_not_shortlisted": REASON_NOT_SHORTLISTED,
    "hard_health": REASON_PROVIDER_UNREACHABLE,
    "policy_excluded": REASON_SECURITY_POLICY_DENIED,
}


class RoutingReceiptError(ValueError):
    """Base error for routing receipt contract violations."""


class RoutingReceiptSchemaError(RoutingReceiptError):
    """Raised when a receipt schema version is unsupported."""


class RoutingReceiptPersistError(RoutingReceiptError, ReceiptStoreError):
    """Raised when a receipt cannot be durably persisted."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(value: Any) -> str:
    return DIGEST_PREFIX + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _require_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RoutingReceiptError(f"{name} must be a non-empty string")
    return value.strip()


def _require_digest(value: Any, name: str) -> str:
    text = _require_string(value, name)
    if (
        not (re.fullmatch(r"sha256:[0-9a-f]{64}", text) or re.fullmatch(r"[0-9a-f]{64}", text))
        and not text.startswith(DIGEST_PREFIX)
        and len(text) < 8
    ):
        # Allow prefixed or raw hex; also allow opaque digests already produced
        # by sibling modules (they may use the sha256: prefix).
        raise RoutingReceiptError(f"{name} must be a digest")
    return text


def normalize_reason_code(code: str) -> str:
    """Return a registry reason code, mapping known legacy aliases."""
    raw = _require_string(code, "reason_code")
    if raw in REASON_CODES:
        return raw
    aliased = _LEGACY_REASON_ALIASES.get(raw)
    if aliased is not None:
        return aliased
    # Unknown codes are preserved but must be machine-shaped (snake_case).
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", raw):
        raise RoutingReceiptError(f"reason_code {raw!r} is not machine-stable")
    return raw


def assert_known_or_stable(code: str) -> str:
    return normalize_reason_code(code)


@dataclass(frozen=True)
class EvidenceValue:
    """Numeric/evidence field with explicit provenance (estimated≠observed)."""

    kind: EvidenceKind
    value: Any = None
    source: str | None = None
    freshness: str | None = None
    unit: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("estimated", "observed", "unknown"):
            raise RoutingReceiptError("EvidenceValue.kind must be estimated|observed|unknown")
        if self.kind == "unknown" and self.value is not None:
            raise RoutingReceiptError("unknown EvidenceValue must not carry a concrete value")
        if self.kind != "unknown" and self.value is None:
            raise RoutingReceiptError(f"{self.kind} EvidenceValue requires a value")
        if self.source is not None:
            _require_string(self.source, "EvidenceValue.source")
        if self.freshness is not None:
            _require_string(self.freshness, "EvidenceValue.freshness")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"kind": self.kind, "value": self.value}
        if self.source is not None:
            payload["source"] = self.source
        if self.freshness is not None:
            payload["freshness"] = self.freshness
        if self.unit is not None:
            payload["unit"] = self.unit
        return payload

    @classmethod
    def from_dict(cls, raw: Any) -> EvidenceValue:
        if not isinstance(raw, Mapping):
            raise RoutingReceiptError("EvidenceValue must be an object")
        return cls(
            kind=cast(EvidenceKind, raw["kind"]),
            value=raw.get("value"),
            source=raw.get("source"),
            freshness=raw.get("freshness"),
            unit=raw.get("unit"),
        )

    @classmethod
    def unknown(cls, *, source: str | None = None) -> EvidenceValue:
        return cls(kind="unknown", value=None, source=source)

    @classmethod
    def estimated(
        cls,
        value: Any,
        *,
        source: str | None = None,
        freshness: str | None = None,
        unit: str | None = None,
    ) -> EvidenceValue:
        return cls(kind="estimated", value=value, source=source, freshness=freshness, unit=unit)

    @classmethod
    def observed(
        cls,
        value: Any,
        *,
        source: str | None = None,
        freshness: str | None = None,
        unit: str | None = None,
    ) -> EvidenceValue:
        return cls(kind="observed", value=value, source=source, freshness=freshness, unit=unit)


@dataclass(frozen=True)
class RouteRef:
    """Concrete route identity (gateway/provider/resource_pool/model)."""

    gateway: str
    provider: str
    model: str
    resource_pool: str | None = None
    route_id: str | None = None
    credential_pool: str | None = None

    def __post_init__(self) -> None:
        _require_string(self.gateway, "gateway")
        _require_string(self.provider, "provider")
        _require_string(self.model, "model")
        if self.resource_pool is not None:
            _require_string(self.resource_pool, "resource_pool")
        if self.route_id is not None:
            _require_string(self.route_id, "route_id")
        if self.credential_pool is not None:
            _require_string(self.credential_pool, "credential_pool")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "gateway": self.gateway,
            "provider": self.provider,
            "model": self.model,
        }
        # Prefer resource_pool (Linear contract); keep credential_pool when distinct.
        pool = self.resource_pool if self.resource_pool is not None else self.credential_pool
        if pool is not None:
            payload["resource_pool"] = pool
        if self.credential_pool is not None and self.credential_pool != pool:
            payload["credential_pool"] = self.credential_pool
        if self.route_id is not None:
            payload["route_id"] = self.route_id
        return payload

    @classmethod
    def from_dict(cls, raw: Any) -> RouteRef:
        if not isinstance(raw, Mapping):
            raise RoutingReceiptError("RouteRef must be an object")
        return cls(
            gateway=str(raw["gateway"]),
            provider=str(raw["provider"]),
            model=str(raw["model"]),
            resource_pool=(
                str(raw["resource_pool"])
                if raw.get("resource_pool") is not None
                else (
                    str(raw["credential_pool"]) if raw.get("credential_pool") is not None else None
                )
            ),
            route_id=str(raw["route_id"]) if raw.get("route_id") is not None else None,
            credential_pool=str(raw["credential_pool"])
            if raw.get("credential_pool") is not None
            else None,
        )


@dataclass(frozen=True)
class CandidateRow:
    """Bounded inspectable candidate decision row."""

    candidate_id: str
    identity: RouteRef | None = None
    eligible: bool | None = None
    reason_codes: tuple[str, ...] = ()
    detail: str | None = None
    task_fit: Mapping[str, Any] | None = None
    quality_evidence: Mapping[str, Any] | None = None
    capacity_freshness: str | None = None
    context_plan_digest: str | None = None
    estimated_input_tokens: EvidenceValue | None = None
    confirm: Mapping[str, Any] | None = None
    passport: Mapping[str, Any] | None = None
    reached_decision_input: bool = False
    shortlisted: bool = False

    def __post_init__(self) -> None:
        _require_string(self.candidate_id, "candidate_id")
        codes = tuple(assert_known_or_stable(code) for code in self.reason_codes)
        object.__setattr__(self, "reason_codes", codes)
        if self.context_plan_digest is not None:
            _require_digest(self.context_plan_digest, "context_plan_digest")
        if self.detail is not None and len(self.detail) > 512:
            raise RoutingReceiptError("candidate detail must be bounded (<=512)")
        if self.task_fit is not None:
            object.__setattr__(self, "task_fit", dict(self.task_fit))
        if self.quality_evidence is not None:
            object.__setattr__(self, "quality_evidence", dict(self.quality_evidence))
        if self.confirm is not None:
            object.__setattr__(self, "confirm", dict(self.confirm))
        if self.passport is not None:
            object.__setattr__(self, "passport", dict(self.passport))

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "candidate_id": self.candidate_id,
            "eligible": self.eligible,
            "reason_codes": list(self.reason_codes),
            "reached_decision_input": self.reached_decision_input,
            "shortlisted": self.shortlisted,
        }
        if self.identity is not None:
            payload["identity"] = self.identity.to_dict()
        if self.detail is not None:
            payload["detail"] = self.detail
        if self.task_fit is not None:
            payload["task_fit"] = dict(self.task_fit)
        if self.quality_evidence is not None:
            payload["quality_evidence"] = dict(self.quality_evidence)
        if self.capacity_freshness is not None:
            payload["capacity_freshness"] = self.capacity_freshness
        if self.context_plan_digest is not None:
            payload["context_plan_digest"] = self.context_plan_digest
        if self.estimated_input_tokens is not None:
            payload["estimated_input_tokens"] = self.estimated_input_tokens.to_dict()
        if self.confirm is not None:
            payload["confirm"] = dict(self.confirm)
        if self.passport is not None:
            payload["passport"] = dict(self.passport)
        return payload

    @classmethod
    def from_dict(cls, raw: Any) -> CandidateRow:
        if not isinstance(raw, Mapping):
            raise RoutingReceiptError("CandidateRow must be an object")
        identity = raw.get("identity")
        tokens = raw.get("estimated_input_tokens")
        return cls(
            candidate_id=str(raw["candidate_id"]),
            identity=None if identity is None else RouteRef.from_dict(identity),
            eligible=raw.get("eligible"),
            reason_codes=tuple(raw.get("reason_codes") or ()),
            detail=raw.get("detail"),
            task_fit=raw.get("task_fit"),
            quality_evidence=raw.get("quality_evidence"),
            capacity_freshness=raw.get("capacity_freshness"),
            context_plan_digest=raw.get("context_plan_digest"),
            estimated_input_tokens=None if tokens is None else EvidenceValue.from_dict(tokens),
            confirm=raw.get("confirm"),
            passport=raw.get("passport"),
            reached_decision_input=bool(raw.get("reached_decision_input", False)),
            shortlisted=bool(raw.get("shortlisted", False)),
        )


def _strip_for_digest(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _strip_for_digest(item)
            for key, item in value.items()
            if key not in DIGEST_EXCLUDED
        }
    if isinstance(value, list):
        return [_strip_for_digest(item) for item in value]
    if isinstance(value, tuple):
        return [_strip_for_digest(item) for item in value]
    return value


def canonical_routing_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return the normalized payload used for the decision/evidence digest."""
    if not isinstance(payload, Mapping):
        raise RoutingReceiptError("canonical payload must be an object")
    cleaned = _strip_for_digest(dict(payload))
    # decision_digest itself must not feed into the digest.
    cleaned.pop("decision_digest", None)
    return cast(dict[str, Any], cleaned)


def decision_digest_for(payload: Mapping[str, Any]) -> str:
    return _digest(canonical_routing_payload(payload))


@dataclass(frozen=True)
class RoutingReceiptV1:
    """Versioned envelope for one live routing/execution decision."""

    receipt_id: str
    schema_version: str = ROUTING_RECEIPT_SCHEMA_VERSION
    story_id: str | None = None
    work_unit_id: str | None = None
    attempt_id: str | None = None
    created_at: str = field(default_factory=_now_iso)
    state: ReceiptState = "in_progress"

    task_profile: Mapping[str, Any] = field(default_factory=dict)
    evidence_snapshot: Mapping[str, Any] = field(default_factory=dict)
    candidate_pipeline: tuple[CandidateRow, ...] = ()
    decision: Mapping[str, Any] = field(default_factory=dict)
    hydration: Mapping[str, Any] = field(default_factory=dict)
    execution: Mapping[str, Any] = field(default_factory=dict)
    verification: Mapping[str, Any] = field(default_factory=dict)
    final: Mapping[str, Any] = field(default_factory=dict)
    decision_digest: str | None = None
    parent_receipt_id: str | None = None
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema_version != ROUTING_RECEIPT_SCHEMA_VERSION:
            raise RoutingReceiptSchemaError(
                f"unsupported routing receipt schema: {self.schema_version!r} "
                f"(supported: {ROUTING_RECEIPT_SCHEMA_VERSION!r})"
            )
        _require_string(self.receipt_id, "receipt_id")
        if self.state not in ("in_progress", "finalized", "failed", "superseded"):
            raise RoutingReceiptError(f"invalid receipt state: {self.state!r}")
        object.__setattr__(self, "candidate_pipeline", tuple(self.candidate_pipeline))
        object.__setattr__(self, "task_profile", dict(self.task_profile))
        object.__setattr__(self, "evidence_snapshot", dict(self.evidence_snapshot))
        object.__setattr__(self, "decision", dict(self.decision))
        object.__setattr__(self, "hydration", dict(self.hydration))
        object.__setattr__(self, "execution", dict(self.execution))
        object.__setattr__(self, "verification", dict(self.verification))
        object.__setattr__(self, "final", dict(self.final))
        object.__setattr__(self, "extensions", dict(self.extensions))
        # Compute digest if absent.
        if self.decision_digest is None:
            object.__setattr__(
                self, "decision_digest", decision_digest_for(self.to_dict(include_digest=False))
            )
        else:
            _require_digest(self.decision_digest, "decision_digest")

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "story_id": self.story_id,
            "work_unit_id": self.work_unit_id,
            "attempt_id": self.attempt_id,
            "created_at": self.created_at,
            "state": self.state,
            "task_profile": dict(self.task_profile),
            "evidence_snapshot": dict(self.evidence_snapshot),
            "candidate_pipeline": [row.to_dict() for row in self.candidate_pipeline],
            "decision": dict(self.decision),
            "hydration": dict(self.hydration),
            "execution": dict(self.execution),
            "verification": dict(self.verification),
            "final": dict(self.final),
            "parent_receipt_id": self.parent_receipt_id,
            "extensions": dict(self.extensions),
        }
        if include_digest:
            payload["decision_digest"] = self.decision_digest or decision_digest_for(payload)
        return payload

    @classmethod
    def from_dict(cls, raw: Any) -> RoutingReceiptV1:
        if not isinstance(raw, Mapping):
            raise RoutingReceiptError("RoutingReceiptV1 must be an object")
        version = raw.get("schema_version")
        if version != ROUTING_RECEIPT_SCHEMA_VERSION:
            raise RoutingReceiptSchemaError(
                f"unsupported routing receipt schema: {version!r} "
                f"(supported: {ROUTING_RECEIPT_SCHEMA_VERSION!r})"
            )
        rows = tuple(CandidateRow.from_dict(item) for item in (raw.get("candidate_pipeline") or ()))
        return cls(
            receipt_id=str(raw["receipt_id"]),
            schema_version=str(version),
            story_id=raw.get("story_id"),
            work_unit_id=raw.get("work_unit_id"),
            attempt_id=raw.get("attempt_id"),
            created_at=str(raw.get("created_at") or _now_iso()),
            state=cast(ReceiptState, raw.get("state") or "in_progress"),
            task_profile=raw.get("task_profile") or {},
            evidence_snapshot=raw.get("evidence_snapshot") or {},
            candidate_pipeline=rows,
            decision=raw.get("decision") or {},
            hydration=raw.get("hydration") or {},
            execution=raw.get("execution") or {},
            verification=raw.get("verification") or {},
            final=raw.get("final") or {},
            decision_digest=raw.get("decision_digest"),
            parent_receipt_id=raw.get("parent_receipt_id"),
            extensions=raw.get("extensions") or {},
        )

    def with_updates(self, **changes: Any) -> RoutingReceiptV1:
        """Return a copy with updates; recomputes decision_digest."""
        payload = self.to_dict(include_digest=False)
        for key, value in changes.items():
            if (
                key == "candidate_pipeline"
                and value is not None
                and not isinstance(value, (list, tuple))
            ):
                raise RoutingReceiptError("candidate_pipeline must be a sequence")
            if key == "decision_digest":
                continue
            payload[key] = value
        if "candidate_pipeline" in changes and changes["candidate_pipeline"] is not None:
            payload["candidate_pipeline"] = [
                item.to_dict() if isinstance(item, CandidateRow) else item
                for item in changes["candidate_pipeline"]
            ]
        payload.pop("decision_digest", None)
        return RoutingReceiptV1.from_dict(payload)


def _identity_from_mapping(raw: Mapping[str, Any] | None) -> RouteRef | None:
    if raw is None:
        return None
    gateway = str(raw.get("gateway") or raw.get("Gateway") or "unknown")
    provider = str(raw.get("provider") or "unknown")
    model = str(raw.get("model") or raw.get("model_id") or "")
    if not model:
        return None
    return RouteRef(
        gateway=gateway or "unknown",
        provider=provider or "unknown",
        model=model,
        resource_pool=(
            str(raw["resource_pool"])
            if raw.get("resource_pool") is not None
            else (str(raw["credential_pool"]) if raw.get("credential_pool") is not None else None)
        ),
        route_id=str(raw["route_id"]) if raw.get("route_id") is not None else None,
        credential_pool=str(raw["credential_pool"])
        if raw.get("credential_pool") is not None
        else None,
    )


def _candidate_rows_from_admit(admit: Any) -> list[CandidateRow]:
    """Build bounded candidate rows from a FreeTierAdmitReceipt-like object."""
    rows: list[CandidateRow] = []
    seen: set[str] = set()

    exclusions = getattr(admit, "exclusions", ()) or ()
    for drop in exclusions:
        if hasattr(drop, "to_dict"):
            payload = drop.to_dict()
            candidate_id = str(payload.get("model") or payload.get("route_id") or "")
            reason = str(payload.get("reason") or REASON_UNMAPPED)
            detail = payload.get("detail")
        elif isinstance(drop, Mapping):
            candidate_id = str(
                drop.get("model") or drop.get("route_id") or drop.get("model_id") or ""
            )
            reason = str(drop.get("reason") or REASON_UNMAPPED)
            detail = drop.get("detail")
        else:
            continue
        if not candidate_id or candidate_id in seen:
            continue
        seen.add(candidate_id)
        rows.append(
            CandidateRow(
                candidate_id=candidate_id,
                eligible=False,
                reason_codes=(normalize_reason_code(reason),),
                detail=None if detail is None else str(detail)[:512],
                shortlisted=False,
                reached_decision_input=False,
            )
        )

    # Shortlist / candidate pool
    pool = getattr(admit, "candidate_pool", None)
    if isinstance(pool, Mapping):
        shortlist = pool.get("shortlist") or ()
        for entry in shortlist:
            if not isinstance(entry, Mapping):
                continue
            candidate_id = str(entry.get("route_id") or entry.get("metadata_id") or "")
            if not candidate_id:
                continue
            seen.add(candidate_id)
            rows.append(
                CandidateRow(
                    candidate_id=candidate_id,
                    identity=RouteRef(
                        gateway="omniroute",
                        provider=str(entry.get("provider") or "unknown"),
                        model=candidate_id,
                        route_id=candidate_id,
                    ),
                    eligible=True,
                    reason_codes=(),
                    task_fit={
                        "score": entry.get("score"),
                        "confidence": entry.get("confidence"),
                        "features": dict(entry.get("score_features") or {}),
                        "inclusion_reason": entry.get("inclusion_reason"),
                    },
                    shortlisted=True,
                    reached_decision_input=True,
                )
            )
        for drop in pool.get("hard_drops") or ():
            if not isinstance(drop, Mapping):
                continue
            candidate_id = str(drop.get("route_id") or "")
            if not candidate_id or candidate_id in seen:
                continue
            seen.add(candidate_id)
            rows.append(
                CandidateRow(
                    candidate_id=candidate_id,
                    eligible=False,
                    reason_codes=(
                        normalize_reason_code(str(drop.get("reason") or REASON_UNMAPPED)),
                    ),
                    detail=None if drop.get("detail") is None else str(drop.get("detail"))[:512],
                )
            )
        probes = {
            str(p.get("route_id")): p for p in (pool.get("probes") or ()) if isinstance(p, Mapping)
        }
        if probes:
            updated: list[CandidateRow] = []
            for row in rows:
                probe = probes.get(row.candidate_id)
                if probe is None:
                    updated.append(row)
                    continue
                codes = list(row.reason_codes)
                if probe.get("passed") is False and REASON_PROBE_FAILED not in codes:
                    codes.append(REASON_PROBE_FAILED)
                updated.append(
                    CandidateRow(
                        candidate_id=row.candidate_id,
                        identity=row.identity,
                        eligible=row.eligible if probe.get("passed") is not False else False,
                        reason_codes=tuple(codes),
                        detail=row.detail,
                        task_fit=row.task_fit,
                        quality_evidence={
                            "source": "probe",
                            "version": str(probe.get("level") or "unknown"),
                            "freshness": "attempt",
                            "passed": probe.get("passed"),
                        },
                        capacity_freshness=row.capacity_freshness,
                        context_plan_digest=row.context_plan_digest,
                        estimated_input_tokens=row.estimated_input_tokens,
                        confirm={"probe": dict(probe)},
                        passport=row.passport,
                        reached_decision_input=row.reached_decision_input,
                        shortlisted=row.shortlisted,
                    )
                )
            rows = updated

    # Attach candidate ContextPlan digests / estimates (candidate ContextPlan digests).
    plans = getattr(admit, "context_plans", ()) or ()
    plan_by_candidate: dict[str, Any] = {}
    for plan in plans:
        if hasattr(plan, "candidate_id"):
            plan_by_candidate[str(plan.candidate_id)] = plan
        elif isinstance(plan, Mapping) and plan.get("candidate_id"):
            plan_by_candidate[str(plan["candidate_id"])] = plan
    if plan_by_candidate:
        updated = []
        for row in rows:
            plan = plan_by_candidate.get(row.candidate_id)
            if plan is None:
                updated.append(row)
                continue
            if hasattr(plan, "digest"):
                digest = plan.digest
                estimated = getattr(plan, "estimated_input_tokens", None)
            else:
                digest = plan.get("digest") or _digest(dict(plan))
                estimated = plan.get("estimated_input_tokens")
            token_ev = (
                None
                if estimated is None
                else EvidenceValue.estimated(estimated, source="context_plan", unit="tokens")
            )
            updated.append(
                CandidateRow(
                    candidate_id=row.candidate_id,
                    identity=row.identity,
                    eligible=row.eligible,
                    reason_codes=row.reason_codes,
                    detail=row.detail,
                    task_fit=row.task_fit,
                    quality_evidence=row.quality_evidence,
                    capacity_freshness=row.capacity_freshness,
                    context_plan_digest=digest,
                    estimated_input_tokens=token_ev,
                    confirm=row.confirm,
                    passport=row.passport,
                    reached_decision_input=row.reached_decision_input,
                    shortlisted=row.shortlisted,
                )
            )
        rows = updated

    # Passport / confirm arrays (passport confirm arrays) — attribute to chosen when present.
    passport = getattr(admit, "passport", ()) or ()
    confirm = getattr(admit, "confirm", ()) or ()
    chosen = getattr(admit, "chosen", None)
    if chosen and (passport or confirm):
        updated = []
        for row in rows:
            if row.candidate_id != chosen:
                updated.append(row)
                continue
            updated.append(
                CandidateRow(
                    candidate_id=row.candidate_id,
                    identity=row.identity,
                    eligible=row.eligible,
                    reason_codes=row.reason_codes
                    + ((REASON_SELECTED,) if REASON_SELECTED not in row.reason_codes else ()),
                    detail=row.detail,
                    task_fit=row.task_fit,
                    quality_evidence=row.quality_evidence,
                    capacity_freshness=row.capacity_freshness,
                    context_plan_digest=row.context_plan_digest,
                    estimated_input_tokens=row.estimated_input_tokens,
                    confirm={
                        "items": [
                            item.to_dict()
                            if hasattr(item, "to_dict")
                            else dict(item)
                            if isinstance(item, Mapping)
                            else item
                            for item in confirm
                        ]
                    },
                    passport={
                        "items": [
                            item.to_dict()
                            if hasattr(item, "to_dict")
                            else dict(item)
                            if isinstance(item, Mapping)
                            else item
                            for item in passport
                        ]
                    },
                    reached_decision_input=True,
                    shortlisted=True,
                )
            )
        rows = updated

    # Ensure admitted ids appear even if pool was empty.
    for model_id in getattr(admit, "admitted", ()) or ():
        if model_id in seen:
            continue
        seen.add(model_id)
        rows.append(
            CandidateRow(
                candidate_id=str(model_id),
                eligible=True,
                reason_codes=(),
                shortlisted=False,
                reached_decision_input=str(model_id) == str(chosen) if chosen else False,
            )
        )
    return rows


def build_routing_receipt(
    *,
    admit: Any | None = None,
    execution_path_decision: Any | None = None,
    context_plan: Any | None = None,
    context_pack: Any | None = None,
    context_receipt: Any | None = None,
    requested_alias: str | None = None,
    selected_identity: Mapping[str, Any] | RouteRef | None = None,
    observed_identity: Mapping[str, Any] | RouteRef | None = None,
    execution_status: str | None = None,
    observed_usage: Mapping[str, Any] | None = None,
    verification: Mapping[str, Any] | None = None,
    story_id: str | None = None,
    work_unit_id: str | None = None,
    attempt_id: str | None = None,
    receipt_id: str | None = None,
    parent_receipt_id: str | None = None,
    state: ReceiptState = "in_progress",
    outcome: str | None = None,
    extensions: Mapping[str, Any] | None = None,
) -> RoutingReceiptV1:
    """Compose a RoutingReceiptV1 from existing decision artifacts.

    Accepts FreeTierAdmitReceipt / ExecutionPathDecision / ContextPlan /
    ContextPack / ContextReceipt-like objects without taking routing authority.
    """

    rid = receipt_id or f"rr-{uuid4().hex}"
    task_profile: dict[str, Any] = {}
    evidence_snapshot: dict[str, Any] = {}
    rows: list[CandidateRow] = []
    decision: dict[str, Any] = {}
    hydration: dict[str, Any] = {}
    execution: dict[str, Any] = {}
    verification_section: dict[str, Any] = {}
    final: dict[str, Any] = {"state": state}

    if admit is not None:
        task_profile = {
            "digest": getattr(admit, "task_profile_digest", None),
            "task_class": getattr(admit, "task_class", None),
            "class_reasons": list(getattr(admit, "class_reasons", ()) or ()),
            "required_capabilities": list(getattr(admit, "requirements", ()) or ()),
            "spend_policy": getattr(admit, "spend_policy", None),
            "selected_because": getattr(admit, "selected_because", None),
        }
        evidence_snapshot = {
            "pack_digest": getattr(admit, "pack_digest", None),
            "prompt_digest": getattr(admit, "prompt_digest", None),
            "pack_state": getattr(admit, "pack_state", None),
            "capability_coverage": getattr(admit, "capability_coverage", None),
            "omissions": [
                item.to_dict() if hasattr(item, "to_dict") else item
                for item in (getattr(admit, "omissions", ()) or ())
            ],
            "unknown_fields": [],
        }
        rows = _candidate_rows_from_admit(admit)
        if getattr(admit, "chosen", None):
            decision["chosen_from_admit"] = admit.chosen
        if getattr(admit, "candidate_pool", None):
            pool = admit.candidate_pool
            evidence_snapshot["candidate_pool_digest"] = (
                pool.get("evidence_digest") if isinstance(pool, Mapping) else None
            )
            evidence_snapshot["shortlist_digest"] = (
                pool.get("shortlist_digest") if isinstance(pool, Mapping) else None
            )

    if execution_path_decision is not None:
        ep = execution_path_decision
        ep_dict = ep.to_dict() if hasattr(ep, "to_dict") else dict(ep)
        selected_route = None
        if hasattr(ep, "selected_route") and ep.selected_route is not None:
            selected_route = (
                ep.selected_route.to_dict()
                if hasattr(ep.selected_route, "to_dict")
                else dict(ep.selected_route)
            )
        elif isinstance(ep_dict.get("selected_route"), Mapping):
            selected_route = dict(ep_dict["selected_route"])
        decision.update(
            {
                "execution_path_digest": ep_dict.get("decision_digest"),
                "selected_strategy": ep_dict.get("selected_strategy"),
                "selected_candidate_id": ep_dict.get("selected_candidate_id"),
                "selected_route": selected_route,
                "strategy_selection_reason": ep_dict.get("strategy_selection_reason"),
                "why_selected": ep_dict.get("why_selected"),
                "expected_cost": ep_dict.get("expected_cost"),
                "rejected": ep_dict.get("rejected"),
                "policy_version": ep_dict.get("schema_version"),
                "trajectory_id": ep_dict.get("trajectory_id"),
                "task_slice_id": ep_dict.get("task_slice_id"),
            }
        )
        # Mark matching candidate as reached + selected.
        selected_id = ep_dict.get("selected_candidate_id")
        if selected_id:
            marked: list[CandidateRow] = []
            found = False
            for row in rows:
                if row.candidate_id == selected_id:
                    found = True
                    codes = list(row.reason_codes)
                    if REASON_SELECTED not in codes:
                        codes.append(REASON_SELECTED)
                    marked.append(
                        CandidateRow(
                            candidate_id=row.candidate_id,
                            identity=row.identity or _identity_from_mapping(selected_route),
                            eligible=True,
                            reason_codes=tuple(codes),
                            detail=row.detail,
                            task_fit=row.task_fit,
                            quality_evidence=row.quality_evidence,
                            capacity_freshness=row.capacity_freshness,
                            context_plan_digest=row.context_plan_digest,
                            estimated_input_tokens=row.estimated_input_tokens,
                            confirm=row.confirm,
                            passport=row.passport,
                            reached_decision_input=True,
                            shortlisted=True,
                        )
                    )
                else:
                    marked.append(row)
            if not found and selected_route is not None:
                marked.append(
                    CandidateRow(
                        candidate_id=str(selected_id),
                        identity=_identity_from_mapping(selected_route),
                        eligible=True,
                        reason_codes=(REASON_SELECTED,),
                        reached_decision_input=True,
                        shortlisted=True,
                    )
                )
            rows = marked

    # Hydration: prefer explicit final plan/pack/receipt.
    def _plan_digest(obj: Any) -> str | None:
        if obj is None:
            return None
        if hasattr(obj, "digest"):
            return str(obj.digest)
        if isinstance(obj, Mapping):
            return str(obj.get("digest") or obj.get("plan_digest") or "") or None
        return None

    def _pack_digest(obj: Any) -> str | None:
        if obj is None:
            return None
        if hasattr(obj, "digest"):
            return str(obj.digest)
        if isinstance(obj, Mapping):
            return str(obj.get("digest") or obj.get("pack_digest") or "") or None
        return None

    hydration = {
        "context_plan_digest": _plan_digest(context_plan)
        or (getattr(context_receipt, "plan_digest", None) if context_receipt is not None else None),
        "context_pack_digest": _pack_digest(context_pack)
        or (getattr(context_receipt, "pack_digest", None) if context_receipt is not None else None),
        "prompt_digest": getattr(admit, "prompt_digest", None) if admit is not None else None,
        "omissions": evidence_snapshot.get("omissions") if evidence_snapshot else [],
    }
    if context_plan is not None and hasattr(context_plan, "to_dict"):
        plan_dict = context_plan.to_dict()
        hydration["budget"] = {
            "token_budget": plan_dict.get("token_budget"),
            "output_token_reserve": plan_dict.get("output_token_reserve"),
            "tool_token_reserve": plan_dict.get("tool_token_reserve"),
            "estimated_input_tokens": (
                EvidenceValue.estimated(
                    plan_dict["estimated_input_tokens"], source="context_plan", unit="tokens"
                ).to_dict()
                if plan_dict.get("estimated_input_tokens") is not None
                else EvidenceValue.unknown(source="context_plan").to_dict()
            ),
        }
    if context_receipt is not None and hasattr(context_receipt, "to_dict"):
        hydration["context_receipt_id"] = getattr(context_receipt, "receipt_id", None)
        hydration["unresolved_uncertainties"] = list(
            getattr(context_receipt, "unresolved_uncertainties", ()) or ()
        )

    # Execution identities — keep requested / selected / observed distinct.
    def _as_route(value: Mapping[str, Any] | RouteRef | None) -> dict[str, Any] | None:
        if value is None:
            return None
        if isinstance(value, RouteRef):
            return value.to_dict()
        ref = _identity_from_mapping(value)
        return None if ref is None else ref.to_dict()

    selected_dict = _as_route(selected_identity)
    if selected_dict is None and decision.get("selected_route"):
        selected_dict = _as_route(cast(Mapping[str, Any], decision["selected_route"]))
    observed_dict = _as_route(observed_identity)
    execution = {
        "requested_alias": requested_alias,
        "selected_identity": selected_dict,
        "observed_identity": observed_dict,
        "status": execution_status,
        "usage": {},
    }
    if observed_usage:
        usage: dict[str, Any] = {}
        for key, _kind in (
            ("input_tokens", "observed"),
            ("output_tokens", "observed"),
            ("cost_usd", "observed"),
            ("latency_ms", "observed"),
        ):
            if key in observed_usage and observed_usage[key] is not None:
                usage[f"observed_{key}"] = EvidenceValue.observed(
                    observed_usage[key], source="provider", unit=key
                ).to_dict()
            else:
                usage[f"observed_{key}"] = EvidenceValue.unknown(source="provider").to_dict()
        # Estimated counterparts stay separate when provided.
        for key in ("estimated_input_tokens", "estimated_complete_cost"):
            if key in observed_usage and observed_usage[key] is not None:
                usage[key] = EvidenceValue.estimated(
                    observed_usage[key], source="decision", unit=key
                ).to_dict()
        execution["usage"] = usage

    mismatch = False
    if selected_dict and observed_dict:
        for field_name in ("gateway", "provider", "model", "resource_pool"):
            left = selected_dict.get(field_name)
            right = observed_dict.get(field_name)
            if left is not None and right is not None and left != right:
                mismatch = True
                break
    if mismatch:
        execution["identity_mismatch"] = True
        execution["failure_reason"] = REASON_IDENTITY_MISMATCH
        if state == "in_progress":
            state = "failed"
            final["state"] = "failed"
        final["reason_codes"] = [*(final.get("reason_codes") or []), REASON_IDENTITY_MISMATCH]

    if verification:
        verification_section = dict(verification)
    elif admit is not None and getattr(admit, "verification", None):
        verification_section = dict(admit.verification)

    if outcome is not None:
        final["outcome"] = outcome
    final["state"] = state

    return RoutingReceiptV1(
        receipt_id=rid,
        story_id=story_id,
        work_unit_id=work_unit_id,
        attempt_id=attempt_id,
        state=state,
        task_profile=task_profile,
        evidence_snapshot=evidence_snapshot,
        candidate_pipeline=tuple(rows),
        decision=decision,
        hydration=hydration,
        execution=execution,
        verification=verification_section,
        final=final,
        parent_receipt_id=parent_receipt_id,
        extensions=dict(extensions or {}),
    )


def default_receipt_store(repo: Path | None = None) -> ReceiptStore:
    """Default-on store: repo ``.verdict/receipts.db`` or ``~/.verdict/receipts.db``."""
    if repo is not None:
        path = Path(repo).expanduser().resolve() / ".verdict" / "receipts.db"
    else:
        path = Path.home() / ".verdict" / "receipts.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return ReceiptStore(path, strict_scope=True)


def attempt_scope(*, story_id: str | None, work_unit_id: str | None, attempt_id: str | None) -> str:
    parts = [p for p in (story_id or "story", work_unit_id or "unit", attempt_id or "attempt") if p]
    scope = "/".join(parts)
    return scope[:512]


def routing_receipt_allowlist(payload: Mapping[str, Any] | None = None) -> set[str]:
    """Field paths that must survive redaction (measured token/cost evidence)."""
    allowed = {
        "estimated_input_tokens",
        "observed_input_tokens",
        "observed_output_tokens",
        "observed_cost_usd",
        "observed_latency_ms",
        "estimated_complete_cost",
        "input_tokens",
        "output_tokens",
        "token_budget",
        "output_token_reserve",
        "tool_token_reserve",
        "candidate_context_window",
        "execution.usage",
        "hydration.budget",
        "hydration.budget.estimated_input_tokens",
        "execution.usage.observed_input_tokens",
        "execution.usage.observed_output_tokens",
        "execution.usage.observed_cost_usd",
        "execution.usage.observed_latency_ms",
        "execution.usage.estimated_input_tokens",
        "execution.usage.estimated_complete_cost",
        "kind",
        "value",
        "source",
        "freshness",
        "unit",
    }
    if payload is not None:
        for index, _row in enumerate(payload.get("candidate_pipeline") or ()):
            allowed.add(f"candidate_pipeline[{index}].estimated_input_tokens")
            allowed.add(f"candidate_pipeline[{index}].estimated_input_tokens.value")
            allowed.add(f"candidate_pipeline[{index}].context_plan_digest")
    return allowed


def persist_routing_receipt(
    store: ReceiptStore,
    receipt: RoutingReceiptV1,
    *,
    scope: str | None = None,
    allowlist: Iterable[str] = (),
) -> ReceiptRecord:
    """Create the root decision record for an attempt (idempotent).

    The store is append-only. Later state transitions must use
    :func:`append_routing_receipt_event` / :func:`finalize_routing_receipt`
    rather than rewriting this root payload.
    """
    scope_value = scope or attempt_scope(
        story_id=receipt.story_id, work_unit_id=receipt.work_unit_id, attempt_id=receipt.attempt_id
    )
    raw_payload = receipt.to_dict()
    merged_allowlist = set(allowlist) | routing_receipt_allowlist(raw_payload)
    payload = redact_sensitive_dict(raw_payload, allowlist=merged_allowlist)
    idem = str(receipt.attempt_id or receipt.receipt_id)
    # Create-once root: if this attempt already has a decision root, return it.
    matches = store.query_receipts(receipt_type="decision", scope=scope_value, limit=100)
    for item in matches:
        if item.parent_receipt_id:
            continue
        if item.idempotency_key == idem or item.receipt_id == receipt.receipt_id:
            return item
    try:
        return store.put_receipt(
            "decision",
            scope_value,
            payload,
            receipt_id=receipt.receipt_id,
            parent_receipt_id=receipt.parent_receipt_id,
            idempotency_key=idem,
            event_type="routing_receipt",
            event_id=f"routing-root:{idem}",
            provenance={
                "source": "verdict_routing_receipt",
                "version": ROUTING_RECEIPT_SCHEMA_VERSION,
            },
            allowlist=merged_allowlist,
        )
    except ReceiptConflictError as exc:
        matches = store.query_receipts(receipt_type="decision", scope=scope_value, limit=100)
        for item in matches:
            if item.parent_receipt_id:
                continue
            if item.idempotency_key == idem or item.receipt_id == receipt.receipt_id:
                return item
        raise RoutingReceiptPersistError(str(exc)) from exc
    except Exception as exc:
        raise RoutingReceiptPersistError(str(exc)) from exc


def append_routing_receipt_event(
    store: ReceiptStore,
    receipt: RoutingReceiptV1,
    *,
    scope: str | None = None,
    event_type: str = "routing_receipt_update",
    event_id: str | None = None,
    terminal_outcome: str | None = None,
) -> ReceiptRecord:
    """Append a lifecycle snapshot for an existing routing receipt root."""
    scope_value = scope or attempt_scope(
        story_id=receipt.story_id, work_unit_id=receipt.work_unit_id, attempt_id=receipt.attempt_id
    )
    raw_payload = receipt.to_dict()
    merged_allowlist = routing_receipt_allowlist(raw_payload)
    payload = redact_sensitive_dict(raw_payload, allowlist=merged_allowlist)
    eid = (
        event_id
        or f"routing-{event_type}:{receipt.attempt_id or receipt.receipt_id}:{receipt.state}"
    )
    try:
        return store.append_event(
            receipt.receipt_id,
            scope=scope_value,
            payload=payload,
            event_id=eid,
            event_type=event_type,
            terminal_outcome=terminal_outcome,
            allowlist=merged_allowlist,
        )
    except ReceiptConflictError:
        raise
    except Exception as exc:
        raise RoutingReceiptPersistError(str(exc)) from exc


def _latest_routing_payload(
    store: ReceiptStore, root: ReceiptRecord, *, scope: str
) -> dict[str, Any]:
    """Return the newest routing-receipt payload in the root's event chain."""
    events = [
        item
        for item in store.query_receipts(scope=scope, limit=10_000)
        if item.parent_receipt_id == root.receipt_id
        and item.event_type
        in {"routing_receipt_update", "routing_receipt_finalize", "routing_receipt_supersede"}
    ]
    if not events:
        return dict(root.payload)
    newest = max(events, key=lambda item: item.sequence)
    return dict(newest.payload)


def load_routing_receipt(
    store: ReceiptStore,
    *,
    receipt_id: str | None = None,
    scope: str | None = None,
    attempt_id: str | None = None,
) -> RoutingReceiptV1 | None:
    """Load a routing receipt by id or attempt idempotency key (latest snapshot)."""
    record: ReceiptRecord | None = None
    if receipt_id is not None:
        record = store.get_receipt(receipt_id, scope=scope)
    elif attempt_id is not None and scope is not None:
        matches = store.query_receipts(receipt_type="decision", scope=scope, limit=100)
        for item in matches:
            if item.parent_receipt_id:
                continue
            if item.idempotency_key == attempt_id or item.payload.get("attempt_id") == attempt_id:
                record = item
                break
    if record is None:
        return None
    scope_value = scope or record.scope
    payload = _latest_routing_payload(store, record, scope=scope_value)
    if payload.get("schema_version") != ROUTING_RECEIPT_SCHEMA_VERSION:
        if "candidate_pipeline" not in payload and "task_profile" not in payload:
            return None
        raise RoutingReceiptSchemaError(
            f"unsupported routing receipt schema: {payload.get('schema_version')!r}"
        )
    return RoutingReceiptV1.from_dict(payload)


def finalize_routing_receipt(
    store: ReceiptStore,
    receipt: RoutingReceiptV1,
    *,
    scope: str | None = None,
    outcome: str = "success",
    verification: Mapping[str, Any] | None = None,
    observed_identity: Mapping[str, Any] | RouteRef | None = None,
    observed_usage: Mapping[str, Any] | None = None,
    execution_status: str = "completed",
) -> RoutingReceiptV1:
    """Idempotently finalize an in-progress receipt via an append-only terminal event."""
    scope_value = scope or attempt_scope(
        story_id=receipt.story_id, work_unit_id=receipt.work_unit_id, attempt_id=receipt.attempt_id
    )
    # Ensure root exists.
    persist_routing_receipt(store, receipt, scope=scope_value)
    updates: dict[str, Any] = {
        "state": "finalized" if outcome == "success" else "failed",
        "final": {
            **dict(receipt.final),
            "outcome": outcome,
            "state": "finalized" if outcome == "success" else "failed",
        },
    }
    execution = dict(receipt.execution)
    execution["status"] = execution_status
    if observed_identity is not None:
        if isinstance(observed_identity, RouteRef):
            execution["observed_identity"] = observed_identity.to_dict()
        else:
            ref = _identity_from_mapping(observed_identity)
            execution["observed_identity"] = None if ref is None else ref.to_dict()
    if observed_usage is not None:
        usage = dict(execution.get("usage") or {})
        for key in ("input_tokens", "output_tokens", "cost_usd", "latency_ms"):
            if key in observed_usage and observed_usage[key] is not None:
                usage[f"observed_{key}"] = EvidenceValue.observed(
                    observed_usage[key], source="provider", unit=key
                ).to_dict()
        execution["usage"] = usage
    updates["execution"] = execution
    if verification is not None:
        updates["verification"] = dict(verification)
    finalized = receipt.with_updates(**updates)
    append_routing_receipt_event(
        store,
        finalized,
        scope=scope_value,
        event_type="routing_receipt_finalize",
        event_id=f"routing-finalize:{receipt.attempt_id or receipt.receipt_id}",
        terminal_outcome=finalized.state,
    )
    return finalized


def link_recovery_attempt(
    store: ReceiptStore,
    previous: RoutingReceiptV1,
    *,
    new_attempt_id: str,
    scope: str | None = None,
    **builder_kwargs: Any,
) -> RoutingReceiptV1:
    """Create a new attempt receipt linked to a prior failed/partial attempt.

    ReceiptStore forbids lifecycle appends after terminalization, so a prior
    terminal attempt is left unchanged. The new attempt fences it by reference
    (``parent_receipt_id`` / ``final.recovery_of``) under a new attempt scope.
    """
    scope_value = scope or attempt_scope(
        story_id=previous.story_id,
        work_unit_id=previous.work_unit_id,
        attempt_id=previous.attempt_id,
    )
    persist_routing_receipt(store, previous, scope=scope_value)
    nxt = build_routing_receipt(
        attempt_id=new_attempt_id,
        parent_receipt_id=previous.receipt_id,
        story_id=previous.story_id,
        work_unit_id=previous.work_unit_id,
        state="in_progress",
        **builder_kwargs,
    )
    nxt = nxt.with_updates(
        final={
            **dict(nxt.final),
            "recovery_of": previous.receipt_id,
            "fenced_prior_attempt": previous.attempt_id,
            "state": "in_progress",
        }
    )
    new_scope = attempt_scope(
        story_id=nxt.story_id, work_unit_id=nxt.work_unit_id, attempt_id=nxt.attempt_id
    )
    persist_routing_receipt(store, nxt, scope=new_scope)
    return nxt


def human_summary(receipt: RoutingReceiptV1) -> str:
    """Operator-facing one-screen summary."""
    selected = (receipt.decision or {}).get("selected_route") or (receipt.execution or {}).get(
        "selected_identity"
    )
    model = "unknown"
    if isinstance(selected, Mapping):
        model = str(selected.get("model") or "unknown")
    excluded = sum(1 for row in receipt.candidate_pipeline if row.eligible is False)
    shortlisted = sum(1 for row in receipt.candidate_pipeline if row.shortlisted)
    return (
        f"routing-receipt {receipt.receipt_id} state={receipt.state} "
        f"model={model} shortlisted={shortlisted} excluded={excluded} "
        f"digest={(receipt.decision_digest or '')[:18]}"
    )


__all__ = [
    "REASON_CODES",
    "ROUTING_RECEIPT_SCHEMA_VERSION",
    "CandidateRow",
    "EvidenceValue",
    "RouteRef",
    "RoutingReceiptError",
    "RoutingReceiptPersistError",
    "RoutingReceiptSchemaError",
    "RoutingReceiptV1",
    "append_routing_receipt_event",
    "attempt_scope",
    "build_routing_receipt",
    "canonical_routing_payload",
    "decision_digest_for",
    "default_receipt_store",
    "finalize_routing_receipt",
    "human_summary",
    "link_recovery_attempt",
    "load_routing_receipt",
    "normalize_reason_code",
    "persist_routing_receipt",
]
