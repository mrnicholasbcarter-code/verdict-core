from __future__ import annotations

from verdict.contracts import TaskSpec
from verdict.ruflo_adapter import TaskStatus, build_fake_ruflo_adapter
from verdict.swarm_contracts import SwarmTaskBudget, SwarmTaskEnvelope
from verdict.swarm_dispatcher import SwarmDispatcher, SwarmDispatchPolicy


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
    sub = adapter.submit(
        task_spec=TaskSpec(objective="Run slice task", task_type="codegen")
    )

    paused = adapter.pause(sub.task_id)
    assert paused.success is False

    # Simulate transition to running before pause
    adapter._fake_state[sub.task_id]["status"] = TaskStatus.RUNNING
    paused_running = adapter.pause(sub.task_id)
    assert paused_running.success is True
    assert paused_running.new_status == TaskStatus.PAUSED
