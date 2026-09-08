"""Versioned, privacy-safe proof receipts for routing decisions.

The producer API retains identifiers and hashes only.  Verification is
implemented in :mod:`verdict.receipt_verifier`, which has no producer-runtime
dependencies and can therefore be copied into an independent verifier.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from verdict.provider_receipts import canonical_hash
from verdict.receipt_verifier import SCHEMA_VERSION, VerificationResult, verify_serialized_receipt
from verdict.security import fingerprint_text

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_SENSITIVE = re.compile(
    r"(?i)(api[_-]?key|authorization|bearer|password|secret|token|private[_-]?key|"
    r"raw[_-]?prompt|raw[_-]?completion|messages|tool[_-]?arguments|email|phone|address)"
)


class ProofReceiptError(ValueError):
    """Raised when a producer receipt cannot satisfy the strict contract."""


def _id(value: str, field: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ProofReceiptError(f"{field} must be a bounded identifier")
    if _SENSITIVE.search(value):
        raise ProofReceiptError(f"{field} contains sensitive content")
    return value


def _digest(value: str, field: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ProofReceiptError(f"{field} must be a sha256 digest")
    return value


def _timestamp(value: datetime | str, field: str) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ProofReceiptError(f"{field} must include a timezone")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if not isinstance(value, str):
        raise ProofReceiptError(f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProofReceiptError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ProofReceiptError(f"{field} must include a timezone")
    return value


@dataclass(frozen=True)
class EvidenceReference:
    evidence_id: str
    digest: str
    status: str = "verified"

    def __post_init__(self) -> None:
        _id(self.evidence_id, "evidence_id")
        _digest(self.digest, "evidence.digest")
        if self.status != "verified":
            raise ProofReceiptError("evidence must be verified")

    def to_dict(self) -> dict[str, str]:
        return {"evidence_id": self.evidence_id, "digest": self.digest, "status": self.status}


@dataclass(frozen=True)
class SourceReference:
    source_id: str
    digest: str
    status: str = "verified"

    def __post_init__(self) -> None:
        _id(self.source_id, "source_id")
        _digest(self.digest, "source.digest")
        if self.status != "verified":
            raise ProofReceiptError("source references must be verified")

    def to_dict(self) -> dict[str, str]:
        return {"source_id": self.source_id, "digest": self.digest, "status": self.status}


@dataclass(frozen=True)
class DropReason:
    candidate_id: str
    code: str
    evidence_id: str | None = None

    def __post_init__(self) -> None:
        _id(self.candidate_id, "drop_reasons.candidate_id")
        _id(self.code, "drop_reasons.code")
        if self.evidence_id is not None:
            _id(self.evidence_id, "drop_reasons.evidence_id")

    def to_dict(self) -> dict[str, str]:
        value = {"candidate_id": self.candidate_id, "code": self.code}
        if self.evidence_id is not None:
            value["evidence_id"] = self.evidence_id
        return value


@dataclass(frozen=True)
class ClaimRecord:
    claim_id: str
    status: str
    claim_hash: str
    receipt_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    supersedes: str | None = None

    def __post_init__(self) -> None:
        _id(self.claim_id, "claim_id")
        if self.status not in {"active", "superseded", "disputed"}:
            raise ProofReceiptError("claim status is invalid")
        _digest(self.claim_hash, "claim_hash")
        if not self.receipt_refs and not self.evidence_refs:
            raise ProofReceiptError("claim must reference a receipt or evidence")
        for ref in (*self.receipt_refs, *self.evidence_refs):
            _id(ref, "claim reference")
        if self.supersedes is not None:
            _id(self.supersedes, "supersedes")

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "claim_id": self.claim_id,
            "status": self.status,
            "claim_hash": self.claim_hash,
            "receipt_refs": list(self.receipt_refs),
            "evidence_refs": list(self.evidence_refs),
        }
        if self.supersedes is not None:
            value["supersedes"] = self.supersedes
        return value


@dataclass(frozen=True)
class ProofReceipt:
    """A complete decision proof whose raw inputs are never persisted."""

    task_id: str
    request_id: str
    policy_version: str
    input_hash: str
    context_hash: str
    eligible_candidates: tuple[str, ...]
    selected_route: str | None
    drop_reasons: tuple[DropReason, ...]
    source_references: tuple[SourceReference, ...]
    evidence: tuple[EvidenceReference, ...]
    created_at: datetime | str
    decision_at: datetime | str
    receipt_id: str = ""
    claims: tuple[ClaimRecord, ...] = ()
    verified_at: datetime | str | None = None
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ProofReceiptError("unsupported receipt schema_version")
        for field in ("task_id", "request_id", "policy_version"):
            _id(getattr(self, field), field)
        _digest(self.input_hash, "input_hash")
        _digest(self.context_hash, "context_hash")
        if not self.eligible_candidates:
            raise ProofReceiptError("eligible_candidates must not be empty")
        for candidate in self.eligible_candidates:
            _id(candidate, "eligible_candidates")
        if self.selected_route is not None:
            _id(self.selected_route, "selected_route")
            if self.selected_route not in self.eligible_candidates:
                raise ProofReceiptError("selected_route must be one of eligible_candidates")
        elif not self.drop_reasons:
            raise ProofReceiptError("a denial must include at least one drop reason")
        if not self.source_references:
            raise ProofReceiptError("source_references must not be empty")
        if not self.evidence:
            raise ProofReceiptError("evidence must not be empty")
        for evidence_item in self.evidence:
            _id(evidence_item.evidence_id, "evidence_id")
            _digest(evidence_item.digest, "evidence.digest")
            if evidence_item.status != "verified":
                raise ProofReceiptError("evidence must be verified")
        for source_item in self.source_references:
            _id(source_item.source_id, "source_id")
            _digest(source_item.digest, "source.digest")
            if source_item.status != "verified":
                raise ProofReceiptError("source references must be verified")
        for drop_reason in self.drop_reasons:
            _id(drop_reason.candidate_id, "drop_reasons.candidate_id")
            _id(drop_reason.code, "drop_reasons.code")
            if drop_reason.evidence_id is not None:
                _id(drop_reason.evidence_id, "drop_reasons.evidence_id")
                if drop_reason.evidence_id not in {item.evidence_id for item in self.evidence}:
                    raise ProofReceiptError("drop reason references unavailable evidence")
        evidence_ids = {item.evidence_id for item in self.evidence}
        claim_ids: set[str] = set()
        for claim in self.claims:
            if claim.claim_id in claim_ids:
                raise ProofReceiptError("claims must have unique identifiers")
            claim_ids.add(claim.claim_id)
            if any(reference not in evidence_ids for reference in claim.evidence_refs):
                raise ProofReceiptError("claim references unavailable evidence")
            if claim.supersedes is not None and claim.supersedes not in claim_ids:
                raise ProofReceiptError("claim supersedes unavailable claim")
        object.__setattr__(self, "created_at", _timestamp(self.created_at, "created_at"))
        object.__setattr__(self, "decision_at", _timestamp(self.decision_at, "decision_at"))
        if self.verified_at is not None:
            object.__setattr__(self, "verified_at", _timestamp(self.verified_at, "verified_at"))
        _id(self.receipt_id, "receipt_id")

    def _protected(self) -> dict[str, Any]:
        timestamps: dict[str, str] = {
            "created_at": _timestamp(self.created_at, "created_at"),
            "decision_at": _timestamp(self.decision_at, "decision_at"),
        }
        if self.verified_at is not None:
            timestamps["verified_at"] = _timestamp(self.verified_at, "verified_at")
        return {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "task_id": self.task_id,
            "request_id": self.request_id,
            "policy_version": self.policy_version,
            "input_hash": self.input_hash,
            "context_hash": self.context_hash,
            "eligible_candidates": list(self.eligible_candidates),
            "selected_route": self.selected_route,
            "drop_reasons": [item.to_dict() for item in self.drop_reasons],
            "source_references": [item.to_dict() for item in self.source_references],
            "evidence": [item.to_dict() for item in self.evidence],
            "timestamps": timestamps,
            "claims": [item.to_dict() for item in self.claims],
        }

    @property
    def digest(self) -> str:
        return canonical_hash(self._protected())

    def to_dict(self) -> dict[str, Any]:
        payload = self._protected()
        payload["integrity"] = {"algorithm": "sha256", "receipt_digest": canonical_hash(payload)}
        return payload

    def serialize(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ProofReceipt:
        result = verify_serialized_receipt(value)
        if not result.valid:
            raise ProofReceiptError("; ".join(result.errors))
        try:
            payload = dict(value)
            timestamps = payload["timestamps"]
            evidence = tuple(EvidenceReference(**item) for item in payload["evidence"])
            sources = tuple(SourceReference(**item) for item in payload["source_references"])
            reasons = tuple(DropReason(**item) for item in payload["drop_reasons"])
            claims = tuple(
                ClaimRecord(
                    claim_id=item["claim_id"],
                    status=item["status"],
                    claim_hash=item["claim_hash"],
                    receipt_refs=tuple(item["receipt_refs"]),
                    evidence_refs=tuple(item["evidence_refs"]),
                    supersedes=item.get("supersedes"),
                )
                for item in payload["claims"]
            )
            return cls(
                task_id=payload["task_id"],
                request_id=payload["request_id"],
                policy_version=payload["policy_version"],
                input_hash=payload["input_hash"],
                context_hash=payload["context_hash"],
                eligible_candidates=tuple(payload["eligible_candidates"]),
                selected_route=payload["selected_route"],
                drop_reasons=reasons,
                source_references=sources,
                evidence=evidence,
                created_at=timestamps["created_at"],
                decision_at=timestamps["decision_at"],
                receipt_id=payload["receipt_id"],
                claims=claims,
                verified_at=timestamps.get("verified_at"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProofReceiptError(f"malformed receipt: {exc}") from exc

    @classmethod
    def issue(
        cls,
        *,
        task_id: str,
        request_id: str,
        policy_version: str,
        input_hash: str,
        context_hash: str,
        eligible_candidates: tuple[str, ...],
        selected_route: str | None,
        drop_reasons: tuple[DropReason, ...],
        source_references: tuple[SourceReference, ...],
        evidence: tuple[EvidenceReference, ...],
        claims: tuple[ClaimRecord, ...] = (),
        receipt_id: str | None = None,
        created_at: datetime | str | None = None,
        decision_at: datetime | str | None = None,
        verified_at: datetime | str | None = None,
    ) -> ProofReceipt:
        now = datetime.now(timezone.utc)
        return cls(
            task_id=task_id,
            request_id=request_id,
            policy_version=policy_version,
            input_hash=input_hash,
            context_hash=context_hash,
            eligible_candidates=eligible_candidates,
            selected_route=selected_route,
            drop_reasons=drop_reasons,
            source_references=source_references,
            evidence=evidence,
            created_at=created_at or now,
            decision_at=decision_at or created_at or now,
            receipt_id=receipt_id or f"receipt-{uuid4().hex}",
            claims=claims,
            verified_at=verified_at,
        )


def build_proof_receipt(**kwargs: Any) -> ProofReceipt:
    """Build a receipt, hashing optional transient input/context values immediately."""

    if "input_hash" not in kwargs and "input" in kwargs:
        kwargs["input_hash"] = canonical_hash(kwargs.pop("input"))
    if "context_hash" not in kwargs and "context" in kwargs:
        kwargs["context_hash"] = canonical_hash(kwargs.pop("context"))
    if "input" in kwargs or "context" in kwargs:
        raise ProofReceiptError(
            "input/context may only be supplied when the corresponding hash is absent"
        )
    return ProofReceipt.issue(**kwargs)


def build_receipt_manifest(
    receipts: tuple[ProofReceipt, ...] | list[ProofReceipt],
) -> dict[str, Any]:
    """Build a deterministic portable manifest containing complete receipts."""

    if not receipts:
        raise ProofReceiptError("manifest must contain at least one receipt")
    items = [receipt.to_dict() for receipt in receipts]
    return {
        "schema_version": SCHEMA_VERSION,
        "receipts": items,
        "manifest_digest": canonical_hash(items),
    }


def claim_hash(claim: str) -> str:
    """Hash claim text so the claim itself is not persisted in the receipt."""

    if not isinstance(claim, str) or not claim.strip():
        raise ProofReceiptError("claim must be non-empty")
    return fingerprint_text(claim, length=64)


__all__ = [
    "ClaimRecord",
    "DropReason",
    "EvidenceReference",
    "ProofReceipt",
    "ProofReceiptError",
    "SourceReference",
    "VerificationResult",
    "build_proof_receipt",
    "build_receipt_manifest",
    "claim_hash",
    "verify_serialized_receipt",
]
