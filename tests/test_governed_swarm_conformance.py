from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import pytest

from verdict.receipt_store import ReceiptStore
from verdict.ruflo_adapter import RufloAdapter, RufloAdapterConfig
from verdict.swarm_evidence import MissionEventType, MissionEvidence
from verdict.swarm_runtime import (
    FakeSwarmRuntimeAdapter,
    RuntimeRequest,
    RuntimeResponse,
    RuntimeState,
)


class RuntimeHarness(Protocol):
    def submit(self, request: RuntimeRequest) -> RuntimeResponse: ...
    def status(self, request: RuntimeRequest, task_id: str) -> RuntimeResponse: ...
    def control(self, request: RuntimeRequest, task_id: str, action: str) -> RuntimeResponse: ...
    def result(self, request: RuntimeRequest, task_id: str) -> RuntimeResponse: ...
    def verify(self, request: RuntimeRequest, task_id: str) -> RuntimeResponse: ...


@dataclass
class CoreHarness:
    adapter: FakeSwarmRuntimeAdapter

    def submit(self, request: RuntimeRequest) -> RuntimeResponse:
        return self.adapter.submit(request)

    def status(self, request: RuntimeRequest, task_id: str) -> RuntimeResponse:
        return self.adapter.status(task_id)

    def control(self, request: RuntimeRequest, task_id: str, action: str) -> RuntimeResponse:
        if action == "pause":
            return self.adapter.pause(task_id)
        if action == "resume":
            return self.adapter.resume(task_id)
        return self.adapter.cancel(task_id)

    def result(self, request: RuntimeRequest, task_id: str) -> RuntimeResponse:
        return self.adapter.result(task_id)

    def verify(self, request: RuntimeRequest, task_id: str) -> RuntimeResponse:
        return self.adapter.verify(task_id, {"required_checks": [{"id": "tests", "passed": True}]})


class DeterministicRufloTransport:
    def __init__(self) -> None:
        self.status_by_task: dict[str, str] = {}

    def __call__(self, envelope: dict[str, object]) -> dict[str, object]:
        method = envelope["method"]
        params = envelope["params"]
        assert isinstance(params, dict)
        if method == "submit":
            task_id = "ruflo-task-1"
            self.status_by_task[task_id] = "queued"
            return {"task_id": task_id, "workflow_id": None, "status": "queued", "accepted": True}
        task_id = str(params["task_id"])
        if method == "status":
            return {
                "task_id": task_id,
                "status": self.status_by_task.get(task_id, "queued"),
                "verification_results": [{"check": "tests", "outcome": "pass"}],
            }
        if method == "control":
            action = params["action"]
            current = self.status_by_task.get(task_id, "queued")
            next_status = {"pause": "paused", "resume": "running", "cancel": "cancelled"}[
                str(action)
            ]
            self.status_by_task[task_id] = next_status
            return {
                "task_id": task_id,
                "action": action,
                "success": True,
                "previous_status": current,
                "new_status": next_status,
            }
        if method == "result":
            self.status_by_task[task_id] = "completed"
            return {
                "task_id": task_id,
                "status": "completed",
                "outcome": "success",
                "verification_passed": True,
                "verification_results": [{"check": "tests", "outcome": "pass"}],
            }
        raise AssertionError(method)


@dataclass
class RufloHarness:
    adapter: RufloAdapter

    def submit(self, request: RuntimeRequest) -> RuntimeResponse:
        return self.adapter.submit_runtime(request)

    def status(self, request: RuntimeRequest, task_id: str) -> RuntimeResponse:
        return self.adapter.runtime_status(request, task_id)

    def control(self, request: RuntimeRequest, task_id: str, action: str) -> RuntimeResponse:
        return self.adapter.runtime_control(request, task_id, action)

    def result(self, request: RuntimeRequest, task_id: str) -> RuntimeResponse:
        return self.adapter.runtime_result(request, task_id)

    def verify(self, request: RuntimeRequest, task_id: str) -> RuntimeResponse:
        return self.adapter.runtime_verify(request, task_id)


def _core_harness() -> RuntimeHarness:
    return CoreHarness(FakeSwarmRuntimeAdapter())


def _ruflo_harness() -> RuntimeHarness:
    return RufloHarness(
        RufloAdapter(RufloAdapterConfig(fake_mode=False), DeterministicRufloTransport())
    )


@pytest.mark.parametrize(
    ("name", "factory"), [("Core", _core_harness), ("Ruflo", _ruflo_harness)], ids=["Core", "Ruflo"]
)
def test_core_vs_ruflo_seven_operation_conformance(
    name: str, factory: Callable[[], RuntimeHarness]
) -> None:
    harness = factory()
    request = RuntimeRequest(
        "op-1",
        "task-1",
        "bounded work",
        swarm_id="swarm-1",
        slice_id="slice-1",
        envelope_digest="env-1",
        approved_bounds={"max_concurrency": 1, "tools": ["read_file"]},
        verification_profile={
            "required_checks": [{"id": "tests", "passed": True}],
            "required_evidence": ["swarm/demo:evidence-1"],
        },
        metadata={"evidence_scope": "swarm/demo"},
    )

    submitted = harness.submit(request)
    task_id = submitted.task_id
    assert submitted.state in {RuntimeState.SUBMITTED, RuntimeState.QUEUED}
    assert harness.status(request, task_id).state in {
        RuntimeState.SUBMITTED,
        RuntimeState.QUEUED,
        RuntimeState.RUNNING,
    }
    assert harness.control(request, task_id, "pause").state in {
        RuntimeState.PAUSED,
        RuntimeState.FAILED,
    }
    assert harness.control(request, task_id, "resume").state in {
        RuntimeState.RUNNING,
        RuntimeState.FAILED,
    }
    assert harness.control(request, task_id, "cancel").state in {
        RuntimeState.CANCELLED,
        RuntimeState.FAILED,
    }
    assert harness.result(request, task_id).state in {
        RuntimeState.COMPLETED,
        RuntimeState.CANCELLED,
    }
    assert harness.verify(request, task_id).failure is None


def test_two_role_one_slice_demonstration_links_lifecycle_denial_and_verification_evidence() -> (
    None
):
    evidence = MissionEvidence.create(
        ReceiptStore(),
        scope="swarm/demo",
        swarm_id="swarm-1",
        event_id="root",
        contract_version="swarm-spec/v1",
    )
    evidence.append(
        MissionEventType.DISPATCH_ADMITTED,
        event_id="submit",
        payload={
            "swarm_id": "swarm-1",
            "slice_id": "slice-1",
            "envelope_digest": "env-1",
            "state": "submitted",
        },
    )
    denial = evidence.append(
        MissionEventType.CAPABILITY_DENIED,
        event_id="deny-tool",
        payload={
            "swarm_id": "swarm-1",
            "slice_id": "slice-1",
            "envelope_digest": "env-1",
            "category": "out_of_envelope",
            "code": "tool_denied",
            "resource_ref": "approved://tool/write",
        },
    )
    verification = evidence.append(
        MissionEventType.VERIFICATION,
        event_id="verify",
        payload={
            "swarm_id": "swarm-1",
            "slice_id": "slice-1",
            "envelope_digest": "env-1",
            "check_id": "tests",
            "passed": True,
            "evidence_ref": evidence.evidence_ref(denial),
        },
    )

    projection = evidence.projections()

    assert verification.payload["evidence_ref"] == denial.receipt_id
    assert all(item["payload"].get("slice_id") == "slice-1" for item in projection["lifecycle"])
    assert projection["lifecycle"][-1]["payload"]["passed"] is True


def test_schema_round_trip_for_swarm_spec_and_runtime_v1() -> None:
    from verdict.swarm_contracts import SwarmTaskBudget
    from verdict.swarm_governance import (
        ConflictPolicy,
        SupervisorPolicy,
        SwarmAgentAssignment,
        SwarmRole,
        SwarmSpec,
        VerificationProfile,
    )
    from verdict.swarm_runtime import SWARM_RUNTIME_PROTOCOL_VERSION

    profile = VerificationProfile(
        profile_id="verify-core",
        version="1",
        required_checks=("pytest",),
        required_evidence=("test-report",),
    )
    spec = SwarmSpec(
        swarm_id="swarm-1",
        objective="ship governed swarm contract models",
        roles=(
            SwarmRole(
                role_id="coder",
                name="Coder",
                required_capabilities=("edit",),
                optional_capabilities=("test",),
                forbidden_capabilities=("deploy",),
                allowed_tools=("read_file",),
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
                model="low",
                slice_id="slice-1",
            ),
        ),
        context_refs=("context-pack:abc",),
        model_constraints={"allowlist": ("low",)},
        budget=SwarmTaskBudget(max_tokens=1000),
        max_concurrency=1,
        conflict_policy=ConflictPolicy(policy_id="conflict", version="1"),
        supervisor=SupervisorPolicy(cancellation_deadline_ms=1000),
        verification=profile,
        evidence_scope="swarm/demo",
    )
    restored = SwarmSpec.from_dict(spec.to_dict())
    assert restored.digest() == spec.digest()
    request = RuntimeRequest(
        request_id="req-1",
        task_id="slice-1",
        objective="run",
        protocol_version=SWARM_RUNTIME_PROTOCOL_VERSION,
        swarm_id="swarm-1",
        slice_id="slice-1",
        envelope_digest="sha256:abc",
        approved_bounds={"max_concurrency": 1},
        verification_profile=profile.to_dict(),
        metadata={"evidence_scope": "swarm/demo"},
    )
    assert RuntimeRequest.from_dict(request.to_dict()) == request


def test_two_roles_one_slice_execute_and_deny_out_of_envelope() -> None:
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
    from verdict.swarm_runtime import SWARM_RUNTIME_PROTOCOL_VERSION
    from verdict.swarm_supervisor import SupervisorError, SwarmSupervisor

    profile = VerificationProfile(
        profile_id="verify-core",
        version="1",
        required_checks=("pytest",),
        required_evidence=("test-report",),
    )
    coder = SwarmRole(
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
    )
    reviewer = SwarmRole(
        role_id="reviewer",
        name="Reviewer",
        required_capabilities=("test",),
        optional_capabilities=("edit",),
        forbidden_capabilities=("deploy",),
        allowed_tools=("read_file",),
        resource_refs=("/home/nick/dev/verdict-core/verdict/swarm_governance.py",),
        model_floor="low",
        max_parallelism=1,
        verification=profile,
    )
    swarm = SwarmSpec(
        swarm_id="swarm-1",
        objective="two roles one slice",
        roles=(coder, reviewer),
        agents=(
            SwarmAgentAssignment(
                agent_id="agent-coder",
                role_id="coder",
                capabilities=("edit", "test"),
                allowed_tools=("read_file",),
                resource_refs=("/home/nick/dev/verdict-core/verdict/swarm_governance.py",),
                model="low",
                slice_id="slice-1",
            ),
            SwarmAgentAssignment(
                agent_id="agent-reviewer",
                role_id="reviewer",
                capabilities=("test",),
                allowed_tools=("read_file",),
                resource_refs=("/home/nick/dev/verdict-core/verdict/swarm_governance.py",),
                model="low",
                slice_id="slice-1",
            ),
        ),
        context_refs=("context-pack:abc",),
        model_constraints={"allowlist": ("low",)},
        budget=SwarmTaskBudget(max_tokens=2000),
        max_concurrency=2,
        conflict_policy=ConflictPolicy(policy_id="conflict", version="1"),
        supervisor=SupervisorPolicy(cancellation_deadline_ms=1000),
        verification=profile,
        evidence_scope="swarm/demo",
    )
    envelope = SwarmTaskEnvelope(
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
        assignment_id="agent-coder",
        envelope=envelope,
        verification=profile,
        evidence_root_id="receipt-root",
        slice_id="slice-1",
    )
    evidence = MissionEvidence.create(
        ReceiptStore(),
        scope="swarm/demo",
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
        metadata={"evidence_scope": "swarm/demo"},
    )
    supervisor.submit(request)
    supervisor.start("slice-1")
    with pytest.raises(SupervisorError, match="out_of_envelope"):
        supervisor.deny_out_of_envelope("slice-1", tool="shell")
    types = [item["event_type"] for item in evidence.projections()["lifecycle"]]
    assert "dispatch_admitted" in types
    assert "capability_denied" in types
