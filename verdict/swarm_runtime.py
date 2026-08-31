from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol, TypeVar, cast, runtime_checkable

RUNTIME_ADAPTER_PROTOCOL_VERSION = "runtime-adapter/v1"
SWARM_RUNTIME_PROTOCOL_VERSION = "swarm-runtime/v1"
SUPPORTED_RUNTIME_PROTOCOL_VERSIONS = frozenset(
    {RUNTIME_ADAPTER_PROTOCOL_VERSION, SWARM_RUNTIME_PROTOCOL_VERSION}
)


class RuntimeState(str, Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    REJECTED = "rejected"


TERMINAL_RUNTIME_STATES = frozenset(
    {
        RuntimeState.COMPLETED,
        RuntimeState.FAILED,
        RuntimeState.CANCELLED,
        RuntimeState.TIMEOUT,
        RuntimeState.REJECTED,
    }
)

T = TypeVar("T", bound="_Serializable")


def _json_mapping(value: Mapping[str, Any], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return dict(value)


def _json_list(value: Sequence[Mapping[str, Any]], name: str) -> list[dict[str, Any]]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{name} must be a list")
    return [_json_mapping(item, f"{name} item") for item in value]


def _non_empty(value: str, name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be non-empty")


def _check_protocol(version: str) -> None:
    if version not in SUPPORTED_RUNTIME_PROTOCOL_VERSIONS:
        raise ValueError(f"protocol_version unsupported: {version}")


class _Serializable:
    @classmethod
    def from_dict(cls: type[T], payload: Mapping[str, Any]) -> T:
        if not isinstance(payload, Mapping):
            raise ValueError("payload must be an object")
        names = {item.name for item in fields(cast(Any, cls))}
        unknown = set(payload) - names
        if unknown:
            raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
        return cls(**dict(payload))

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for item in fields(cast(Any, self)):
            value = getattr(self, item.name)
            if isinstance(value, Enum):
                value = value.value
            elif hasattr(value, "to_dict"):
                value = value.to_dict()
            result[item.name] = value
        return result


@dataclass(frozen=True)
class RuntimeFailure(_Serializable):
    code: str
    message: str
    retryable: bool = False
    details: Mapping[str, Any] = field(default_factory=dict)
    category: str = "malformed_message"
    field_path: str | None = None
    swarm_id: str | None = None
    slice_id: str | None = None
    envelope_digest: str | None = None
    operation_id: str | None = None
    evidence_ref: str | None = None

    def __post_init__(self) -> None:
        _non_empty(self.code, "failure.code")
        _non_empty(self.message, "failure.message")
        if not isinstance(self.retryable, bool):
            raise ValueError("failure.retryable must be boolean")
        object.__setattr__(self, "details", _json_mapping(self.details, "failure.details"))


@dataclass(frozen=True)
class RuntimeRequest(_Serializable):
    request_id: str
    task_id: str
    objective: str
    protocol_version: str = RUNTIME_ADAPTER_PROTOCOL_VERSION
    swarm_id: str | None = None
    slice_id: str | None = None
    envelope_digest: str | None = None
    approved_bounds: Mapping[str, Any] = field(default_factory=dict)
    verification_profile: Mapping[str, Any] = field(default_factory=dict)
    allowed_controls: Sequence[str] = field(default_factory=lambda: ("pause", "resume", "cancel"))
    route_attempts: Sequence[Mapping[str, Any]] = field(default_factory=list)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _check_protocol(self.protocol_version)
        for name in ("request_id", "task_id", "objective"):
            _non_empty(getattr(self, name), name)
        object.__setattr__(
            self, "approved_bounds", _json_mapping(self.approved_bounds, "approved_bounds")
        )
        object.__setattr__(
            self,
            "verification_profile",
            _json_mapping(self.verification_profile, "verification_profile"),
        )
        object.__setattr__(self, "allowed_controls", tuple(self.allowed_controls))
        if set(self.allowed_controls) - {"pause", "resume", "cancel"}:
            raise ValueError("allowed_controls contains unsupported control")
        object.__setattr__(
            self, "route_attempts", _json_list(self.route_attempts, "route_attempts")
        )
        object.__setattr__(self, "metadata", _json_mapping(self.metadata, "metadata"))
        if self.protocol_version == SWARM_RUNTIME_PROTOCOL_VERSION:
            for name in ("swarm_id", "slice_id", "envelope_digest"):
                value = getattr(self, name)
                if value is None:
                    raise ValueError(f"{name} is required for swarm-runtime/v1")
                _non_empty(value, name)
            if not self.approved_bounds:
                raise ValueError("approved_bounds is required for swarm-runtime/v1")
            if not self.verification_profile:
                raise ValueError("verification_profile is required for swarm-runtime/v1")
            evidence_scope = self.metadata.get("evidence_scope")
            if not isinstance(evidence_scope, str) or not evidence_scope:
                raise ValueError("metadata.evidence_scope is required for swarm-runtime/v1")


@dataclass(frozen=True)
class RuntimeResponse(_Serializable):
    request_id: str
    task_id: str
    state: RuntimeState | str
    protocol_version: str = RUNTIME_ADAPTER_PROTOCOL_VERSION
    swarm_id: str | None = None
    slice_id: str | None = None
    envelope_digest: str | None = None
    operation_id: str | None = None
    evidence_ref: str | None = None
    output: Mapping[str, Any] | None = None
    failure: RuntimeFailure | Mapping[str, Any] | None = None
    route_attempts: Sequence[Mapping[str, Any]] = field(default_factory=list)
    cancel_deadline_at: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _check_protocol(self.protocol_version)
        for name in ("request_id", "task_id"):
            _non_empty(getattr(self, name), name)
        object.__setattr__(self, "state", RuntimeState(self.state))
        if self.swarm_id is not None:
            _non_empty(self.swarm_id, "swarm_id")
        if self.slice_id is not None:
            _non_empty(self.slice_id, "slice_id")
        if self.envelope_digest is not None:
            _non_empty(self.envelope_digest, "envelope_digest")
        if self.output is not None:
            object.__setattr__(self, "output", _json_mapping(self.output, "output"))
        if isinstance(self.failure, Mapping):
            object.__setattr__(self, "failure", RuntimeFailure.from_dict(self.failure))
        elif self.failure is not None and not isinstance(self.failure, RuntimeFailure):
            raise ValueError("failure must be a RuntimeFailure")
        object.__setattr__(
            self, "route_attempts", _json_list(self.route_attempts, "route_attempts")
        )
        if self.cancel_deadline_at is not None:
            _non_empty(self.cancel_deadline_at, "cancel_deadline_at")
        object.__setattr__(self, "metadata", _json_mapping(self.metadata, "metadata"))


_ALLOWED_TRANSITIONS = {
    RuntimeState.PENDING: frozenset(
        {RuntimeState.SUBMITTED, RuntimeState.CANCELLED, RuntimeState.REJECTED}
    ),
    RuntimeState.SUBMITTED: frozenset(
        {
            RuntimeState.QUEUED,
            RuntimeState.RUNNING,
            RuntimeState.CANCELLED,
            RuntimeState.FAILED,
            RuntimeState.TIMEOUT,
        }
    ),
    RuntimeState.QUEUED: frozenset(
        {RuntimeState.RUNNING, RuntimeState.CANCELLED, RuntimeState.FAILED, RuntimeState.TIMEOUT}
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
        {RuntimeState.RUNNING, RuntimeState.FAILED, RuntimeState.CANCELLED, RuntimeState.TIMEOUT}
    ),
}


def validate_response(response: RuntimeResponse, request: RuntimeRequest) -> RuntimeResponse:
    """Reject observations that do not retain identity or approved bounds."""
    if response.request_id != request.request_id or response.task_id != request.task_id:
        raise ValueError("response correlation identity does not match request")
    for name in ("swarm_id", "slice_id", "envelope_digest"):
        expected = getattr(request, name)
        observed = getattr(response, name)
        if request.protocol_version == SWARM_RUNTIME_PROTOCOL_VERSION and observed is None:
            raise ValueError(f"response {name} is required")
        if expected is not None and observed not in (None, expected):
            raise ValueError(f"response {name} broadens or changes approved identity")
    if request.protocol_version == SWARM_RUNTIME_PROTOCOL_VERSION:
        if response.protocol_version != request.protocol_version:
            raise ValueError("response protocol_version does not match request")
        for name in ("operation_id", "evidence_ref"):
            if not getattr(response, name):
                raise ValueError(f"response {name} is required")
    observed_bounds = response.metadata.get("observed_bounds", {})
    if not isinstance(observed_bounds, Mapping):
        raise ValueError("response observed_bounds must be an object")
    for name, approved in request.approved_bounds.items():
        if name not in observed_bounds:
            if request.protocol_version == SWARM_RUNTIME_PROTOCOL_VERSION:
                raise ValueError(f"response observed_bounds missing {name}")
            continue
        observed = observed_bounds[name]
        if isinstance(approved, (int, float)) and isinstance(observed, (int, float)):
            if observed > approved:
                raise ValueError(f"response bound {name} exceeds approved bound")
        elif isinstance(approved, (list, tuple, set)) and isinstance(observed, (list, tuple, set)):
            if not set(observed).issubset(set(approved)):
                raise ValueError(f"response bound {name} exceeds approved bound")
        elif observed != approved:
            raise ValueError(f"response bound {name} differs from approved bound")
    if request.protocol_version == SWARM_RUNTIME_PROTOCOL_VERSION:
        verification_status = response.metadata.get("verification_status")
        if verification_status not in {"pending", "passed", "failed"}:
            raise ValueError("response verification status is required")
        evidence_scope = response.metadata.get("evidence_scope")
        if evidence_scope != request.metadata.get("evidence_scope"):
            raise ValueError("response evidence scope does not match request")
    return response


@runtime_checkable
class SwarmRuntimeAdapter(Protocol):
    def submit(self, request: RuntimeRequest) -> RuntimeResponse: ...

    def start(self, task_id: str) -> RuntimeResponse: ...

    def complete(
        self, task_id: str, output: Mapping[str, Any] | None = None
    ) -> RuntimeResponse: ...

    def fail(self, task_id: str, failure: RuntimeFailure) -> RuntimeResponse: ...

    def pause(self, task_id: str, *, reason: str = "") -> RuntimeResponse: ...

    def resume(self, task_id: str, *, reason: str = "") -> RuntimeResponse: ...

    def cancel(
        self, task_id: str, deadline_at: datetime | None = None, *, reason: str = ""
    ) -> RuntimeResponse: ...

    def result(self, task_id: str) -> RuntimeResponse: ...

    def verify(self, task_id: str, verification: Mapping[str, Any]) -> RuntimeResponse: ...

    def status(self, task_id: str) -> RuntimeResponse: ...


class FakeSwarmRuntimeAdapter:
    def __init__(self, *, now: Callable[[], datetime] | None = None) -> None:
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._requests: dict[str, RuntimeRequest] = {}
        self._responses: dict[str, RuntimeResponse] = {}

    def submit(self, request: RuntimeRequest) -> RuntimeResponse:
        response = RuntimeResponse(
            request_id=request.request_id,
            task_id=request.task_id,
            state=RuntimeState.SUBMITTED,
            protocol_version=request.protocol_version,
            swarm_id=request.swarm_id,
            slice_id=request.slice_id,
            envelope_digest=request.envelope_digest,
            operation_id=request.request_id,
            evidence_ref=f"{request.metadata.get('evidence_scope')}:{request.task_id}",
            route_attempts=request.route_attempts,
            metadata={
                "observed_bounds": dict(request.approved_bounds),
                "evidence_scope": request.metadata.get("evidence_scope"),
                "verification_status": "pending",
            },
        )
        self._requests[request.task_id] = request
        self._responses[request.task_id] = response
        return response

    def start(self, task_id: str) -> RuntimeResponse:
        current = self.status(task_id)
        response = self._replace(current, state=RuntimeState.RUNNING)
        self._responses[task_id] = response
        return response

    def complete(self, task_id: str, output: Mapping[str, Any] | None = None) -> RuntimeResponse:
        current = self.status(task_id)
        response = self._replace(current, state=RuntimeState.COMPLETED, output=output or {})
        self._responses[task_id] = response
        return response

    def fail(self, task_id: str, failure: RuntimeFailure) -> RuntimeResponse:
        current = self.status(task_id)
        response = self._replace(current, state=RuntimeState.FAILED, failure=failure)
        self._responses[task_id] = response
        return response

    def pause(self, task_id: str, *, reason: str = "") -> RuntimeResponse:
        current = self.status(task_id)
        response = self._replace(current, state=RuntimeState.PAUSED, metadata={"reason": reason})
        self._responses[task_id] = response
        return response

    def resume(self, task_id: str, *, reason: str = "") -> RuntimeResponse:
        current = self.status(task_id)
        response = self._replace(current, state=RuntimeState.RUNNING, metadata={"reason": reason})
        self._responses[task_id] = response
        return response

    def cancel(
        self, task_id: str, deadline_at: datetime | None = None, *, reason: str = ""
    ) -> RuntimeResponse:
        current = self.status(task_id)
        deadline = deadline_at or self._now()
        response = self._replace(
            current,
            state=RuntimeState.CANCELLED,
            cancel_deadline_at=deadline.isoformat(),
            metadata={"reason": reason},
        )
        self._responses[task_id] = response
        return response

    def result(self, task_id: str) -> RuntimeResponse:
        return self.status(task_id)

    def verify(self, task_id: str, verification: Mapping[str, Any]) -> RuntimeResponse:
        current = self.status(task_id)
        checks = verification.get("required_checks", ())
        if not isinstance(checks, Sequence) or isinstance(checks, (str, bytes)):
            raise ValueError("verification.required_checks must be a list")
        required_refs = self._requests[task_id].verification_profile.get("required_evidence", ())
        if not isinstance(required_refs, Sequence) or isinstance(required_refs, (str, bytes)):
            raise ValueError("verification.required_evidence must be a list")
        request_scope = self._requests[task_id].metadata.get("evidence_scope")
        for ref in required_refs:
            if not isinstance(ref, str) or (
                request_scope and not ref.startswith(f"{request_scope}:")
            ):
                return self._replace(
                    current,
                    state=RuntimeState.FAILED,
                    failure=RuntimeFailure(
                        code="evidence_scope_mismatch",
                        message="verification evidence is outside slice scope",
                        category="verification",
                    ),
                )
        if any(
            not isinstance(check, Mapping) or check.get("passed") is not True for check in checks
        ):
            return self._replace(
                current,
                state=RuntimeState.FAILED,
                failure=RuntimeFailure(
                    code="required_verification_failed",
                    message="required verification failed",
                    category="verification",
                ),
            )
        if current.state == RuntimeState.COMPLETED and checks:
            return current
        return self._replace(current, state=RuntimeState.COMPLETED)

    def status(self, task_id: str) -> RuntimeResponse:
        try:
            return self._responses[task_id]
        except KeyError as exc:
            raise ValueError(f"unknown task_id: {task_id}") from exc

    def _replace(self, response: RuntimeResponse, **changes: Any) -> RuntimeResponse:
        payload = response.to_dict()
        if "metadata" in changes:
            metadata = dict(response.metadata)
            metadata.update(_json_mapping(changes.pop("metadata"), "metadata"))
            changes["metadata"] = metadata
        payload.update(changes)
        return RuntimeResponse.from_dict(payload)


__all__ = [
    "RUNTIME_ADAPTER_PROTOCOL_VERSION",
    "SUPPORTED_RUNTIME_PROTOCOL_VERSIONS",
    "SWARM_RUNTIME_PROTOCOL_VERSION",
    "TERMINAL_RUNTIME_STATES",
    "FakeSwarmRuntimeAdapter",
    "RuntimeFailure",
    "RuntimeRequest",
    "RuntimeResponse",
    "RuntimeState",
    "SwarmRuntimeAdapter",
    "validate_response",
]
