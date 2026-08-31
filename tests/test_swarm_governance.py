from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from verdict.contracts import ContractValidationError
from verdict.swarm_contracts import (
    SwarmTaskBudget,
    SwarmTaskEnvelope,
    approved_envelope_bounds,
    capture_envelope_digest,
    validate_envelope_link,
)
from verdict.swarm_dispatcher import SwarmDispatchPolicy, dispatch_governed_swarm
from verdict.swarm_governance import (
    ConflictPolicy,
    SupervisorPolicy,
    SwarmAgentAssignment,
    SwarmRole,
    SwarmSlice,
    SwarmSpec,
    VerificationProfile,
)


def verification() -> VerificationProfile:
    return VerificationProfile(
        profile_id="verify-core",
        version="1",
        required_checks=("pytest",),
        required_evidence=("test-report",),
    )


def envelope(**overrides: object) -> SwarmTaskEnvelope:
    payload = {
        "task_id": "slice-1-task",
        "objective": "implement bounded slice",
        "allowed_paths": ["/home/nick/dev/verdict-core/verdict/swarm_governance.py"],
        "required_capabilities": ["edit"],
        "budget": SwarmTaskBudget(max_tokens=1000),
        "timeout_ms": 1000,
        "max_iterations": 3,
        "max_parallelism": 1,
        "verification_command": "pytest:tests/test_swarm_governance.py",
    }
    payload.update(overrides)
    return SwarmTaskEnvelope(**payload)


def role(**overrides: object) -> SwarmRole:
    payload = {
        "role_id": "coder",
        "name": "Coder",
        "required_capabilities": ("edit",),
        "optional_capabilities": ("test",),
        "forbidden_capabilities": ("deploy",),
        "allowed_tools": ("read_file", "write"),
        "model_floor": "low",
        "max_parallelism": 1,
        "verification": verification(),
    }
    payload.update(overrides)
    return SwarmRole(**payload)


def assignment(**overrides: object) -> SwarmAgentAssignment:
    payload = {
        "agent_id": "agent-1",
        "role_id": "coder",
        "capabilities": ("edit", "test"),
        "allowed_tools": ("read_file",),
        "model": "low",
        "resource_refs": ("/home/nick/dev/verdict-core/verdict/swarm_governance.py",),
        "slice_id": "slice-1",
    }
    payload.update(overrides)
    return SwarmAgentAssignment(**payload)


def spec(**overrides: object) -> SwarmSpec:
    payload = {
        "swarm_id": "swarm-1",
        "objective": "ship governed swarm contract models",
        "roles": (role(),),
        "agents": (assignment(),),
        "context_refs": ("context-pack:abc",),
        "model_constraints": {"allowlist": ("low", "mid")},
        "budget": SwarmTaskBudget(max_tokens=2000),
        "max_concurrency": 1,
        "conflict_policy": ConflictPolicy(policy_id="conflict", version="1"),
        "supervisor": SupervisorPolicy(
            allowed_actions=("pause", "resume", "cancel", "status", "result"),
            cancellation_deadline_ms=1000,
        ),
        "verification": verification(),
        "evidence_scope": "swarm/scope",
    }
    payload.update(overrides)
    return SwarmSpec(**payload)


def test_spec_serialization_and_digest_are_deterministic() -> None:
    first = spec()
    second = spec()

    assert first.to_dict() == second.to_dict()
    assert first.digest() == second.digest()
    assert first.digest().startswith("sha256:")
    digests = {spec().digest() for _ in range(100)}
    assert digests == {first.digest()}
    assert list(first.to_dict()) == [
        "schema_version",
        "swarm_id",
        "objective",
        "roles",
        "agents",
        "context_refs",
        "model_constraints",
        "budget",
        "max_concurrency",
        "conflict_policy",
        "supervisor",
        "verification",
        "evidence_scope",
    ]


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"roles": (role(), role())}, "role IDs must be unique"),
        ({"agents": (assignment(), assignment())}, "agent IDs must be unique"),
        ({"agents": (assignment(role_id="missing"),)}, "unknown role_id"),
        ({"agents": (assignment(capabilities=("test",)),)}, "missing required capabilities"),
        ({"agents": (assignment(capabilities=("edit", "deploy")),)}, "forbidden capabilities"),
        ({"agents": (assignment(allowed_tools=("delete",)),)}, "not allowed for role"),
        ({"max_concurrency": 2}, "max_concurrency cannot exceed role parallelism"),
    ],
)
def test_spec_validates_references_and_bounds(override: dict[str, object], message: str) -> None:
    with pytest.raises(ContractValidationError, match=message):
        spec(**override)


def test_verification_profile_requires_closed_required_proof() -> None:
    with pytest.raises(ContractValidationError, match="required_checks"):
        VerificationProfile(profile_id="verify", version="1", required_evidence=("report",))

    with pytest.raises(ContractValidationError, match="required_evidence"):
        VerificationProfile(profile_id="verify", version="1", required_checks=("pytest",))

    with pytest.raises(ContractValidationError, match="fail_closed"):
        VerificationProfile(
            profile_id="verify",
            version="1",
            required_checks=("pytest",),
            required_evidence=("report",),
            fail_closed=False,
        )


def test_role_verification_must_include_swarm_required_checks() -> None:
    with pytest.raises(
        ContractValidationError, match="role verification must include swarm checks"
    ):
        spec(
            verification=VerificationProfile(
                profile_id="verify-sw",
                version="1",
                required_checks=("pytest", "lint"),
                required_evidence=("report",),
            )
        )


def test_slice_links_to_spec_and_envelope_digest() -> None:
    swarm = spec()
    task = envelope()
    slice_contract = SwarmSlice(
        slice_id="slice-1",
        swarm_id=swarm.swarm_id,
        role_id="coder",
        agent_id="agent-1",
        envelope=task,
        verification=verification(),
        evidence_root_id="receipt-root",
        swarm_spec_digest=swarm.digest(),
        spec=swarm,
    )

    assert slice_contract.envelope_digest == SwarmSlice.digest_envelope(task)
    assert slice_contract.to_dict()["spec"] is None
    assert (
        slice_contract.digest()
        == SwarmSlice.from_spec(
            spec=swarm,
            assignment_id="agent-1",
            envelope=task,
            verification=verification(),
            evidence_root_id="receipt-root",
            slice_id="slice-1",
        ).digest()
    )


def test_slice_rejects_bad_links_and_broadened_envelope() -> None:
    swarm = spec()
    task = envelope()

    with pytest.raises(ContractValidationError, match="swarm_id must match spec"):
        SwarmSlice(
            slice_id="slice-1",
            swarm_id="other",
            role_id="coder",
            agent_id="agent-1",
            envelope=task,
            verification=verification(),
            evidence_root_id="receipt-root",
            swarm_spec_digest=swarm.digest(),
            spec=swarm,
        )

    with pytest.raises(ContractValidationError, match="cannot broaden allowed_paths"):
        SwarmSlice.from_spec(
            spec=swarm,
            assignment_id="agent-1",
            envelope=envelope(allowed_paths=["/home/nick/dev/verdict-core", "/workspace/extra"]),
            verification=verification(),
            evidence_root_id="receipt-root",
        )

    with pytest.raises(ContractValidationError, match="cannot broaden max_parallelism"):
        SwarmSlice.from_spec(
            spec=swarm,
            assignment_id="agent-1",
            envelope=envelope(max_parallelism=2),
            verification=verification(),
            evidence_root_id="receipt-root",
        )


def test_invalid_swarm_spec_never_calls_dispatcher_or_adapter() -> None:
    dispatcher = MagicMock()
    adapter = MagicMock()
    snapshot = MagicMock()
    invalid_payload = {
        "schema_version": "swarm-spec/v1",
        "swarm_id": "swarm-1",
        "objective": "invalid because role is missing",
        "roles": [role().to_dict()],
        "agents": [assignment(role_id="missing").to_dict()],
        "context_refs": ["context-pack:abc"],
        "model_constraints": {"allowlist": ["low", "mid"]},
        "budget": {"max_usd": 0.0, "max_tokens": 2000, "max_latency_ms": 0},
        "max_concurrency": 1,
        "conflict_policy": ConflictPolicy(policy_id="conflict", version="1").to_dict(),
        "supervisor": SupervisorPolicy(
            allowed_actions=("pause", "resume", "cancel", "status", "result"),
            cancellation_deadline_ms=1000,
        ).to_dict(),
        "verification": verification().to_dict(),
        "evidence_scope": "swarm/scope",
    }

    with pytest.raises(ContractValidationError, match="unknown role_id"):
        dispatch_governed_swarm(
            invalid_payload,
            envelope=envelope(),
            snapshot=snapshot,
            dispatcher=dispatcher,
            adapter=adapter,
        )

    assert dispatcher.dispatch.call_count == 0
    assert adapter.submit.call_count == 0
    snapshot.assert_not_called()


def test_envelope_link_captures_immutable_digest_and_rejects_weakened_bounds() -> None:
    task = envelope()
    digest = capture_envelope_digest(task)
    assert digest == SwarmSlice.digest_envelope(task)
    assert digest.startswith("sha256:")
    assert validate_envelope_link(task, digest) == digest

    with pytest.raises(ContractValidationError, match="envelope_digest is required"):
        validate_envelope_link(task, None)
    with pytest.raises(ContractValidationError, match="envelope_digest is required"):
        validate_envelope_link(task, "   ")
    with pytest.raises(ContractValidationError, match="envelope_digest does not match"):
        validate_envelope_link(task, "sha256:deadbeef")

    approved = approved_envelope_bounds(task)
    with pytest.raises(ContractValidationError, match="cannot weaken max_parallelism"):
        validate_envelope_link(task, digest, proposed_bounds={**approved, "max_parallelism": 2})
    with pytest.raises(ContractValidationError, match="cannot weaken timeout_ms"):
        validate_envelope_link(task, digest, proposed_bounds={**approved, "timeout_ms": 5000})
    with pytest.raises(ContractValidationError, match="cannot weaken required_capabilities"):
        validate_envelope_link(
            task, digest, proposed_bounds={**approved, "required_capabilities": ["edit", "deploy"]}
        )


def test_dispatch_policy_accepts_swarm_level_bounds_without_weakening_envelope() -> None:
    swarm = spec(max_concurrency=1, budget=SwarmTaskBudget(max_usd=1.0, max_tokens=500))
    role_bounds = role(max_parallelism=1, budget=SwarmTaskBudget(max_usd=2.0, max_tokens=800))
    task = envelope(
        max_parallelism=4,
        timeout_ms=10_000,
        budget=SwarmTaskBudget(max_usd=5.0, max_tokens=2000),
        required_capabilities=["edit"],
    )
    policy = SwarmDispatchPolicy.from_swarm_bounds(
        task,
        swarm=swarm,
        role=role_bounds,
        slice_limit={"max_concurrency": 3, "timeout_ms": 8_000, "max_budget": 4.0},
    )

    assert policy.max_concurrency == 1
    assert policy.timeout_seconds == 8.0
    assert policy.max_budget == 1.0
    assert "edit" in policy.required_capabilities
    assert policy.envelope is task
    assert policy.envelope.max_parallelism == 4


def test_conflict_policy_is_stable_across_repeated_ties() -> None:
    policy = ConflictPolicy(policy_id="conflict", version="1")
    candidates = ((1, {"id": "b"}), (1, {"id": "a"}), (0, {"id": "c"}))
    first = policy.select(candidates)
    for _ in range(100):
        assert policy.select(candidates) == first
    assert first["selected"]["id"] == "a"
    assert first["tie_break"] == "lexical_digest"
