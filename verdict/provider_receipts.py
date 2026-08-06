"""Provider-neutral deterministic evaluation receipts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

PROVIDER_RECEIPT_SCHEMA_VERSION = "1"


def canonical_hash(value: Any) -> str:
    """Hash JSON-compatible input using stable canonical serialization."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


@dataclass(frozen=True)
class ProviderReceipt:
    """Portable provider result; never an authorization decision."""

    run_id: str
    provider: str
    provider_version: str
    inputs_hash: str
    config_hash: str
    outcome: str
    provenance: Mapping[str, Any]
    evidence_refs: tuple[str, ...] = ()
    schema_version: str = PROVIDER_RECEIPT_SCHEMA_VERSION
    details: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        for name in ("run_id", "provider", "provider_version", "outcome"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if self.schema_version != PROVIDER_RECEIPT_SCHEMA_VERSION:
            raise ValueError("unsupported provider receipt schema_version")
        for name in ("inputs_hash", "config_hash"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.startswith("sha256:"):
                raise ValueError(f"{name} must be a sha256 digest")
        if not isinstance(self.provenance, Mapping):
            raise ValueError("provenance must be an object")
        if any(not isinstance(item, str) or not item.strip() for item in self.evidence_refs):
            raise ValueError("evidence_refs must contain non-empty strings")
        _reject_sensitive(self.provenance)
        object.__setattr__(self, "provenance", _freeze(self.provenance))
        if self.details is not None:
            _reject_sensitive(self.details)
            object.__setattr__(self, "details", _freeze(self.details))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "provider": self.provider,
            "provider_version": self.provider_version,
            "inputs_hash": self.inputs_hash,
            "config_hash": self.config_hash,
            "outcome": self.outcome,
            "provenance": _json_copy(self.provenance),
            "evidence_refs": list(self.evidence_refs),
            "details": _json_copy(self.details) if self.details is not None else None,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ProviderReceipt:
        if not isinstance(value, Mapping):
            raise ValueError("provider receipt must be an object")
        required = {
            "schema_version",
            "run_id",
            "provider",
            "provider_version",
            "inputs_hash",
            "config_hash",
            "outcome",
            "provenance",
            "evidence_refs",
            "details",
        }
        unknown = set(value) - required
        missing = required - set(value)
        if missing:
            raise ValueError(f"provider receipt missing field(s): {sorted(missing)}")
        if unknown:
            raise ValueError(f"provider receipt has unknown field(s): {sorted(unknown)}")
        refs = value["evidence_refs"]
        if not isinstance(refs, list):
            raise ValueError("evidence_refs must be an array")
        return cls(
            run_id=value["run_id"],
            provider=value["provider"],
            provider_version=value["provider_version"],
            inputs_hash=value["inputs_hash"],
            config_hash=value["config_hash"],
            outcome=value["outcome"],
            provenance=value["provenance"],
            evidence_refs=tuple(refs),
            schema_version=value["schema_version"],
            details=value["details"],
        )


def build_provider_receipt(
    *,
    run_id: str,
    provider: str,
    provider_version: str,
    inputs: Any,
    config: Any,
    outcome: str,
    provenance: Mapping[str, Any],
    evidence_refs: tuple[str, ...] = (),
    details: Mapping[str, Any] | None = None,
) -> ProviderReceipt:
    """Create a receipt from raw inputs while retaining only hashes."""
    return ProviderReceipt(
        run_id=run_id,
        provider=provider,
        provider_version=provider_version,
        inputs_hash=canonical_hash(inputs),
        config_hash=canonical_hash(config),
        outcome=outcome,
        provenance=provenance,
        evidence_refs=evidence_refs,
        details=details,
    )


def _reject_sensitive(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in {"api_key", "authorization", "password", "secret", "token"}:
                raise ValueError(f"sensitive receipt field rejected: {key}")
            _reject_sensitive(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _reject_sensitive(child)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(child) for key, child in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(child) for child in value)
    return value


def _json_copy(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_copy(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_copy(child) for child in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError("receipt metadata must be JSON-compatible")


__all__ = [
    "PROVIDER_RECEIPT_SCHEMA_VERSION",
    "ProviderReceipt",
    "build_provider_receipt",
    "canonical_hash",
]
