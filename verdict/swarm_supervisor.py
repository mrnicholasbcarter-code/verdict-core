from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from time import monotonic as system_monotonic
from typing import Any

from verdict.swarm_evidence import MissionEventType, MissionEvidence
from verdict.swarm_runtime import (
    RuntimeFailure,
    RuntimeRequest,
    RuntimeResponse,
    RuntimeState,
    SwarmRuntimeAdapter,
    validate_response,
)


class SupervisorError(RuntimeError):
    pass


def _state_value(state: RuntimeState | str) -> str:
    return state.value if isinstance(state, RuntimeState) else str(state)


class SwarmSupervisor:
    def __init__(
        self,
        adapter: SwarmRuntimeAdapter,
        *,
        now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        evidence: MissionEvidence | None = None,
    ) -> None:
        self._adapter = adapter
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic or system_monotonic
        self._evidence = evidence
        self._states: dict[str, RuntimeState] = {}
        self._requests: dict[str, RuntimeRequest] = {}
        self._cancel_deadlines: dict[str, datetime] = {}

    def submit(self, request: RuntimeRequest) -> RuntimeResponse:
        if request.task_id in self._states:
            raise SupervisorError(f"task already submitted: {request.task_id}")
        response = validate_response(self._adapter.submit(request), request)
        self._requests[request.task_id] = request
        self._record(request.task_id, RuntimeState.PENDING, response)
        self._emit(
            MissionEventType.DISPATCH_ADMITTED,
            request.task_id,
            {"state": _state_value(response.state), "operation_id": request.request_id},
        )
        return response

    def start(self, task_id: str) -> RuntimeResponse:
        current = self._state(task_id)
        response = self._validated(task_id, self._adapter.start(task_id))
        self._record(task_id, current, response)
        return response

    def complete(self, task_id: str, output: Mapping[str, Any] | None = None) -> RuntimeResponse:
        current = self._state(task_id)
        response = self._validated(task_id, self._adapter.complete(task_id, output=output))
        self._record(task_id, current, response)
        return response

    def fail(self, task_id: str, failure: RuntimeFailure) -> RuntimeResponse:
        current = self._state(task_id)
        response = self._validated(task_id, self._adapter.fail(task_id, failure))
        self._record(task_id, current, response)
        return response

    def cancel(self, task_id: str, deadline_at: datetime | None = None) -> RuntimeResponse:
        current = self._state(task_id)
        self._check_control(task_id, "cancel")
        deadline = deadline_at or self._now()
        if deadline < self._now():
            raise SupervisorError("cancel deadline is in the past")
        previous = self._cancel_deadlines.get(task_id)
        narrowed = deadline if previous is None else min(previous, deadline)
        remaining_seconds = max(0.0, (narrowed - self._now()).total_seconds())
        monotonic_deadline = self._monotonic() + remaining_seconds
        response = self._validated(task_id, self._adapter.cancel(task_id, deadline_at=narrowed))
        if self._monotonic() >= monotonic_deadline and response.state != RuntimeState.CANCELLED:
            response = RuntimeResponse(
                request_id=response.request_id,
                task_id=response.task_id,
                state=RuntimeState.TIMEOUT,
                protocol_version=response.protocol_version,
                failure=RuntimeFailure(
                    "cancellation_deadline_exceeded",
                    "cancellation was not acknowledged by deadline",
                    category="timeout",
                    retryable=False,
                ),
                metadata=response.metadata,
            )
        self._cancel_deadlines[task_id] = narrowed
        self._record(task_id, current, response)
        self._emit(
            MissionEventType.CANCEL,
            task_id,
            {
                "prior_state": current.value,
                "new_state": _state_value(response.state),
                "category": (
                    None
                    if not isinstance(response.failure, RuntimeFailure)
                    else response.failure.category
                ),
            },
        )
        return response

    def pause(self, task_id: str, *, reason: str = "") -> RuntimeResponse:
        current = self._state(task_id)
        self._check_control(task_id, "pause")
        response = self._validated(task_id, self._adapter.pause(task_id, reason=reason))
        self._record(task_id, current, response)
        self._emit(
            MissionEventType.PAUSE,
            task_id,
            {"prior_state": current.value, "new_state": _state_value(response.state)},
        )
        return response

    def resume(self, task_id: str, *, reason: str = "") -> RuntimeResponse:
        current = self._state(task_id)
        self._check_control(task_id, "resume")
        response = self._validated(task_id, self._adapter.resume(task_id, reason=reason))
        self._record(task_id, current, response)
        self._emit(
            MissionEventType.RESUME,
            task_id,
            {"prior_state": current.value, "new_state": _state_value(response.state)},
        )
        return response

    def result(self, task_id: str) -> RuntimeResponse:
        return self._adapter.result(task_id)

    def verify(self, task_id: str, verification: Mapping[str, Any]) -> RuntimeResponse:
        current = self._state(task_id)
        response = self._validated(task_id, self._adapter.verify(task_id, verification))
        self._record(task_id, current, response)
        return response

    def status(self, task_id: str) -> RuntimeResponse:
        return self._adapter.status(task_id)

    def deny_out_of_envelope(
        self, task_id: str, *, tool: str | None = None, resource: str | None = None
    ) -> RuntimeResponse:
        request = self._requests.get(task_id)
        if request is None:
            raise SupervisorError(f"unknown task_id: {task_id}")
        self._state(task_id)
        reason = f"forbidden tool: {tool}" if tool else f"forbidden resource: {resource}"
        self._emit(
            MissionEventType.CAPABILITY_DENIED,
            task_id,
            {"category": "out_of_envelope", "code": "out_of_envelope", "reason": reason},
        )
        raise SupervisorError(f"out_of_envelope: {reason}")

    def _emit(
        self, event_type: MissionEventType, task_id: str, payload: Mapping[str, Any]
    ) -> None:
        if self._evidence is None:
            return
        request = self._requests[task_id]
        body = {
            "swarm_id": request.swarm_id,
            "slice_id": request.slice_id or task_id,
            "envelope_digest": request.envelope_digest,
            **payload,
        }
        self._evidence.append(
            event_type,
            event_id=f"{task_id}:{event_type.value}:{payload.get('operation_id', task_id)}",
            payload={key: value for key, value in body.items() if value is not None},
        )

    def _validated(self, task_id: str, response: RuntimeResponse) -> RuntimeResponse:
        request = self._requests[task_id]
        return validate_response(response, request)

    def _check_control(self, task_id: str, control: str) -> None:
        request = self._requests.get(task_id)
        if request is not None and control not in request.allowed_controls:
            raise SupervisorError(f"unauthorized control: {control}")

    def _state(self, task_id: str) -> RuntimeState:
        try:
            return self._states[task_id]
        except KeyError as exc:
            raise SupervisorError(f"unknown task_id: {task_id}") from exc

    def _record(self, task_id: str, current: RuntimeState, response: RuntimeResponse) -> None:
        next_state = RuntimeState(response.state)
        if next_state == current:
            return
        from verdict.swarm_runtime import _ALLOWED_TRANSITIONS

        if next_state not in _ALLOWED_TRANSITIONS.get(current, frozenset()):
            raise SupervisorError(f"illegal transition: {current.value} -> {next_state.value}")
        self._states[task_id] = next_state


__all__ = ["SupervisorError", "SwarmSupervisor"]
