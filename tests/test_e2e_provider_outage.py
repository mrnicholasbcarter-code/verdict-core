"""End-to-end: a provider goes down mid-workflow and the workflow still finishes.

Issue #330. This is deliberately not a unit test of any one component. It drives
a multi-step workflow through the real routing entry point
(:func:`verdict.decision_kernel.decide`), executes each step against a recording
executor, kills the bound provider partway through, and then asserts the two
properties that make failover worth having:

* **zero step re-execution** -- work that already completed is never run again,
  and the failed step runs exactly once more, not from the top of the workflow;
* **zero context loss** -- every artifact recorded before the outage survives
  the rebind verbatim, including across a cold ``ExecutionSession.resume``.

Everything here is offline: no provider, no credential, no network.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from verdict.availability import AvailabilityCandidate, AvailabilityReport, AvailabilityState
from verdict.contracts import TaskSpec
from verdict.decision_kernel import (
    OUTCOME_ACCEPTED,
    OUTCOME_DEGRADED,
    DecisionRecord,
    decide,
    verify_decision,
)
from verdict.execution_session import ExecutionSession, FailureEntry
from verdict.failover_engine import FailoverEngine
from verdict.memory_plane import MemoryPlane
from verdict.models import ModelInfo

POLICY_VERSION = "1"

# (error_class, status_code) pairs a real outage presents as.
OUTAGE_MODES = [
    pytest.param("rate_limited", 429, id="429-rate-limited"),
    pytest.param("server_error", 500, id="500-server-error"),
    pytest.param("timeout", None, id="timeout"),
]

WORKFLOW: list[tuple[str, str]] = [
    ("s1", "gather the diff"),
    ("s2", "review the diff"),
    ("s3", "draft the summary"),
    ("s4", "publish the summary"),
]

# Mid-workflow on purpose: steps before it must survive, steps after it must run.
OUTAGE_STEP = "s3"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """A routing decision that reaches the network is not a decision (NFR-003)."""

    def _forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("network access forbidden (NFR-003, FR-009)")

    monkeypatch.setattr(socket.socket, "connect", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)


def _catalog() -> list[ModelInfo]:
    """Two providers, both qualified for the same work, so a rebind is possible."""
    return [
        ModelInfo(
            id="alpha/pro",
            provider="alpha",
            capability_tier=3,
            capabilities=frozenset({"code-review"}),
        ),
        ModelInfo(
            id="beta/pro",
            provider="beta",
            capability_tier=3,
            capabilities=frozenset({"code-review"}),
        ),
    ]


def _task() -> TaskSpec:
    return TaskSpec(
        objective="Review a small diff and summarize it",
        task_type="review",
        required_capabilities=["code-review"],
        privacy="standard",
        risk="low",
    )


def _report(model: ModelInfo, state: AvailabilityState) -> AvailabilityReport:
    candidates = (AvailabilityCandidate(model=model, state=state, source="fixture"),)
    return AvailabilityReport(
        candidates=candidates,
        eligible=candidates if state is AvailabilityState.READY else (),
        source="fixture",
        freshness_seconds=0.0,
    )


def _healthy(catalog: list[ModelInfo]) -> dict[str, AvailabilityReport]:
    return {model.id: _report(model, AvailabilityState.READY) for model in catalog}


@dataclass(frozen=True)
class Call:
    """One executor invocation, successful or not."""

    step_id: str
    model_id: str
    ok: bool


class RecordingExecutor:
    """Runs workflow steps and records every attempt, so re-execution is visible."""

    def __init__(self, *, outage_step: str, error_class: str) -> None:
        self.calls: list[Call] = []
        self._outage_step = outage_step
        self._error_class = error_class
        self._down: set[str] = set()

    def take_down(self, model_id: str) -> None:
        """The provider stays down for the rest of the workflow, not just one call."""
        self._down.add(model_id)

    def outage_begins(self, step_id: str) -> bool:
        return step_id == self._outage_step

    def run(self, step_id: str, model_id: str) -> str:
        """Return the step's output, or raise if the bound provider is out."""
        failing = model_id in self._down or (step_id == self._outage_step and not self._down)
        self.calls.append(Call(step_id=step_id, model_id=model_id, ok=not failing))
        if failing:
            raise RuntimeError(f"{self._error_class} from {model_id}")
        return f"{step_id}:done-by:{model_id}"

    def successes(self) -> list[Call]:
        return [call for call in self.calls if call.ok]

    def attempts_for(self, step_id: str) -> list[Call]:
        return [call for call in self.calls if call.step_id == step_id]


def _decide(
    catalog: list[ModelInfo], truth: dict[str, AvailabilityReport], task: TaskSpec
) -> DecisionRecord:
    """Route one step through the real decision kernel, and verify the decision."""
    record = decide(
        task_spec=task,
        policy_version=POLICY_VERSION,
        candidates=catalog,
        availability_truth=truth,
        protected=False,
    )
    fault = verify_decision(
        record,
        task_spec=task,
        policy_version=POLICY_VERSION,
        candidates=catalog,
        availability_truth=truth,
        protected=False,
    )
    assert fault is None, f"decision failed independent verification: {fault}"
    return record


def _route(
    catalog: list[ModelInfo], truth: dict[str, AvailabilityReport], task: TaskSpec
) -> ModelInfo:
    """Pick the model the kernel binds this step to.

    A workflow that has already lost a provider routes ``degraded`` rather than
    ``accepted`` -- fewer qualified candidates remain -- and that is still a
    routable decision. Only a denial would stop the workflow.
    """
    record = _decide(catalog, truth, task)
    assert record.outcome in {OUTCOME_ACCEPTED, OUTCOME_DEGRADED}, (
        f"kernel refused to route while a healthy model existed: {record.outcome}"
    )
    assert record.admitted, "kernel admitted nothing while a healthy model existed"
    return record.admitted[0]


def _drive_workflow(
    plane: MemoryPlane,
    session: ExecutionSession,
    executor: RecordingExecutor,
    catalog: list[ModelInfo],
    truth: dict[str, AvailabilityReport],
    task: TaskSpec,
    *,
    error_class: str,
    status_code: int | None,
) -> str:
    """Run every step, routing each one afresh, and fail over when a step dies.

    Returns the model id the outage removed.
    """
    quarantined = ""
    for step_id, _name in WORKFLOW:
        chosen = _route(catalog, truth, task)
        try:
            output = executor.run(step_id, chosen.id)
        except RuntimeError as exc:
            # An outage is survivable only if it is classified as transient.
            assert FailoverEngine.is_transient_failure(error_class, status_code), (
                f"{error_class} must be transient for failover to be attempted"
            )
            session.fail_step(plane, step_id, error=str(exc), model_id=chosen.id)

            # The provider is out. The availability truth the kernel reads must
            # say so, or the next decision would rebind to the same dead model.
            executor.take_down(chosen.id)
            failed_model = next(model for model in catalog if model.id == chosen.id)
            truth[chosen.id] = _report(failed_model, AvailabilityState.UNAVAILABLE)
            quarantined = chosen.id

            replacement = _route(catalog, truth, task)
            assert replacement.id != chosen.id, "kernel rebound to the model it was told is down"

            session.resume_from_failure(
                plane,
                failure=FailureEntry(
                    step_id=step_id,
                    provider=chosen.provider,
                    model_id=chosen.id,
                    error_class=error_class,
                    message=str(exc),
                    created_at=0.0,
                    status_code=status_code,
                    quarantine_model=chosen.id,
                    replacement_model=replacement.id,
                ),
                replacement_model=replacement.id,
            )
            chosen = replacement
            output = executor.run(step_id, chosen.id)

        session.record_artifact(step_id, output)
        session.complete_step(plane, step_id, model_id=chosen.id)

    assert quarantined, "the outage never fired; the test proves nothing"
    return quarantined


@pytest.mark.parametrize(("error_class", "status_code"), OUTAGE_MODES)
def test_provider_outage_midworkflow_completes_without_re_executing_any_step(
    tmp_path: Path, error_class: str, status_code: int | None
) -> None:
    plane = MemoryPlane(str(tmp_path / "plane.db"))
    catalog = _catalog()
    task = _task()
    truth = _healthy(catalog)
    executor = RecordingExecutor(outage_step=OUTAGE_STEP, error_class=error_class)

    initial = _route(catalog, truth, task)
    session = ExecutionSession.create(
        "sess-330",
        {"objective": task.objective, "task_type": task.task_type},
        steps=list(WORKFLOW),
        plane=plane,
        model_id=initial.id,
    )
    session.start(plane, model_id=initial.id)

    quarantined = _drive_workflow(
        plane,
        session,
        executor,
        catalog,
        truth,
        task,
        error_class=error_class,
        status_code=status_code,
    )

    # The workflow finished despite losing a provider halfway through.
    assert session.state == "completed"
    assert session.completed_steps == [step_id for step_id, _ in WORKFLOW]
    assert not session.remaining_steps

    # Zero step re-execution: every step succeeded exactly once, and only the
    # step that died was attempted twice.
    successes = executor.successes()
    assert [call.step_id for call in successes] == [step_id for step_id, _ in WORKFLOW]
    for step_id, _name in WORKFLOW:
        expected = 2 if step_id == OUTAGE_STEP else 1
        assert len(executor.attempts_for(step_id)) == expected, (
            f"step {step_id} was attempted {len(executor.attempts_for(step_id))} times"
        )
    assert session.attempts == {OUTAGE_STEP: 1}

    # The steps before the outage kept their original binding; the ones after it
    # ran on the replacement. Nothing re-ran on the dead provider.
    by_step = {call.step_id: call.model_id for call in successes}
    assert by_step[OUTAGE_STEP] != quarantined
    # Step ids sort lexically in workflow order, so this is "at or after the outage".
    assert all(call.model_id != quarantined for call in successes if call.step_id >= OUTAGE_STEP)

    # Zero context loss: pre-outage artifacts survived the rebind verbatim.
    for step_id, _name in WORKFLOW:
        assert session.artifacts[step_id] == f"{step_id}:done-by:{by_step[step_id]}"
    assert len(session.failure_log) == 1
    assert session.failure_log[0].step_id == OUTAGE_STEP
    assert session.failure_log[0].error_class == error_class
    assert session.failure_log[0].status_code == status_code


def test_an_outage_downgrades_the_decision_rather_than_hiding_it() -> None:
    """Failover must not launder a degraded catalog into a clean 'accepted'."""
    catalog = _catalog()
    task = _task()
    truth = _healthy(catalog)

    healthy = _decide(catalog, truth, task)
    assert healthy.outcome == OUTCOME_ACCEPTED

    down = healthy.admitted[0]
    truth[down.id] = _report(down, AvailabilityState.UNAVAILABLE)

    degraded = _decide(catalog, truth, task)
    assert degraded.outcome == OUTCOME_DEGRADED
    assert [model.id for model in degraded.admitted] == [
        model.id for model in catalog if model.id != down.id
    ]


@pytest.mark.parametrize(("error_class", "status_code"), OUTAGE_MODES)
def test_a_cold_resume_after_the_outage_loses_no_context(
    tmp_path: Path, error_class: str, status_code: int | None
) -> None:
    """A crashed process must reconstruct the post-failover state exactly."""
    plane = MemoryPlane(str(tmp_path / "plane.db"))
    catalog = _catalog()
    task = _task()
    truth = _healthy(catalog)
    executor = RecordingExecutor(outage_step=OUTAGE_STEP, error_class=error_class)

    initial = _route(catalog, truth, task)
    session = ExecutionSession.create(
        "sess-330-resume",
        {"objective": task.objective, "task_type": task.task_type},
        steps=list(WORKFLOW),
        plane=plane,
        model_id=initial.id,
    )
    session.start(plane, model_id=initial.id)
    _drive_workflow(
        plane,
        session,
        executor,
        catalog,
        truth,
        task,
        error_class=error_class,
        status_code=status_code,
    )

    restored = ExecutionSession.resume("sess-330-resume", plane)

    assert restored.completed_steps == session.completed_steps
    assert restored.artifacts == session.artifacts
    assert restored.attempts == session.attempts
    assert restored.state == session.state
    assert restored.model_id == session.model_id
    assert [entry.to_dict() for entry in restored.failure_log] == [
        entry.to_dict() for entry in session.failure_log
    ]
    # Resuming a finished workflow must not schedule any further execution.
    assert not restored.remaining_steps
    assert len(executor.successes()) == len(WORKFLOW)
