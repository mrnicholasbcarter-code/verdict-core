"""Signed, independently verifiable policy-decision artifacts."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from verdict.policy import EligibilityCompilation, Policy, PolicyValidationError

ARTIFACT_SCHEMA_VERSION = "1"


def _canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _key(value: str | bytes) -> bytes:
    key = value.encode() if isinstance(value, str) else value
    if not key:
        raise PolicyValidationError("artifact verification key must not be empty")
    return key


@dataclass(frozen=True)
class SignedPolicyDecisionArtifact:
    """A compact signature envelope for an immutable policy compilation.

    HMAC is intentionally used as the portable v1 signing primitive: the
    issuer and verifier can independently validate integrity without adding a
    cryptography dependency. The key distribution and issuer registry remain
    deployment responsibilities; a valid signature does not prove factual
    correctness of provider evidence.
    """

    issuer_id: str
    policy_version: str
    policy_digest: str
    compilation_digest: str
    decisions: tuple[dict[str, Any], ...]
    issued_at: datetime
    signature: str
    schema_version: str = ARTIFACT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != ARTIFACT_SCHEMA_VERSION:
            raise PolicyValidationError("artifact schema_version must be '1'")
        for name in ("issuer_id", "policy_version", "policy_digest", "compilation_digest"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise PolicyValidationError(f"artifact {name} must be non-empty")
        if self.issued_at.tzinfo is None:
            raise PolicyValidationError("artifact issued_at must be timezone-aware")
        if not isinstance(self.signature, str) or not self.signature.startswith("hmac-sha256:"):
            raise PolicyValidationError("artifact signature must use hmac-sha256")

    def unsigned_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "issuer_id": self.issuer_id,
            "policy_version": self.policy_version,
            "policy_digest": self.policy_digest,
            "compilation_digest": self.compilation_digest,
            "decisions": list(self.decisions),
            "issued_at": self.issued_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.unsigned_payload(), "signature": self.signature}

    def verify(self, key: str | bytes) -> bool:
        expected = _sign(self.unsigned_payload(), key)
        return hmac.compare_digest(self.signature, expected)

    @classmethod
    def issue(
        cls,
        issuer_id: str,
        policy: Policy,
        compilation: EligibilityCompilation,
        key: str | bytes,
        *,
        issued_at: datetime | None = None,
    ) -> SignedPolicyDecisionArtifact:
        timestamp = issued_at or datetime.now(timezone.utc)
        if timestamp.tzinfo is None:
            raise PolicyValidationError("artifact issued_at must be timezone-aware")
        decisions = tuple(item.to_dict() for item in compilation.decisions)
        unsigned: dict[str, Any] = {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "issuer_id": issuer_id,
            "policy_version": policy.version,
            "policy_digest": policy.digest,
            "compilation_digest": compilation.digest,
            "decisions": decisions,
            "issued_at": timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        return cls(
            issuer_id=issuer_id,
            policy_version=policy.version,
            policy_digest=policy.digest,
            compilation_digest=compilation.digest,
            decisions=decisions,
            issued_at=timestamp,
            signature=_sign(unsigned, key),
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> SignedPolicyDecisionArtifact:
        required = {
            "schema_version",
            "issuer_id",
            "policy_version",
            "policy_digest",
            "compilation_digest",
            "decisions",
            "issued_at",
            "signature",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise PolicyValidationError("policy artifact fields are invalid")
        if not isinstance(value["decisions"], list):
            raise PolicyValidationError("artifact decisions must be an array")
        try:
            issued_at = datetime.fromisoformat(str(value["issued_at"]).replace("Z", "+00:00"))
        except ValueError as exc:
            raise PolicyValidationError("artifact issued_at must be ISO-8601") from exc
        return cls(
            schema_version=value["schema_version"],
            issuer_id=value["issuer_id"],
            policy_version=value["policy_version"],
            policy_digest=value["policy_digest"],
            compilation_digest=value["compilation_digest"],
            decisions=tuple(value["decisions"]),
            issued_at=issued_at,
            signature=value["signature"],
        )


def _sign(payload: dict[str, Any], key: str | bytes) -> str:
    digest = hmac.new(_key(key), _canonical(payload), hashlib.sha256).hexdigest()
    return f"hmac-sha256:{digest}"


__all__ = ["ARTIFACT_SCHEMA_VERSION", "SignedPolicyDecisionArtifact"]
