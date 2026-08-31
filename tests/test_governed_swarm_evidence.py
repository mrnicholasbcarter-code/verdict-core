from __future__ import annotations

import pytest

from verdict.receipt_store import ReceiptConflictError, ReceiptStore
from verdict.swarm_evidence import MissionEventType, MissionEvidence


def test_lifecycle_conflict_projection_preserves_order_and_terminal() -> None:
    evidence = MissionEvidence.create(
        ReceiptStore(),
        scope="swarm/demo",
        swarm_id="swarm-1",
        event_id="root",
        contract_version="swarm-spec/v1",
    )
    evidence.append(
        MissionEventType.DISPATCH_ADMITTED,
        event_id="dispatch",
        payload={
            "swarm_id": "swarm-1",
            "slice_id": "slice-1",
            "envelope_digest": "env-1",
            "state": "submitted",
        },
    )
    evidence.append(
        MissionEventType.STATUS_OBSERVED,
        event_id="running",
        payload={
            "swarm_id": "swarm-1",
            "slice_id": "slice-1",
            "envelope_digest": "env-1",
            "state": "running",
        },
    )
    evidence.append(
        MissionEventType.CONFLICT_RESOLVED,
        event_id="conflict",
        payload={
            "swarm_id": "swarm-1",
            "slice_id": "slice-1",
            "envelope_digest": "env-1",
            "policy_id": "policy",
            "policy_version": "v1",
            "candidate_digests": ["sha256:a", "sha256:b"],
            "selected_digest": "sha256:a",
            "tie_break": "lexical_digest",
            "decision_digest": "sha256:decision",
        },
    )
    evidence.append(
        MissionEventType.MISSION_COMPLETED,
        event_id="terminal",
        payload={
            "swarm_id": "swarm-1",
            "slice_id": "slice-1",
            "envelope_digest": "env-1",
            "state": "completed",
        },
    )

    projection = evidence.projections()

    assert [item["event_type"] for item in projection["lifecycle"]] == [
        "dispatch_admitted",
        "status_observed",
        "mission_completed",
    ]
    assert projection["conflicts"][0]["payload"]["selected_digest"] == "sha256:a"
    assert projection["terminal"]["event_type"] == "mission_completed"


def test_terminal_conflict_and_evidence_scope_validation() -> None:
    evidence = MissionEvidence.create(
        ReceiptStore(),
        scope="swarm/demo",
        swarm_id="swarm-1",
        event_id="root",
        contract_version="swarm-spec/v1",
    )
    evidence.append(
        MissionEventType.MISSION_COMPLETED, event_id="terminal", payload={"state": "completed"}
    )

    with pytest.raises(ReceiptConflictError):
        evidence.append(
            MissionEventType.MISSION_FAILED, event_id="failed", payload={"state": "failed"}
        )

    other = MissionEvidence.create(
        ReceiptStore(),
        scope="swarm/other",
        swarm_id="swarm-2",
        event_id="root-2",
        contract_version="swarm-spec/v1",
    )
    foreign = other.append(
        MissionEventType.STATUS_OBSERVED, event_id="foreign", payload={"state": "running"}
    )
    with pytest.raises(ValueError, match="outside the mission scope"):
        evidence.evidence_ref(foreign)


def test_supervisor_emits_ordered_lifecycle_and_denial_evidence() -> None:
    from verdict.swarm_contracts import SwarmTaskBudget, SwarmTaskEnvelope
    from verdict.swarm_governance import (
        ConflictPolicy,
        SupervisorPolicy,
        SwarmAgentAssignment,
        SwarmRole,
        SwarmSlice,
        SwarmSpec,
        VerificationProfile,
    )
    from verdict.swarm_runtime import (
        SWARM_RUNTIME_PROTOCOL_VERSION,
        FakeSwarmRuntimeAdapter,
        RuntimeRequest,
    )
    from verdict.swarm_supervisor import SupervisorError, SwarmSupervisor

    profile = VerificationProfile(
        profile_id="verify-core",
        version="1",
        required_checks=("pytest",),
        required_evidence=("test-report",),
    )
    swarm = SwarmSpec(
        swarm_id="swarm-1",
        objective="ship governed swarm contract models",
        roles=(
            SwarmRole(
                role_id="coder",
                name="Coder",
                required_capabilities=("edit",),
                optional_capabilities=("test",),
                forbidden_capabilities=("deploy",),
                allowed_tools=("read_file", "write"),
                resource_refs=("/home/nick/dev/verdict-core/verdict/swarm_governance.py",),
                model_floor="low",
                max_parallelism=1,
                verification=profile,
            ),
        ),
        agents=(
            SwarmAgentAssignment(
                agent_id="agent-1",
                role_id="coder",
                capabilities=("edit", "test"),
                allowed_tools=("read_file",),
                resource_refs=("/home/nick/dev/verdict-core/verdict/swarm_governance.py",),
                model="low",
                slice_id="slice-1",
            ),
        ),
        context_refs=("context-pack:abc",),
        model_constraints={"allowlist": ("low", "mid")},
        budget=SwarmTaskBudget(max_tokens=2000),
        max_concurrency=1,
        conflict_policy=ConflictPolicy(policy_id="conflict", version="1"),
        supervisor=SupervisorPolicy(cancellation_deadline_ms=1000),
        verification=profile,
        evidence_scope="swarm/scope",
    )
    task = SwarmTaskEnvelope(
        task_id="slice-1-task",
        objective="implement bounded slice",
        allowed_paths=["/home/nick/dev/verdict-core/verdict/swarm_governance.py"],
        required_capabilities=["edit"],
        budget=SwarmTaskBudget(max_tokens=1000),
        timeout_ms=1000,
        max_iterations=3,
        max_parallelism=1,
        verification_command="pytest",
    )
    slice_contract = SwarmSlice.from_spec(
        spec=swarm,
        assignment_id="agent-1",
        envelope=task,
        verification=profile,
        evidence_root_id="receipt-root",
        slice_id="slice-1",
    )
    evidence = MissionEvidence.create(
        ReceiptStore(),
        scope="swarm/scope",
        swarm_id="swarm-1",
        event_id="root",
        contract_version="swarm-spec/v1",
    )
    supervisor = SwarmSupervisor(FakeSwarmRuntimeAdapter(), evidence=evidence)
    request = RuntimeRequest(
        request_id="req-1",
        task_id="slice-1",
        objective="run bounded slice",
        protocol_version=SWARM_RUNTIME_PROTOCOL_VERSION,
        swarm_id=slice_contract.swarm_id,
        slice_id=slice_contract.slice_id,
        envelope_digest=slice_contract.envelope_digest,
        approved_bounds=slice_contract.effective_bounds(swarm, 1),
        verification_profile=profile.to_dict(),
        metadata={"evidence_scope": "swarm/scope"},
    )
    supervisor.submit(request)
    supervisor.start("slice-1")
    with pytest.raises(SupervisorError, match="out_of_envelope"):
        supervisor.deny_out_of_envelope("slice-1", tool="shell")
    projection = evidence.projections()
    assert "dispatch_admitted" in [item["event_type"] for item in projection["lifecycle"]]
