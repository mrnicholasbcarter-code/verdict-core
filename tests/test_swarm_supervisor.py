from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from verdict.swarm_runtime import FakeSwarmRuntimeAdapter, RuntimeRequest, RuntimeState
from verdict.swarm_supervisor import SupervisorError, SwarmSupervisor


def test_supervisor_enforces_legal_transitions() -> None:
    supervisor = SwarmSupervisor(FakeSwarmRuntimeAdapter())
    request = RuntimeRequest(request_id="req-1", task_id="task-1", objective="Run slice")

    accepted = supervisor.submit(request)
    running = supervisor.start("task-1")
    completed = supervisor.complete("task-1", output={"ok": True})

    assert [accepted.state, running.state, completed.state] == [
        RuntimeState.SUBMITTED,
        RuntimeState.RUNNING,
        RuntimeState.COMPLETED,
    ]

    with pytest.raises(SupervisorError, match="illegal transition"):
        supervisor.start("task-1")


def test_supervisor_cancellation_deadline_only_narrows() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    adapter = FakeSwarmRuntimeAdapter(now=lambda: now)
    supervisor = SwarmSupervisor(adapter, now=lambda: now)
    request = RuntimeRequest(request_id="req-1", task_id="task-1", objective="Run slice")

    supervisor.submit(request)
    supervisor.start("task-1")
    wide = supervisor.cancel("task-1", deadline_at=now + timedelta(seconds=30))
    narrow = supervisor.cancel("task-1", deadline_at=now + timedelta(seconds=5))
    widened = supervisor.cancel("task-1", deadline_at=now + timedelta(seconds=60))

    assert wide.cancel_deadline_at == (now + timedelta(seconds=30)).isoformat()
    assert narrow.cancel_deadline_at == (now + timedelta(seconds=5)).isoformat()
    assert widened.cancel_deadline_at == narrow.cancel_deadline_at


def test_supervisor_rejects_past_cancel_deadline() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    supervisor = SwarmSupervisor(FakeSwarmRuntimeAdapter(now=lambda: now), now=lambda: now)
    request = RuntimeRequest(request_id="req-1", task_id="task-1", objective="Run slice")

    supervisor.submit(request)
    supervisor.start("task-1")

    with pytest.raises(SupervisorError, match="cancel deadline is in the past"):
        supervisor.cancel("task-1", deadline_at=now - timedelta(seconds=1))


def test_two_roles_complete_one_slice_each_with_fake_runtime() -> None:
    adapter = FakeSwarmRuntimeAdapter()
    supervisor = SwarmSupervisor(adapter)
    roles = (("coder", "slice-code"), ("reviewer", "slice-review"))

    for role_id, slice_id in roles:
        request = RuntimeRequest(
            request_id=f"request-{role_id}",
            task_id=slice_id,
            objective=f"Run the {role_id} slice",
            route_attempts=[{"role_id": role_id, "slice_id": slice_id}],
        )
        assert supervisor.submit(request).state == RuntimeState.SUBMITTED
        assert supervisor.start(slice_id).state == RuntimeState.RUNNING
        completed = supervisor.complete(slice_id, output={"role_id": role_id})

        assert completed.state == RuntimeState.COMPLETED
        assert completed.output == {"role_id": role_id}
        assert completed.route_attempts == [{"role_id": role_id, "slice_id": slice_id}]
        assert supervisor.status(slice_id) == completed
