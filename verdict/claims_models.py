"""Append-only, privacy-safe claims lifecycle and hydration boundary.

Claims are stored as hashes plus provenance.  Claim text may be supplied to
``Claim.create`` for an in-process decision, but it is deliberately omitted
from every serialized record and receipt.  Lifecycle changes append transition
records; no claim or evidence is deleted.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, cast

from verdict.context_pack import ContextUnit
from verdict.proof_receipts import EvidenceReference, ProofReceiptError, SourceReference, claim_hash

CLAIMS_LEDGER_SCHEMA_VERSION = "1"
ClaimStatus = Literal["observed", "active", "verified", "superseded", "disputed"]
FreshnessStatus = Literal["fresh", "expired", "future_skew", "policy_missing"]

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SENSITIVE = re.compile(
    r"(?i)(api[_-]?key|authorization|bearer|password|secret|token|private[_-]?key|"
    r"raw[_-]?prompt|raw[_-]?completion|messages|tool[_-]?arguments|email|phone|address)"
)
_STATUSES = frozenset({"observed", "active", "verified", "superseded", "disputed"})
_TRANSITIONS: dict[str, frozenset[str]] = {
    "observed": frozenset({"active", "verified", "superseded", "disputed"}),
    "active": frozenset({"verified", "superseded", "disputed"}),
    "verified": frozenset({"active", "superseded", "disputed"}),
    "disputed": frozenset({"active", "verified"}),
    "superseded": frozenset(),
}


class ClaimsLedgerError(ValueError):
    """Raised when a claim or lifecycle operation violates the contract."""


class ClaimTransitionError(ClaimsLedgerError):
    """Raised when a status transition is not allowed."""


def _identifier(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ClaimsLedgerError(f"{field_name} must be a bounded identifier")
    if _SENSITIVE.search(value):
        raise ClaimsLedgerError(f"{field_name} contains sensitive content")
    return value


def _digest(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ClaimsLedgerError(f"{field_name} must be a sha256 digest")
    return value


def _timestamp(value: datetime | str, field_name: str) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ClaimsLedgerError(f"{field_name} must include a timezone")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if not isinstance(value, str):
        raise ClaimsLedgerError(f"{field_name} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ClaimsLedgerError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ClaimsLedgerError(f"{field_name} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _as_datetime(value: datetime | str, field_name: str) -> datetime:
    return datetime.fromisoformat(_timestamp(value, field_name).replace("Z", "+00:00"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ref_tuple(
    value: Sequence[SourceReference] | Sequence[EvidenceReference],
    expected: type[SourceReference] | type[EvidenceReference],
    field_name: str,
) -> tuple[SourceReference, ...] | tuple[EvidenceReference, ...]:
    refs = tuple(value)
    if any(not isinstance(item, expected) for item in refs):
        raise ClaimsLedgerError(f"{field_name} must contain {expected.__name__} values")
    return cast(tuple[SourceReference, ...] | tuple[EvidenceReference, ...], refs)


@dataclass(frozen=True)
class Claim:
    """A privacy-safe claim version.

    ``text`` is transient process memory only and is excluded from equality,
    repr, and serialization.  Persisted identity is ``claim_id`` plus
    ``text_hash``.
    """

    claim_id: str
    subject: str
    text_hash: str
    status: ClaimStatus
    observed_at: datetime | str
    verified_at: datetime | str | None = None
    confidence: float = 0.0
    source_refs: tuple[SourceReference, ...] = ()
    evidence_refs: tuple[EvidenceReference, ...] = ()
    supersedes: str | None = None
    source_type: str = "unknown"
    text: str | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        _identifier(self.claim_id, "claim_id")
        _identifier(self.subject, "subject")
        _digest(self.text_hash, "text_hash")
        if self.status not in _STATUSES:
            raise ClaimsLedgerError("claim status is invalid")
        observed = _timestamp(self.observed_at, "observed_at")
        object.__setattr__(self, "observed_at", observed)
        if self.verified_at is not None:
            verified = _timestamp(self.verified_at, "verified_at")
            if _as_datetime(verified, "verified_at") < _as_datetime(observed, "observed_at"):
                raise ClaimsLedgerError("verified_at must not precede observed_at")
            object.__setattr__(self, "verified_at", verified)
        if isinstance(self.confidence, bool) or not isinstance(self.confidence, (int, float)):
            raise ClaimsLedgerError("confidence must be a number")
        if not math.isfinite(float(self.confidence)) or not 0.0 <= self.confidence <= 1.0:
            raise ClaimsLedgerError("confidence must be between 0 and 1")
        _identifier(self.source_type, "source_type")
        if self.supersedes is not None:
            _identifier(self.supersedes, "supersedes")
        object.__setattr__(
            self, "source_refs", _ref_tuple(self.source_refs, SourceReference, "source_refs")
        )
        object.__setattr__(
            self,
            "evidence_refs",
            _ref_tuple(self.evidence_refs, EvidenceReference, "evidence_refs"),
        )
        if not self.source_refs and not self.evidence_refs:
            raise ClaimsLedgerError("claim must reference a source or evidence")
        if self.text is not None:
            if not isinstance(self.text, str) or not self.text.strip():
                raise ClaimsLedgerError("text must be a non-empty string")
            if claim_hash(self.text) != self.text_hash:
                raise ClaimsLedgerError("text_hash does not match text")

    @classmethod
    def create(
        cls,
        *,
        subject: str,
        text: str,
        observed_at: datetime | str,
        source_refs: tuple[SourceReference, ...] = (),
        evidence_refs: tuple[EvidenceReference, ...] = (),
        claim_id: str | None = None,
        status: ClaimStatus = "active",
        verified_at: datetime | str | None = None,
        confidence: float = 0.0,
        supersedes: str | None = None,
        source_type: str = "unknown",
    ) -> Claim:
        """Create a claim from transient text without persisting that text."""
        if not isinstance(text, str) or not text.strip():
            raise ClaimsLedgerError("text must be a non-empty string")
        text_digest = claim_hash(text)
        stable_id = claim_id or (
            "claim-"
            + hashlib.sha256(
                json.dumps([subject, text_digest], separators=(",", ":"), sort_keys=True).encode()
            ).hexdigest()[:32]
        )
        return cls(
            claim_id=stable_id,
            subject=subject,
            text_hash=text_digest,
            status=status,
            observed_at=observed_at,
            verified_at=verified_at,
            confidence=confidence,
            source_refs=source_refs,
            evidence_refs=evidence_refs,
            supersedes=supersedes,
            source_type=source_type,
            text=text,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the persisted representation; claim text is never included."""
        value: dict[str, Any] = {
            "schema_version": CLAIMS_LEDGER_SCHEMA_VERSION,
            "claim_id": self.claim_id,
            "subject": self.subject,
            "text_hash": self.text_hash,
            "status": self.status,
            "observed_at": self.observed_at,
            "verified_at": self.verified_at,
            "confidence": self.confidence,
            "source_type": self.source_type,
            "source_refs": [item.to_dict() for item in self.source_refs],
            "evidence_refs": [item.to_dict() for item in self.evidence_refs],
        }
        if self.supersedes is not None:
            value["supersedes"] = self.supersedes
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Claim:
        if not isinstance(value, Mapping):
            raise ClaimsLedgerError("claim must be an object")
        required = {
            "schema_version",
            "claim_id",
            "subject",
            "text_hash",
            "status",
            "observed_at",
            "verified_at",
            "confidence",
            "source_type",
            "source_refs",
            "evidence_refs",
        }
        if set(value) - required - {"supersedes"}:
            raise ClaimsLedgerError("claim contains unknown fields")
        if set(value) & required != required:
            raise ClaimsLedgerError("claim is missing required fields")
        if value["schema_version"] != CLAIMS_LEDGER_SCHEMA_VERSION:
            raise ClaimsLedgerError("unsupported claims ledger schema version")
        sources = value["source_refs"]
        evidence = value["evidence_refs"]
        if not isinstance(sources, list) or not isinstance(evidence, list):
            raise ClaimsLedgerError("claim references must be arrays")
        try:
            return cls(
                claim_id=value["claim_id"],
                subject=value["subject"],
                text_hash=value["text_hash"],
                status=value["status"],
                observed_at=value["observed_at"],
                verified_at=value["verified_at"],
                confidence=value["confidence"],
                source_type=value["source_type"],
                source_refs=tuple(SourceReference(**item) for item in sources),
                evidence_refs=tuple(EvidenceReference(**item) for item in evidence),
                supersedes=value.get("supersedes"),
            )
        except (KeyError, TypeError, ProofReceiptError) as exc:
            raise ClaimsLedgerError(f"malformed claim: {exc}") from exc

    def with_status(self, status: ClaimStatus) -> Claim:
        return replace(self, status=status)


@dataclass(frozen=True)
class FreshnessPolicy:
    """Per-source expiry policy.

    Expiry is exact: ``now >= observed_at + ttl_seconds`` is expired.
    Observations up to ``clock_skew_seconds`` in the future are accepted;
    farther-future observations are rejected as ``future_skew``.
    """

    source_type: str
    ttl_seconds: float | None
    clock_skew_seconds: float = 0.0

    def __post_init__(self) -> None:
        _identifier(self.source_type, "source_type")
        if self.ttl_seconds is not None and (
            isinstance(self.ttl_seconds, bool)
            or not isinstance(self.ttl_seconds, (int, float))
            or not math.isfinite(float(self.ttl_seconds))
            or self.ttl_seconds < 0
        ):
            raise ClaimsLedgerError("ttl_seconds must be non-negative or None")
        if (
            isinstance(self.clock_skew_seconds, bool)
            or not isinstance(self.clock_skew_seconds, (int, float))
            or not math.isfinite(float(self.clock_skew_seconds))
            or self.clock_skew_seconds < 0
        ):
            raise ClaimsLedgerError("clock_skew_seconds must be non-negative")

    def expiry(self, observed_at: datetime | str) -> datetime | None:
        observed = _as_datetime(observed_at, "observed_at")
        return (
            None
            if self.ttl_seconds is None
            else observed + timedelta(seconds=float(self.ttl_seconds))
        )

    def evaluate(
        self, observed_at: datetime | str, *, now: datetime | str | None = None
    ) -> FreshnessStatus:
        observed = _as_datetime(observed_at, "observed_at")
        current = _now() if now is None else _as_datetime(now, "now")
        if current < observed - timedelta(seconds=float(self.clock_skew_seconds)):
            return "future_skew"
        expires = self.expiry(observed)
        if expires is None:
            return "policy_missing"
        return "expired" if current >= expires else "fresh"


@dataclass(frozen=True)
class FreshnessResult:
    source_type: str
    status: FreshnessStatus
    observed_at: str
    expires_at: str | None
    clock_skew_seconds: float

    @property
    def fresh(self) -> bool:
        return self.status == "fresh"


@dataclass(frozen=True)
class ClaimTransition:
    transition_id: str
    claim_id: str
    from_status: ClaimStatus
    to_status: ClaimStatus
    occurred_at: str
    reason: str
    superseded_by: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.transition_id, "transition_id")
        _identifier(self.claim_id, "claim_id")
        if self.from_status not in _STATUSES or self.to_status not in _STATUSES:
            raise ClaimsLedgerError("transition status is invalid")
        object.__setattr__(self, "occurred_at", _timestamp(self.occurred_at, "occurred_at"))
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ClaimsLedgerError("transition reason must be non-empty")
        if self.superseded_by is not None:
            _identifier(self.superseded_by, "superseded_by")

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema_version": CLAIMS_LEDGER_SCHEMA_VERSION,
            "transition_id": self.transition_id,
            "claim_id": self.claim_id,
            "from_status": self.from_status,
            "to_status": self.to_status,
            "occurred_at": self.occurred_at,
            "reason": self.reason,
        }
        if self.superseded_by is not None:
            value["superseded_by"] = self.superseded_by
        return value


@dataclass(frozen=True)
class ClaimHydrationOmission:
    claim_id: str | None
    reason: str
    status: str
    required_fact: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "reason": self.reason,
            "status": self.status,
            "required_fact": self.required_fact,
        }


@dataclass(frozen=True)
class ClaimHydrationResult:
    claims: tuple[Claim, ...]
    units: tuple[ContextUnit, ...]
    omissions: tuple[ClaimHydrationOmission, ...]
    required_facts: tuple[str, ...]
    satisfied_facts: tuple[str, ...]

    @property
    def blocked(self) -> bool:
        return bool(set(self.required_facts) - set(self.satisfied_facts))

    @property
    def status(self) -> str:
        return "blocked" if self.blocked else "available"


__all__ = [
    "CLAIMS_LEDGER_SCHEMA_VERSION",
    "Claim",
    "ClaimHydrationOmission",
    "ClaimHydrationResult",
    "ClaimStatus",
    "ClaimTransition",
    "ClaimTransitionError",
    "ClaimsLedgerError",
    "FreshnessPolicy",
    "FreshnessResult",
    "FreshnessStatus",
]
