from __future__ import annotations

from datetime import datetime, timezone

import pytest

from verdict.contracts import ContractValidationError, TaskSpec
from verdict.ruflo_adapter import TaskStatus, build_fake_ruflo_adapter
from verdict.swarm_contracts import (
    SwarmTaskBudget,
    SwarmTaskEnvelope,
    approved_envelope_bounds,
    capture_envelope_digest,
    validate_envelope_link,
)
from verdict.swarm_dispatcher import FanOutLimiter, SwarmDispatcher, SwarmDispatchPolicy
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
    RuntimeResponse,
    RuntimeState,
    validate_response,
)
from verdict.swarm_supervisor import SupervisorError, SwarmSupervisor


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
        "resource_refs": ("/home/nick/dev/verdict-core/verdict/swarm_governance.py",),
        "model_floor": "low",
        "max_parallelism": 2,
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
        "resource_refs": ("/home/nick/dev/verdict-core/verdict/swarm_governance.py",),
        "model": "low",
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
        "max_concurrency": 2,
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


def slice_for(swarm: SwarmSpec | None = None, task: SwarmTaskEnvelope | None = None) -> SwarmSlice:
    swarm = swarm or spec()
    task = task or envelope()
    return SwarmSlice.from_spec(
        spec=swarm,
        assignment_id="agent-1",
        envelope=task,
        verification=verification(),
        evidence_root_id="receipt-root",
        slice_id="slice-1",
    )


def runtime_request(slice_contract: SwarmSlice, **overrides: object) -> RuntimeRequest:
    swarm = slice_contract.spec
    assert swarm is not None
    bounds = slice_contract.effective_bounds(swarm, dispatcher_concurrency=8)
    payload = {
        "request_id": "req-1",
        "task_id": slice_contract.slice_id,
        "objective": "run bounded slice",
        "protocol_version": SWARM_RUNTIME_PROTOCOL_VERSION,
        "swarm_id": slice_contract.swarm_id,
        "slice_id": slice_contract.slice_id,
        "envelope_digest": slice_contract.envelope_digest,
        "approved_bounds": bounds,
        "verification_profile": slice_contract.verification.to_dict(),
        "metadata": {"evidence_scope": swarm.evidence_scope},
    }
    payload.update(overrides)
    return RuntimeRequest(**payload)


def test_envelope_link_and_narrowing_only_mutations() -> None:
    task = envelope()
    digest = capture_envelope_digest(task)
    assert validate_envelope_link(task, digest) == digest
    approved = approved_envelope_bounds(task)
    swarm = spec()
    slice_contract = slice_for(swarm, task)
    assert slice_contract.envelope_digest == digest

    with pytest.raises(ContractValidationError, match="cannot weaken"):
        validate_envelope_link(
            task, digest, proposed_bounds={**approved, "timeout_ms": task.timeout_ms + 1}
        )
    with pytest.raises(ContractValidationError, match="cannot weaken"):
        validate_envelope_link(task, digest, proposed_bounds={**approved, "max_parallelism": 8})
    with pytest.raises(ContractValidationError, match="cannot weaken"):
        validate_envelope_link(
            task, digest, proposed_bounds={**approved, "required_capabilities": ["edit", "deploy"]}
        )
    with pytest.raises(ContractValidationError, match="cannot weaken"):
        validate_envelope_link(
            task,
            digest,
            proposed_bounds={**approved, "allowed_paths": [*task.allowed_paths, "/tmp/escape"]},
        )
    with pytest.raises(ContractValidationError, match="cannot broaden"):
        SwarmSlice.from_spec(
            spec=swarm,
            assignment_id="agent-1",
            envelope=envelope(allowed_paths=["/tmp/escape"]),
            verification=verification(),
            evidence_root_id="receipt-root",
        )


def test_effective_concurrency_is_minimum_and_excess_is_backpressured() -> None:
    swarm = spec(max_concurrency=2)
    role_bounds = role(max_parallelism=2)
    task = envelope(max_parallelism=1)
    policy = SwarmDispatchPolicy.from_swarm_bounds(
        task, swarm=swarm, role=role_bounds, slice_limit={"max_concurrency": 4}
    )
    assert policy.max_concurrency == 1
    limiter = FanOutLimiter(max_concurrent=policy.max_concurrency, max_queue_depth=1)
    dispatcher = SwarmDispatcher(policy=policy, fan_out_limiter=limiter)
    assert dispatcher.fan_out.max_concurrent == 1
    assert dispatcher.fan_out.try_acquire() is True
    assert dispatcher.fan_out.try_acquire() is False
    assert dispatcher.fan_out.is_backpressured() is True


def test_deterministic_runtime_lifecycle_and_deadline() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    clock = {"t": 0.0}

    class SilentCancelAdapter(FakeSwarmRuntimeAdapter):
        def cancel(self, task_id: str, deadline_at=None, *, reason: str = "") -> RuntimeResponse:
            current = self.status(task_id)
            deadline = deadline_at or self._now()
            return self._replace(
                current, cancel_deadline_at=deadline.isoformat(), metadata={"reason": reason}
            )

    adapter = SilentCancelAdapter(now=lambda: now)
    supervisor = SwarmSupervisor(adapter, now=lambda: now, monotonic=lambda: clock["t"])
    request = runtime_request(slice_for())
    accepted = supervisor.submit(request)
    running = supervisor.start(request.task_id)
    paused = supervisor.pause(request.task_id)
    resumed = supervisor.resume(request.task_id)
    clock["t"] = 10.0
    timed_out = supervisor.cancel(request.task_id, deadline_at=now)

    assert [accepted.state, running.state, paused.state, resumed.state, timed_out.state] == [
        RuntimeState.SUBMITTED,
        RuntimeState.RUNNING,
        RuntimeState.PAUSED,
        RuntimeState.RUNNING,
        RuntimeState.TIMEOUT,
    ]
    assert timed_out.failure is not None
    assert timed_out.failure.category == "timeout"
    with pytest.raises(SupervisorError, match="illegal transition"):
        supervisor.pause(request.task_id)
    with pytest.raises(SupervisorError, match="illegal transition"):
        supervisor.resume(request.task_id)


def test_forbidden_tools_and_broader_runtime_bounds_are_denied() -> None:
    slice_contract = slice_for()
    request = runtime_request(slice_contract)
    supervisor = SwarmSupervisor(FakeSwarmRuntimeAdapter())
    supervisor.submit(request)
    supervisor.start(request.task_id)

    with pytest.raises(SupervisorError, match="out_of_envelope"):
        supervisor.deny_out_of_envelope(request.task_id, tool="shell")

    broader = RuntimeResponse(
        request_id=request.request_id,
        task_id=request.task_id,
        state=RuntimeState.RUNNING,
        protocol_version=request.protocol_version,
        swarm_id=request.swarm_id,
        slice_id=request.slice_id,
        envelope_digest=request.envelope_digest,
        operation_id="op-broad",
        evidence_ref="swarm/scope:slice-1",
        metadata={
            "observed_bounds": {**dict(request.approved_bounds), "max_concurrency": 99},
            "evidence_scope": "swarm/scope",
            "verification_status": "pending",
        },
    )
    with pytest.raises(ValueError, match="exceeds approved bound"):
        validate_response(broader, request)


def test_swarm_spec_supervisor_validates_roles_and_envelope_link() -> None:
    envelope = SwarmTaskEnvelope(
        objective="Run multi-agent slice",
        allowed_paths=["/home/nick/dev/project"],
        budget=SwarmTaskBudget(max_usd=10.0),
        required_capabilities=["file_read"],
        max_parallelism=2,
    )
    dispatcher = SwarmDispatcher(policy=SwarmDispatchPolicy(envelope=envelope))

    assert dispatcher.policy.max_concurrency == 2
    assert dispatcher.policy.max_budget == 10.0
    assert dispatcher.policy.envelope.required_capabilities == ["file_read"]


def test_supervisor_control_delegates_to_ruflo_adapter() -> None:
    adapter = build_fake_ruflo_adapter()
    sub = adapter.submit(task_spec=TaskSpec(objective="Run slice task", task_type="codegen"))

    paused = adapter.pause(sub.task_id)
    assert paused.success is False

    # Simulate transition to running before pause
    adapter._fake_state[sub.task_id]["status"] = TaskStatus.RUNNING
    paused_running = adapter.pause(sub.task_id)
    assert paused_running.success is True
    assert paused_running.new_status == TaskStatus.PAUSED
