"""Execution continuity state machine for issue #258.

An :class:`ExecutionSession` is the durable unit of execution continuity.
Every successful tool use, code edit, or model turn is followed by an atomic
checkpoint written to the SQLite/WAL :class:`verdict.memory_plane.MemoryPlane`.
A crashed process can reconstruct the exact runtime state with
``ExecutionSession.resume(session_id)`` and continue at the failed step without
re-running completed work.

The session carries its own replay log (``steps``), attempt counters, and a
failure log so that the execution history survives restarts.  Provider
failures are handled by :class:`verdict.failover_engine.FailoverEngine`, which
rebinds the session to an equivalent qualified ``ModelPassport``.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from verdict.memory_plane import MemoryPlane, MemoryRecord

EXECUTION_SESSION_SCHEMA_VERSION = "1"
SESSION_NAMESPACE = "execution_sessions"

SessionState = Literal["created", "running", "checkpointed", "failed", "completed"]

_VALID_STATES = frozenset({"created", "running", "checkpointed", "failed", "completed"})


class ExecutionSessionError(RuntimeError):
    """Raised when an execution session violates its state contract."""


def _strict_mapping(
    value: Mapping[str, Any], *, required: set[str], optional: set[str], field_name: str
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ExecutionSessionError(f"{field_name} must be an object")
    keys = set(value)
    missing = required - keys
    unknown = keys - required - optional
    if missing:
        raise ExecutionSessionError(f"{field_name} missing field(s): {', '.join(sorted(missing))}")
    if unknown:
        raise ExecutionSessionError(
            f"{field_name} has unknown field(s): {', '.join(sorted(unknown))}"
        )
    return dict(value)


def _now() -> float:
    return time.time()


@dataclass
class StepRecord:
    """One entry in the execution replay log."""

    step_id: str
    name: str
    status: str
    started_at: float
    completed_at: float | None = None
    model_id: str | None = None
    tokens_used: int = 0
    evidence_receipt: str | None = None
    error: str | None = None
    # issue #258 side-effect guard. ``side_effect_kind`` classifies whether a
    # step has a durable external effect; ``committed`` records that the effect
    # landed before the completion checkpoint, so a crash mid-step cannot cause
    # an irreversible effect to be re-run on resume.
    side_effect_kind: str = "read-only"  # read-only | idempotent | reversible | irreversible
    committed: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "step_id": self.step_id,
            "name": self.name,
            "status": self.status,
            "started_at": self.started_at,
        }
        if self.completed_at is not None:
            payload["completed_at"] = self.completed_at
        if self.model_id is not None:
            payload["model_id"] = self.model_id
        if self.tokens_used:
            payload["tokens_used"] = self.tokens_used
        if self.evidence_receipt is not None:
            payload["evidence_receipt"] = self.evidence_receipt
        if self.error is not None:
            payload["error"] = self.error
        if self.side_effect_kind != "read-only":
            payload["side_effect_kind"] = self.side_effect_kind
        if self.committed:
            payload["committed"] = self.committed
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> StepRecord:
        payload = _strict_mapping(
            value,
            required={"step_id", "name", "status", "started_at"},
            optional={
                "completed_at",
                "model_id",
                "tokens_used",
                "evidence_receipt",
                "error",
                "side_effect_kind",
                "committed",
            },
            field_name="step_record",
        )
        return cls(
            step_id=payload["step_id"],
            name=payload["name"],
            status=payload["status"],
            started_at=payload["started_at"],
            completed_at=payload.get("completed_at"),
            model_id=payload.get("model_id"),
            tokens_used=payload.get("tokens_used", 0),
            evidence_receipt=payload.get("evidence_receipt"),
            error=payload.get("error"),
            side_effect_kind=payload.get("side_effect_kind", "read-only"),
            committed=payload.get("committed", False),
        )


@dataclass(frozen=True)
class CheckpointRecord:
    """Metadata for one atomic checkpoint written to the MemoryPlane."""

    seq: int
    reason: str
    created_at: float
    record_id: str
    state: str = "checkpointed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "reason": self.reason,
            "created_at": self.created_at,
            "record_id": self.record_id,
            "state": self.state,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CheckpointRecord:
        payload = _strict_mapping(
            value,
            required={"seq", "reason", "created_at", "record_id"},
            optional={"state"},
            field_name="checkpoint_record",
        )
        return cls(
            seq=payload["seq"],
            reason=payload["reason"],
            created_at=payload["created_at"],
            record_id=payload["record_id"],
            state=payload.get("state", "checkpointed"),
        )


@dataclass(frozen=True)
class FailureEntry:
    """A durable, replayable record of one provider failure."""

    step_id: str
    provider: str
    model_id: str
    error_class: str
    message: str
    created_at: float
    status_code: int | None = None
    quarantine_model: str | None = None
    replacement_model: str | None = None

    @property
    def key(self) -> str:
        return f"{self.provider}/{self.model_id}"

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "step_id": self.step_id,
            "provider": self.provider,
            "model_id": self.model_id,
            "error_class": self.error_class,
            "message": self.message,
            "created_at": self.created_at,
        }
        if self.status_code is not None:
            payload["status_code"] = self.status_code
        if self.quarantine_model is not None:
            payload["quarantine_model"] = self.quarantine_model
        if self.replacement_model is not None:
            payload["replacement_model"] = self.replacement_model
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> FailureEntry:
        payload = _strict_mapping(
            value,
            required={"step_id", "provider", "model_id", "error_class", "message", "created_at"},
            optional={"status_code", "quarantine_model", "replacement_model"},
            field_name="failure_entry",
        )
        return cls(
            step_id=payload["step_id"],
            provider=payload["provider"],
            model_id=payload["model_id"],
            error_class=payload["error_class"],
            message=payload["message"],
            created_at=payload["created_at"],
            status_code=payload.get("status_code"),
            quarantine_model=payload.get("quarantine_model"),
            replacement_model=payload.get("replacement_model"),
        )


@dataclass
class ExecutionSession:
    """Durable, checkpointed execution state machine (issue #258)."""

    session_id: str
    task_spec: dict[str, Any]
    steps: list[StepRecord]
    current_step: str | None = None
    completed_steps: list[str] = field(default_factory=list)
    checkpoints: list[CheckpointRecord] = field(default_factory=list)
    artifacts: dict[str, Any] = field(default_factory=dict)
    attempts: dict[str, int] = field(default_factory=dict)
    failure_log: list[FailureEntry] = field(default_factory=list)
    state: SessionState = "created"
    model_id: str | None = None
    bound_passport_key: str | None = None
    created_at: float = field(default_factory=_now)
    updated_at: float = field(default_factory=_now)
    schema_version: str = EXECUTION_SESSION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ExecutionSessionError("session_id must be a non-empty string")
        if self.schema_version != EXECUTION_SESSION_SCHEMA_VERSION:
            raise ExecutionSessionError("schema_version must be '1'")
        if self.state not in _VALID_STATES:
            raise ExecutionSessionError(f"invalid session state: {self.state!r}")
        if not isinstance(self.steps, Sequence) or not self.steps:
            raise ExecutionSessionError("a session requires at least one step")
        self.steps = list(self.steps)
        if self.current_step is None:
            self.current_step = next(
                (step.step_id for step in self.steps if step.status == "pending"),
                self.steps[0].step_id,
            )

    # ------------------------------------------------------------------ factory

    @classmethod
    def create(
        cls,
        session_id: str,
        task_spec: dict[str, Any],
        *,
        steps: list[tuple[str, str]],
        plane: MemoryPlane | None = None,
        model_id: str | None = None,
    ) -> ExecutionSession:
        """Build a session from ``(step_id, name)`` pairs and optionally persist it."""
        created_at = _now()
        records = [
            StepRecord(step_id=step_id, name=name, status="pending", started_at=created_at)
            for step_id, name in steps
        ]
        session = cls(
            session_id=session_id,
            task_spec=task_spec,
            steps=records,
            model_id=model_id,
            created_at=created_at,
            updated_at=created_at,
        )
        if plane is not None:
            session.checkpoint(plane, reason="created")
        return session

    @classmethod
    def resume(cls, session_id: str, plane: MemoryPlane) -> ExecutionSession:
        """Restore the exact runtime state from the most recent active checkpoint."""
        record = plane.get(SESSION_NAMESPACE, session_id)
        if record is None:
            raise ExecutionSessionError(f"no checkpoint found for session {session_id!r}")
        try:
            payload = json.loads(record.content)
        except (TypeError, ValueError) as exc:
            raise ExecutionSessionError("stored session checkpoint is not valid JSON") from exc
        return cls.from_dict(payload)

    # ------------------------------------------------------------------ state machine

    def checkpoint(self, plane: MemoryPlane, *, reason: str) -> CheckpointRecord:
        """Atomically persist the full session state as a new checkpoint."""
        seq = len(self.checkpoints) + 1
        record_id = f"{self.session_id}:ckpt:{seq}"
        record = CheckpointRecord(seq=seq, reason=reason, created_at=_now(), record_id=record_id)
        self.checkpoints.append(record)
        self.updated_at = _now()
        self._persist(plane, record_id)
        return record

    def start(self, plane: MemoryPlane, *, model_id: str | None = None) -> CheckpointRecord:
        """Transition to ``running`` and persist the starting state."""
        if self.state not in {"created", "checkpointed", "failed"}:
            raise ExecutionSessionError(f"cannot start session in state {self.state!r}")
        if model_id is not None:
            self.model_id = model_id
        self.state = "running"
        if self.current_step is None:
            self.current_step = next(
                (step.step_id for step in self.steps if step.status == "pending"), None
            )
        return self.checkpoint(plane, reason="started")

    def complete_step(
        self,
        plane: MemoryPlane,
        step_id: str,
        *,
        model_id: str | None = None,
        tokens_used: int = 0,
        evidence_receipt: str | None = None,
    ) -> CheckpointRecord:
        """Mark a step successful, advance the cursor, and checkpoint (atomic)."""
        step = self._find_step(step_id)
        if step.status in {"completed", "failed"}:
            raise ExecutionSessionError(f"step {step_id!r} already {step.status}")
        # A committed idempotent/reversible step already landed its durable effect on an
        # earlier attempt; it skips re-committing below and finalizes normally.
        if step.committed and step.side_effect_kind == "irreversible":
            raise ExecutionSessionError(f"irreversible step {step_id!r} already committed")
        if model_id is not None:
            self.model_id = model_id
        if not step.committed and step.side_effect_kind != "read-only":
            # The step body ran and its durable external effect landed. Persist
            # ``committed`` immediately so a crash after the effect but before
            # the completion checkpoint cannot re-run it on resume.
            step.committed = True
            self.checkpoint(plane, reason=f"step_committed:{step_id}")
        step.status = "completed"
        step.completed_at = _now()
        step.model_id = model_id or self.model_id
        step.tokens_used = int(tokens_used)
        step.evidence_receipt = evidence_receipt
        if step_id not in self.completed_steps:
            self.completed_steps.append(step_id)
        pending = [item for item in self.steps if item.status == "pending"]
        if not pending:
            self.state = "completed"
            self.current_step = None
        else:
            self.state = "checkpointed"
            self.current_step = pending[0].step_id
        return self.checkpoint(plane, reason=f"step_completed:{step_id}")

    def fail_step(
        self, plane: MemoryPlane, step_id: str, *, error: str, model_id: str | None = None
    ) -> CheckpointRecord:
        """Mark a step failed, transition to ``failed``, and checkpoint (atomic)."""
        step = self._find_step(step_id)
        step.status = "failed"
        step.error = error
        step.completed_at = _now()
        step.model_id = model_id or self.model_id
        self.state = "failed"
        return self.checkpoint(plane, reason=f"step_failed:{step_id}")

    def resume_from_failure(
        self,
        plane: MemoryPlane,
        *,
        failure: FailureEntry,
        replacement_model: str | None = None,
        replacement_passport_key: str | None = None,
        reason: str = "failover",
    ) -> CheckpointRecord:
        """Rebind the session after a failover and re-arm the failed step.

        Completed steps are preserved verbatim; only the failed step is
        re-marked ``pending`` so the next attempt re-runs exactly that step.
        """
        self.failure_log.append(failure)
        self.attempts[failure.step_id] = self.attempts.get(failure.step_id, 0) + 1
        if replacement_model is not None:
            self.model_id = replacement_model
            self.bound_passport_key = replacement_passport_key
        step = self._find_step(failure.step_id)
        if step.committed:
            # A durable effect already landed before the crash. Re-running the
            # step would duplicate it, so either refuse (irreversible) or skip
            # the re-run and record the step as completed.
            if step.side_effect_kind == "irreversible":
                raise ExecutionSessionError(
                    f"irreversible step {failure.step_id!r} already committed"
                )
            step.status = "completed"
            step.error = None
            step.completed_at = _now()
            if failure.step_id not in self.completed_steps:
                self.completed_steps.append(failure.step_id)
        elif step.status == "failed":
            step.status = "pending"
            step.error = None
            step.completed_at = None
        self.state = "running"
        if step.committed:
            # The committed step is skipped; advance to the next pending step.
            next_pending = next(
                (item.step_id for item in self.steps if item.status == "pending"), None
            )
            self.current_step = next_pending
        elif self.current_step != failure.step_id:
            self.current_step = failure.step_id
        return self.checkpoint(plane, reason=reason)

    def record_artifact(self, name: str, value: Any) -> None:
        """Stage an artifact; the next checkpoint persists it with the session."""
        self.artifacts[name] = value
        self.updated_at = _now()

    # ------------------------------------------------------------------ queries

    @property
    def remaining_steps(self) -> list[StepRecord]:
        return [step for step in self.steps if step.status == "pending"]

    def _find_step(self, step_id: str) -> StepRecord:
        for step in self.steps:
            if step.step_id == step_id:
                return step
        raise ExecutionSessionError(f"unknown step {step_id!r}")

    # ------------------------------------------------------------------ persistence

    def _persist(self, plane: MemoryPlane, record_id: str) -> None:
        content = json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        record = MemoryRecord(
            record_id=record_id,
            namespace=SESSION_NAMESPACE,
            key=self.session_id,
            content=content,
            source="execution-session",
            trust="local-observation",
            scope="default",
            metadata={"session_id": self.session_id, "state": self.state},
            created_at=self.updated_at,
            updated_at=self.updated_at,
            authority="unverified",
            confidence=0.0,
            sensitivity="standard",
            status="active",
        )
        plane.put(record)

    # ------------------------------------------------------------------ serialization

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "task_spec": self.task_spec,
            "steps": [step.to_dict() for step in self.steps],
            "current_step": self.current_step,
            "completed_steps": list(self.completed_steps),
            "checkpoints": [checkpoint.to_dict() for checkpoint in self.checkpoints],
            "artifacts": self.artifacts,
            "attempts": dict(self.attempts),
            "failure_log": [entry.to_dict() for entry in self.failure_log],
            "state": self.state,
            "model_id": self.model_id,
            "bound_passport_key": self.bound_passport_key,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ExecutionSession:
        payload = _strict_mapping(
            value,
            required={
                "schema_version",
                "session_id",
                "task_spec",
                "steps",
                "current_step",
                "completed_steps",
                "checkpoints",
                "artifacts",
                "attempts",
                "failure_log",
                "state",
                "created_at",
                "updated_at",
            },
            optional={"model_id", "bound_passport_key"},
            field_name="execution_session",
        )
        return cls(
            schema_version=payload["schema_version"],
            session_id=payload["session_id"],
            task_spec=dict(payload["task_spec"]),
            steps=[StepRecord.from_dict(item) for item in payload["steps"]],
            current_step=payload["current_step"],
            completed_steps=list(payload["completed_steps"]),
            checkpoints=[CheckpointRecord.from_dict(item) for item in payload["checkpoints"]],
            artifacts=dict(payload["artifacts"]),
            attempts={str(key): int(value) for key, value in payload["attempts"].items()},
            failure_log=[FailureEntry.from_dict(item) for item in payload["failure_log"]],
            state=payload["state"],
            model_id=payload.get("model_id"),
            bound_passport_key=payload.get("bound_passport_key"),
            created_at=payload["created_at"],
            updated_at=payload["updated_at"],
        )


__all__ = [
    "EXECUTION_SESSION_SCHEMA_VERSION",
    "SESSION_NAMESPACE",
    "CheckpointRecord",
    "ExecutionSession",
    "ExecutionSessionError",
    "FailureEntry",
    "SessionState",
    "StepRecord",
]
