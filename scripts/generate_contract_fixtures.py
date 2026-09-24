#!/usr/bin/env python3
"""Generate deterministic ExecutionEnvelope fixtures for cross-language contract testing."""

import hashlib
import json

# Import from local verdict package
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from verdict.contracts import ExecutionEnvelope, TaskSpec, VerificationPlan

# Fixed timestamp for deterministic output
FIXED_NOW = "2024-01-15T12:00:00Z"
FIXED_EXPIRY = "2024-01-15T13:00:00Z"
FIXED_PAST_EXPIRY = "2024-01-15T11:00:00Z"

# Canonical policy digest (SHA-256 of a known policy)
CANONICAL_DIGEST = "a" * 64  # Example digest


def create_task_spec(**overrides):
    """Create a minimal TaskSpec for fixtures."""
    defaults = {
        "objective": "test execution",
        "task_type": "test",
        "capabilities": [],
        "schema_version": "1",
    }
    defaults.update(overrides)
    return TaskSpec.from_dict(defaults)


def create_verification_plan(**overrides):
    """Create a minimal VerificationPlan for fixtures."""
    defaults = {"checks": [], "schema_version": "1"}
    defaults.update(overrides)
    return VerificationPlan.from_dict(defaults)


def fixture_accepted():
    """Accepted envelope: all checks pass."""
    task_spec = create_task_spec()
    verification = create_verification_plan()

    envelope = ExecutionEnvelope(
        task_spec=task_spec,
        eligibility_decision={"decision": "accept", "admitted": True},
        policy_digest=CANONICAL_DIGEST,
        allowed_capabilities=["read", "write"],
        execution_constraints={"max_usd": 1.0, "max_ms": 5000, "expires_at": FIXED_EXPIRY},
        verification_requirements=verification,
        evidence_ids=["evidence-001"],
        routing_decision={"routed_to": "node-1", "decision": "accept"},
        created_at=FIXED_NOW,
        schema_version="1",
    )
    return envelope.to_dict()


def fixture_denied():
    """Hard denied envelope: eligibility_decision denies."""
    task_spec = create_task_spec()
    verification = create_verification_plan()

    envelope = ExecutionEnvelope(
        task_spec=task_spec,
        eligibility_decision={"decision": "deny", "reason": "policy violation"},
        policy_digest=CANONICAL_DIGEST,
        allowed_capabilities=[],
        execution_constraints={},
        verification_requirements=verification,
        evidence_ids=[],
        routing_decision=None,  # No routing for denied
        created_at=FIXED_NOW,
        schema_version="1",
    )
    return envelope.to_dict()


def fixture_expired():
    """Expired envelope: created_at and expires_at in the past."""
    task_spec = create_task_spec()
    verification = create_verification_plan()

    envelope = ExecutionEnvelope(
        task_spec=task_spec,
        eligibility_decision={"decision": "accept", "admitted": True},
        policy_digest=CANONICAL_DIGEST,
        allowed_capabilities=["read"],
        execution_constraints={
            "expires_at": FIXED_PAST_EXPIRY  # Already expired
        },
        verification_requirements=verification,
        evidence_ids=["evidence-002"],
        routing_decision={"routed_to": "node-2", "decision": "accept"},
        created_at=FIXED_NOW,
        schema_version="1",
    )
    return envelope.to_dict()


def fixture_wrong_digest():
    """Wrong digest: policy_digest does not match expected."""
    task_spec = create_task_spec()
    verification = create_verification_plan()

    envelope = ExecutionEnvelope(
        task_spec=task_spec,
        eligibility_decision={"decision": "accept", "admitted": True},
        policy_digest="b" * 64,  # Wrong digest
        allowed_capabilities=["read"],
        execution_constraints={"max_usd": 1.0},
        verification_requirements=verification,
        evidence_ids=["evidence-003"],
        routing_decision={"routed_to": "node-3", "decision": "accept"},
        created_at=FIXED_NOW,
        schema_version="1",
    )
    return envelope.to_dict()


def fixture_unknown_field():
    """Unknown field: additive field consumers may ignore or reject."""
    base = fixture_accepted()
    base["unknown_future_field"] = "future_value"
    return base


def fixture_null_defaults():
    """Null defaults: optional fields absent."""
    task_spec = create_task_spec()
    verification = create_verification_plan()

    envelope = ExecutionEnvelope(
        task_spec=task_spec,
        eligibility_decision={"decision": "accept", "admitted": True},
        policy_digest=CANONICAL_DIGEST,
        allowed_capabilities=["read"],
        execution_constraints={},
        verification_requirements=verification,
        evidence_ids=[],
        routing_decision=None,  # Explicitly null
        created_at=None,  # Explicitly null
        schema_version="1",
    )
    return envelope.to_dict()


def sha256_json(obj):
    """Compute SHA-256 of canonicalized JSON."""
    canonical = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def generate_fixtures(output_dir: Path):
    """Generate all fixtures and manifest."""
    output_dir.mkdir(parents=True, exist_ok=True)

    fixtures = {
        "accepted.json": (fixture_accepted(), "ACCEPT"),
        "denied.json": (fixture_denied(), "DENY"),
        "expired.json": (fixture_expired(), "EXPIRED"),
        "wrong-digest.json": (fixture_wrong_digest(), "DIGEST_MISMATCH"),
        "unknown-field.json": (fixture_unknown_field(), "ACCEPT_IGNORING_UNKNOWN"),
        "null-defaults.json": (fixture_null_defaults(), "ACCEPT_DEFAULTS"),
    }

    manifest = {
        "contract_version": "1",
        "schema_id": "https://llm-gate.dev/schemas/contracts.v1.json#/$defs/execution_envelope",
        "fixtures": {},
    }

    for filename, (fixture_data, expected_verdict) in fixtures.items():
        # Write fixture with sorted keys for determinism
        fixture_path = output_dir / filename
        fixture_path.write_text(json.dumps(fixture_data, indent=2, sort_keys=True) + "\n")

        # Add to manifest
        manifest["fixtures"][filename] = {
            "sha256": sha256_json(fixture_data),
            "expected_verdict": expected_verdict,
        }

    # Write manifest
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    print(f"Generated {len(fixtures)} fixtures in {output_dir}")
    print(f"Manifest: {manifest_path}")


if __name__ == "__main__":
    repo_root = Path(__file__).parent.parent
    output_dir = repo_root / "contracts" / "fixtures" / "execution-envelope" / "v1"
    generate_fixtures(output_dir)
