"""Cross-repo contract compatibility manifest and fail-closed check (CON-001).

See docs/adr/ADR-024-cross-repo-compatibility-gate.md. Downstream repos
(verdict-node and others) declare which contract hashes they were built
against; `check_compatibility()` fails closed on any mismatch or missing
declaration rather than assuming compatibility.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from types import MappingProxyType

from verdict.contracts import (
    AvailabilitySnapshot,
    Contract,
    OutcomeEvent,
    RoutingDecisionContract,
    RuntimeCandidate,
    TaskSpec,
    WorkflowPlan,
)
from verdict.provider_receipts import canonical_hash
from verdict.swarm_contracts import SwarmTaskEnvelope

COMPATIBILITY_MANIFEST_SCHEMA_VERSION = "1"

# The cross-repo-relevant contract set, matching CONTRACT_PARITY.md's
# Python <-> TypeScript parity list.
CROSS_REPO_CONTRACTS: tuple[type[Contract], ...] = (
    TaskSpec,
    RoutingDecisionContract,
    AvailabilitySnapshot,
    RuntimeCandidate,
    WorkflowPlan,
    OutcomeEvent,
    SwarmTaskEnvelope,
)


def _contract_schema_hash(contract_cls: type[Contract]) -> str:
    # Contract itself isn't a @dataclass (only its concrete subclasses are), so
    # mypy can't statically prove this is a DataclassInstance -- every member of
    # CROSS_REPO_CONTRACTS is, in fact, a dataclass (see test_compatibility_manifest.py).
    field_shape = sorted((f.name, str(f.type)) for f in fields(contract_cls))  # type: ignore[arg-type]
    payload = {
        "name": contract_cls.__name__,
        "contract_version": contract_cls.contract_version,
        "fields": field_shape,
    }
    return canonical_hash(payload)


@dataclass(frozen=True)
class CompatibilityManifest:
    """Deterministic snapshot of cross-repo contract schema hashes."""

    contracts: Mapping[str, str]
    manifest_hash: str
    schema_version: str = COMPATIBILITY_MANIFEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != COMPATIBILITY_MANIFEST_SCHEMA_VERSION:
            raise ValueError("unsupported compatibility manifest schema_version")
        object.__setattr__(self, "contracts", MappingProxyType(dict(self.contracts)))

    def to_dict(self) -> dict[str, str | dict[str, str]]:
        return {
            "schema_version": self.schema_version,
            "manifest_hash": self.manifest_hash,
            "contracts": dict(self.contracts),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> CompatibilityManifest:
        if not isinstance(payload, Mapping):
            raise ValueError("compatibility manifest must be an object")
        required = {"schema_version", "manifest_hash", "contracts"}
        missing = required - set(payload)
        if missing:
            raise ValueError(f"compatibility manifest missing field(s): {sorted(missing)}")
        contracts = payload["contracts"]
        if not isinstance(contracts, Mapping):
            raise ValueError("compatibility manifest 'contracts' must be an object")
        return cls(
            schema_version=str(payload["schema_version"]),
            manifest_hash=str(payload["manifest_hash"]),
            contracts={str(k): str(v) for k, v in contracts.items()},
        )


def build_compatibility_manifest() -> CompatibilityManifest:
    """Build the current compatibility manifest from CROSS_REPO_CONTRACTS."""
    contracts = {cls.__name__: _contract_schema_hash(cls) for cls in CROSS_REPO_CONTRACTS}
    manifest_hash = canonical_hash(dict(sorted(contracts.items())))
    return CompatibilityManifest(contracts=contracts, manifest_hash=manifest_hash)


@dataclass(frozen=True)
class CompatibilityCheckResult:
    """Result of checking a declared compatibility claim against the current manifest."""

    allowed: bool
    reason: str | None
    mismatched_contracts: tuple[str, ...] = ()


def check_compatibility(
    declared: Mapping[str, str], current: CompatibilityManifest | None = None
) -> CompatibilityCheckResult:
    """Fail-closed compatibility check: unverified or mismatched claims are rejected.

    Extra entries in `declared` for contracts the current manifest doesn't
    know about are ignored (forward-compatible declarations aren't a risk);
    a missing or hash-mismatched entry for any contract the current manifest
    *does* have is rejected.
    """
    manifest = current if current is not None else build_compatibility_manifest()

    missing = [name for name in manifest.contracts if name not in declared]
    if missing:
        return CompatibilityCheckResult(
            allowed=False, reason="missing_declaration", mismatched_contracts=tuple(sorted(missing))
        )

    mismatched = [
        name
        for name, expected_hash in manifest.contracts.items()
        if declared[name] != expected_hash
    ]
    if mismatched:
        return CompatibilityCheckResult(
            allowed=False,
            reason="contract_hash_mismatch",
            mismatched_contracts=tuple(sorted(mismatched)),
        )

    return CompatibilityCheckResult(allowed=True, reason=None)


__all__ = [
    "COMPATIBILITY_MANIFEST_SCHEMA_VERSION",
    "CROSS_REPO_CONTRACTS",
    "CompatibilityCheckResult",
    "CompatibilityManifest",
    "build_compatibility_manifest",
    "check_compatibility",
]
