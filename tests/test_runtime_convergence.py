from datetime import datetime, timedelta, timezone

import pytest

from verdict.swarm_dispatcher import SwarmDispatcher, SwarmDispatchPolicy
from verdict.swarm_runtime import (
    _ALLOWED_TRANSITIONS,
    SWARM_RUNTIME_PROTOCOL_VERSION,
    FakeSwarmRuntimeAdapter,
    RuntimeRequest,
    RuntimeResponse,
    RuntimeState,
    validate_response,
)
from verdict.swarm_supervisor import SupervisorError, SwarmSupervisor


def test_response_rejects_broader_numeric_and_set_bounds() -> None:
    request = RuntimeRequest(
        "req", "task", "work", approved_bounds={"max_concurrency": 2, "tools": ["read"]}
    )
    with pytest.raises(ValueError, match="exceeds approved"):
        validate_response(
            RuntimeResponse(
                "req",
                "task",
                RuntimeState.RUNNING,
                metadata={"observed_bounds": {"max_concurrency": 3}},
            ),
            request,
        )
    with pytest.raises(ValueError, match="exceeds approved"):
        validate_response(
            RuntimeResponse(
                "req",
                "task",
                RuntimeState.RUNNING,
                metadata={"observed_bounds": {"tools": ["read", "write"]}},
            ),
            request,
        )


def test_supervisor_rejects_skip_transition_and_deadline_failure() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    adapter = FakeSwarmRuntimeAdapter(now=lambda: now)
    supervisor = SwarmSupervisor(adapter, now=lambda: now)
    supervisor.submit(RuntimeRequest("req", "task", "work"))
    with pytest.raises(SupervisorError, match="illegal transition"):
        supervisor.complete("task", {"ok": True})

    supervisor.start("task")
    adapter._responses["task"] = RuntimeResponse("req", "task", RuntimeState.RUNNING)

    # A non-cancelling adapter response at the exact deadline becomes visible timeout.
    class NoAck(FakeSwarmRuntimeAdapter):
        def cancel(self, task_id, deadline_at=None, *, reason=""):
            return self.status(task_id)

    no_ack = NoAck(now=lambda: now)
    guarded = SwarmSupervisor(no_ack, now=lambda: now)
    guarded.submit(RuntimeRequest("r2", "t2", "work"))
    guarded.start("t2")
    response = guarded.cancel("t2", deadline_at=now)
    assert response.state == RuntimeState.TIMEOUT
    assert response.failure is not None


def test_effective_dispatcher_bounds_take_strictest_limit() -> None:
    class Owner:
        max_parallelism = 3
        timeout_ms = 4_000
        budget = type("Budget", (), {"max_usd": 2.0})()

    policy = SwarmDispatchPolicy()
    bounds = policy.effective_bounds(
        Owner(),
        type("Narrower", (), {"max_concurrency": 1, "timeout_ms": 1_000, "max_budget": 1.0})(),
    )
    assert bounds == {
        "max_concurrency": 1,
        "timeout_ms": 1000.0,
        "max_usd": 1.0,
        "max_queue_depth": 100,
    }


def test_dispatcher_construction_applies_all_narrowing_limits_and_finite_capacity() -> None:
    class Limit:
        required_capabilities: tuple[str, ...] = ()
        max_parallelism = 4
        timeout_ms = 5_000
        max_queue_depth = 8
        budget = type("Budget", (), {"max_usd": 4.0})()

    role = type(
        "RoleLimit",
        (),
        {"max_concurrency": 3, "timeout_ms": 4_000, "max_queue_depth": 6, "max_budget": 3.0},
    )()
    slice_limit = type(
        "SliceLimit",
        (),
        {"max_concurrency": 2, "timeout_ms": 3_000, "max_queue_depth": 2, "max_budget": 2.0},
    )()
    runtime = type(
        "RuntimeLimit",
        (),
        {"max_concurrency": 1, "timeout_ms": 2_000, "max_queue_depth": 4, "max_budget": 1.0},
    )()

    policy = SwarmDispatchPolicy(envelope=Limit(), narrowing_limits=(role, slice_limit, runtime))
    dispatcher = SwarmDispatcher(policy)

    assert policy.max_concurrency == 1
    assert policy.timeout_seconds == 2.0
    assert policy.max_budget == 1.0
    assert dispatcher.fan_out.max_concurrent == 1
    assert dispatcher.fan_out.max_queue_depth == 2
    assert dispatcher.fan_out.try_acquire() is True
    assert dispatcher.fan_out.try_acquire() is False
    assert dispatcher.fan_out.is_backpressured() is True


def test_portable_runtime_contract_requires_immutable_slice_identity_and_bounds() -> None:
    with pytest.raises(ValueError, match="swarm_id"):
        RuntimeRequest("req", "task", "work", protocol_version=SWARM_RUNTIME_PROTOCOL_VERSION)

    request = RuntimeRequest(
        "req",
        "task",
        "work",
        protocol_version=SWARM_RUNTIME_PROTOCOL_VERSION,
        swarm_id="swarm-1",
        slice_id="slice-1",
        envelope_digest="sha256:envelope",
        approved_bounds={"max_concurrency": 1},
        verification_profile={"required_checks": ["tests"]},
        metadata={"evidence_scope": "swarm/demo"},
    )
    response = RuntimeResponse(
        "req", "task", RuntimeState.SUBMITTED, protocol_version=SWARM_RUNTIME_PROTOCOL_VERSION
    )

    with pytest.raises(ValueError, match="response swarm_id is required"):
        validate_response(response, request)

    incomplete = RuntimeResponse(
        "req",
        "task",
        RuntimeState.SUBMITTED,
        protocol_version=SWARM_RUNTIME_PROTOCOL_VERSION,
        swarm_id="swarm-1",
        slice_id="slice-1",
        envelope_digest="sha256:envelope",
    )
    with pytest.raises(ValueError, match="operation_id is required"):
        validate_response(incomplete, request)


def test_portable_response_must_preserve_protocol_bounds_verification_and_scope() -> None:
    request = RuntimeRequest(
        "req",
        "task",
        "work",
        protocol_version=SWARM_RUNTIME_PROTOCOL_VERSION,
        swarm_id="swarm-1",
        slice_id="slice-1",
        envelope_digest="sha256:envelope",
        approved_bounds={"max_concurrency": 1},
        verification_profile={"required_checks": ["tests"]},
        metadata={"evidence_scope": "swarm/demo"},
    )

    def response(**metadata: object) -> RuntimeResponse:
        return RuntimeResponse(
            "req",
            "task",
            RuntimeState.RUNNING,
            protocol_version=SWARM_RUNTIME_PROTOCOL_VERSION,
            swarm_id="swarm-1",
            slice_id="slice-1",
            envelope_digest="sha256:envelope",
            operation_id="op-1",
            evidence_ref="swarm/demo:event-1",
            metadata=metadata,
        )

    with pytest.raises(ValueError, match="observed_bounds missing"):
        validate_response(response(evidence_scope="swarm/demo"), request)
    with pytest.raises(ValueError, match="verification status is required"):
        validate_response(
            response(observed_bounds={"max_concurrency": 1}, evidence_scope="swarm/demo"), request
        )
    with pytest.raises(ValueError, match="evidence scope"):
        validate_response(
            response(
                observed_bounds={"max_concurrency": 1},
                verification_status="passed",
                evidence_scope="swarm/other",
            ),
            request,
        )


def test_swarm_runtime_v1_transition_table_matches_normative_contract() -> None:
    assert "accepted" not in {state.value for state in RuntimeState}
    assert "timed_out" not in {state.value for state in RuntimeState}
    assert {
        RuntimeState.PENDING: frozenset(
            {RuntimeState.SUBMITTED, RuntimeState.REJECTED, RuntimeState.CANCELLED}
        ),
        RuntimeState.SUBMITTED: frozenset(
            {
                RuntimeState.QUEUED,
                RuntimeState.RUNNING,
                RuntimeState.FAILED,
                RuntimeState.CANCELLED,
                RuntimeState.TIMEOUT,
            }
        ),
        RuntimeState.QUEUED: frozenset(
            {
                RuntimeState.RUNNING,
                RuntimeState.FAILED,
                RuntimeState.CANCELLED,
                RuntimeState.TIMEOUT,
            }
        ),
        RuntimeState.RUNNING: frozenset(
            {
                RuntimeState.PAUSED,
                RuntimeState.COMPLETED,
                RuntimeState.FAILED,
                RuntimeState.CANCELLED,
                RuntimeState.TIMEOUT,
            }
        ),
        RuntimeState.PAUSED: frozenset(
            {
                RuntimeState.RUNNING,
                RuntimeState.FAILED,
                RuntimeState.CANCELLED,
                RuntimeState.TIMEOUT,
            }
        ),
    } == _ALLOWED_TRANSITIONS


def test_cancellation_uses_explicit_monotonic_deadline_with_zero_tolerance() -> None:
    wall_now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    class MonotonicClock:
        value = 10.0

        def __call__(self) -> float:
            return self.value

        def advance(self, seconds: float) -> None:
            self.value += seconds

    monotonic = MonotonicClock()

    class NoAckAtDeadline(FakeSwarmRuntimeAdapter):
        def cancel(
            self, task_id: str, deadline_at: datetime | None = None, *, reason: str = ""
        ) -> RuntimeResponse:
            monotonic.advance(5.0)
            return self.status(task_id)

    adapter = NoAckAtDeadline(now=lambda: wall_now)
    supervisor = SwarmSupervisor(adapter, now=lambda: wall_now, monotonic=monotonic)
    supervisor.submit(RuntimeRequest("req", "task", "work"))
    supervisor.start("task")

    response = supervisor.cancel("task", deadline_at=wall_now + timedelta(seconds=5))

    assert response.state == RuntimeState.TIMEOUT
    assert response.failure is not None
    assert response.failure.code == "cancellation_deadline_exceeded"


def test_portable_supervisor_preserves_identity_bounds_verification_and_scope() -> None:
    request = RuntimeRequest(
        "req",
        "task",
        "work",
        protocol_version=SWARM_RUNTIME_PROTOCOL_VERSION,
        swarm_id="swarm-1",
        slice_id="slice-1",
        envelope_digest="sha256:envelope",
        approved_bounds={"max_concurrency": 1},
        verification_profile={"required_checks": ["tests"]},
        metadata={"evidence_scope": "swarm/demo"},
    )
    supervisor = SwarmSupervisor(FakeSwarmRuntimeAdapter())

    submitted = supervisor.submit(request)
    running = supervisor.start("task")
    paused = supervisor.pause("task", reason="checkpoint")
    resumed = supervisor.resume("task", reason="continue")

    for response in (submitted, running, paused, resumed, supervisor.status("task")):
        assert response.swarm_id == request.swarm_id
        assert response.slice_id == request.slice_id
        assert response.envelope_digest == request.envelope_digest
        assert response.metadata["observed_bounds"] == request.approved_bounds
        assert response.metadata["verification_status"] == "pending"
        assert response.metadata["evidence_scope"] == "swarm/demo"
