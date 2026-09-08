"""Dependency-free verification for serialized Verdict proof receipts.

This module intentionally imports only the Python standard library.  It is the
portable verification boundary: a verifier can inspect a receipt or manifest
without importing the routing, policy, gateway, or receipt-store runtime.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

SCHEMA_VERSION = "1"
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_SENSITIVE_ID = re.compile(
    r"(?i)(?:^|[-_.:/])(api[_-]?key|authorization|bearer|password|secret|token|"
    r"private[_-]?key|raw[_-]?prompt|raw[_-]?completion|messages|tool[_-]?arguments|"
    r"email|phone|address)(?:$|[-_.:/])"
)
_STATUSES = frozenset({"verified"})
_EVIDENCE_STATUSES = frozenset({"verified", "missing", "malformed", "skipped", "unavailable"})
_CLAIM_STATUSES = frozenset({"active", "superseded", "disputed"})
_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "receipt_id",
        "task_id",
        "request_id",
        "policy_version",
        "input_hash",
        "context_hash",
        "eligible_candidates",
        "selected_route",
        "drop_reasons",
        "source_references",
        "evidence",
        "timestamps",
        "claims",
        "integrity",
    }
)


@dataclass(frozen=True)
class VerificationResult:
    """A fail-closed verification result suitable for CLI and API callers."""

    valid: bool
    receipt_id: str | None = None
    errors: tuple[str, ...] = ()
    checked_evidence: int = 0

    def __bool__(self) -> bool:
        return self.valid


def canonical_json(value: Any) -> str:
    """Return the canonical JSON form shared by receipt creation and checking."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def canonical_digest(value: Any) -> str:
    return f"sha256:{hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()}"


def _load(value: Mapping[str, Any] | str | bytes) -> Mapping[str, Any]:
    if isinstance(value, (str, bytes)):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise ValueError("receipt must be a JSON object")
    return value


def _required_object(
    value: Any, name: str, required: set[str], optional: set[str] | None = None
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    keys = set(value)
    missing = required - keys
    unknown = keys - required - (optional or set())
    if missing:
        raise ValueError(f"{name} missing field(s): {', '.join(sorted(missing))}")
    if unknown:
        raise ValueError(f"{name} has unknown field(s): {', '.join(sorted(map(str, unknown)))}")
    return dict(value)


def _string(value: Any, name: str, *, identifier: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    if identifier and not _ID.fullmatch(value):
        raise ValueError(f"{name} must be a bounded identifier")
    if identifier and _SENSITIVE_ID.search(value):
        raise ValueError(f"{name} contains sensitive content")
    return value


def _digest(value: Any, name: str) -> str:
    text = _string(value, name)
    if not _DIGEST.fullmatch(text):
        raise ValueError(f"{name} must be a sha256 digest")
    return text


def _timestamp(value: Any, name: str) -> str:
    text = _string(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return text


def _validate_evidence(value: Any) -> tuple[int, list[str]]:
    if not isinstance(value, list) or not value:
        raise ValueError("evidence must be a non-empty array")
    errors: list[str] = []
    for index, item in enumerate(value):
        try:
            entry = _required_object(
                item,
                f"evidence[{index}]",
                {"evidence_id", "digest", "status"},
            )
            _string(entry["evidence_id"], f"evidence[{index}].evidence_id", identifier=True)
            _digest(entry["digest"], f"evidence[{index}].digest")
            status = _string(entry["status"], f"evidence[{index}].status")
            if status not in _EVIDENCE_STATUSES:
                raise ValueError(f"evidence[{index}].status is invalid")
            if status != "verified":
                raise ValueError(f"evidence[{index}] is {status}")
        except ValueError as exc:
            errors.append(str(exc))
    return len(value), errors


def _validate_receipt(value: Mapping[str, Any]) -> tuple[list[str], str | None, int]:
    errors: list[str] = []
    receipt_id: str | None = None
    checked_evidence = 0
    try:
        payload = _required_object(value, "receipt", set(_RECEIPT_FIELDS))
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported receipt schema_version")
        receipt_id = _string(payload["receipt_id"], "receipt_id", identifier=True)
        _string(payload["task_id"], "task_id", identifier=True)
        _string(payload["request_id"], "request_id", identifier=True)
        _string(payload["policy_version"], "policy_version", identifier=True)
        _digest(payload["input_hash"], "input_hash")
        _digest(payload["context_hash"], "context_hash")

        candidates = payload["eligible_candidates"]
        if not isinstance(candidates, list) or not candidates:
            raise ValueError("eligible_candidates must be a non-empty array")
        for index, candidate in enumerate(candidates):
            _string(candidate, f"eligible_candidates[{index}]", identifier=True)
        route = payload["selected_route"]
        if route is not None:
            _string(route, "selected_route", identifier=True)

        reasons = payload["drop_reasons"]
        if not isinstance(reasons, list):
            raise ValueError("drop_reasons must be an array")
        for index, reason in enumerate(reasons):
            item = _required_object(
                reason,
                f"drop_reasons[{index}]",
                {"candidate_id", "code"},
                {"evidence_id"},
            )
            _string(item["candidate_id"], f"drop_reasons[{index}].candidate_id", identifier=True)
            _string(item["code"], f"drop_reasons[{index}].code", identifier=True)
            if "evidence_id" in item:
                _string(item["evidence_id"], f"drop_reasons[{index}].evidence_id", identifier=True)
        if route is None and not reasons:
            raise ValueError("a denial must include at least one drop reason")
        if route is not None and route not in candidates:
            raise ValueError("selected_route must be one of eligible_candidates")

        sources = payload["source_references"]
        if not isinstance(sources, list) or not sources:
            raise ValueError("source_references must be a non-empty array")
        for index, source in enumerate(sources):
            item = _required_object(
                source,
                f"source_references[{index}]",
                {"source_id", "digest", "status"},
            )
            _string(item["source_id"], f"source_references[{index}].source_id", identifier=True)
            _digest(item["digest"], f"source_references[{index}].digest")
            if item["status"] not in _STATUSES:
                raise ValueError(f"source_references[{index}] is not verified")

        checked_evidence, evidence_errors = _validate_evidence(payload["evidence"])
        errors.extend(evidence_errors)
        evidence_ids = {
            str(item["evidence_id"])
            for item in payload["evidence"]
            if isinstance(item, Mapping) and "evidence_id" in item
        }
        for index, reason in enumerate(reasons):
            if "evidence_id" in reason and reason["evidence_id"] not in evidence_ids:
                raise ValueError(f"drop_reasons[{index}] references unavailable evidence")

        timestamps = _required_object(
            payload["timestamps"],
            "timestamps",
            {"created_at", "decision_at"},
            {"verified_at"},
        )
        for name, timestamp in timestamps.items():
            _timestamp(timestamp, f"timestamps.{name}")

        claims = payload["claims"]
        if not isinstance(claims, list):
            raise ValueError("claims must be an array")
        claim_ids: set[str] = set()
        for index, claim in enumerate(claims):
            item = _required_object(
                claim,
                f"claims[{index}]",
                {"claim_id", "status", "claim_hash", "receipt_refs", "evidence_refs"},
                {"supersedes"},
            )
            _string(item["claim_id"], f"claims[{index}].claim_id", identifier=True)
            if item["claim_id"] in claim_ids:
                raise ValueError(f"claims[{index}] duplicates claim_id")
            claim_ids.add(item["claim_id"])
            if item["status"] not in _CLAIM_STATUSES:
                raise ValueError(f"claims[{index}].status is invalid")
            _digest(item["claim_hash"], f"claims[{index}].claim_hash")
            refs = item["receipt_refs"]
            evidence_refs = item["evidence_refs"]
            if not isinstance(refs, list) or not isinstance(evidence_refs, list):
                raise ValueError(f"claims[{index}] references must be arrays")
            if not refs and not evidence_refs:
                raise ValueError(f"claims[{index}] must reference a receipt or evidence")
            for ref in refs:
                _string(ref, f"claims[{index}].receipt_refs", identifier=True)
            for ref in evidence_refs:
                _string(ref, f"claims[{index}].evidence_refs", identifier=True)
                if ref not in evidence_ids:
                    raise ValueError(f"claims[{index}] references unavailable evidence {ref}")
            if "supersedes" in item:
                _string(item["supersedes"], f"claims[{index}].supersedes", identifier=True)
                if item["supersedes"] not in claim_ids:
                    raise ValueError(f"claims[{index}] supersedes unavailable claim")

        integrity = _required_object(payload["integrity"], "integrity", {"algorithm", "receipt_digest"})
        if integrity["algorithm"] != "sha256":
            raise ValueError("integrity.algorithm must be sha256")
        supplied = _digest(integrity["receipt_digest"], "integrity.receipt_digest")
        protected = {key: payload[key] for key in _RECEIPT_FIELDS if key != "integrity"}
        expected = canonical_digest(protected)
        if supplied != expected:
            raise ValueError("receipt integrity digest mismatch")
    except (TypeError, ValueError, KeyError) as exc:
        errors.append(str(exc))
    return errors, receipt_id, checked_evidence


def verify_serialized_receipt(value: Mapping[str, Any] | str | bytes) -> VerificationResult:
    """Verify a serialized receipt without importing the producer runtime."""

    try:
        payload = _load(value)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return VerificationResult(False, errors=(f"malformed receipt: {exc}",))
    errors, receipt_id, checked = _validate_receipt(payload)
    return VerificationResult(not errors, receipt_id=receipt_id, errors=tuple(errors), checked_evidence=checked)


def verify_serialized_manifest(value: Mapping[str, Any] | str | bytes) -> VerificationResult:
    """Verify a manifest and every receipt it carries, fail-closed."""

    try:
        payload = _load(value)
        manifest = _required_object(payload, "manifest", {"schema_version", "receipts", "manifest_digest"})
        if manifest["schema_version"] != SCHEMA_VERSION:
            raise ValueError("unsupported manifest schema_version")
        receipts = manifest["receipts"]
        if not isinstance(receipts, list) or not receipts:
            raise ValueError("manifest receipts must be a non-empty array")
        supplied = _digest(manifest["manifest_digest"], "manifest_digest")
        expected = canonical_digest(receipts)
        if supplied != expected:
            raise ValueError("manifest integrity digest mismatch")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return VerificationResult(False, errors=(f"malformed manifest: {exc}",))

    errors: list[str] = []
    receipt_id: str | None = None
    checked = 0
    for index, receipt in enumerate(receipts):
        result = verify_serialized_receipt(receipt)
        checked += result.checked_evidence
        receipt_id = receipt_id or result.receipt_id
        errors.extend(f"receipts[{index}]: {error}" for error in result.errors)
    return VerificationResult(not errors, receipt_id=receipt_id, errors=tuple(errors), checked_evidence=checked)


class IndependentReceiptVerifier:
    """Stateless facade for embedding the verifier in a clean process."""

    @staticmethod
    def verify_receipt(value: Mapping[str, Any] | str | bytes) -> VerificationResult:
        return verify_serialized_receipt(value)

    @staticmethod
    def verify_manifest(value: Mapping[str, Any] | str | bytes) -> VerificationResult:
        return verify_serialized_manifest(value)


ReceiptVerifier = IndependentReceiptVerifier


# Friendly aliases for callers that prefer the shorter names.
verify_receipt = verify_serialized_receipt
verify_manifest = verify_serialized_manifest


__all__ = [
    "SCHEMA_VERSION",
    "IndependentReceiptVerifier",
    "ReceiptVerifier",
    "VerificationResult",
    "canonical_digest",
    "canonical_json",
    "verify_manifest",
    "verify_receipt",
    "verify_serialized_manifest",
    "verify_serialized_receipt",
]
