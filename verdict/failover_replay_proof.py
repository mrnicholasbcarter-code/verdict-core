"""Offline mission-level forced-failover and replay proof for issue #267."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from verdict.execution_session import ExecutionSession
from verdict.failover_engine import FailoverEngine
from verdict.memory_plane import MemoryPlane
from verdict.model_passports import ModelPassport


@dataclass(frozen=True)
class MissionEvent:
    seq: int
    mission_id: str
    kind: str
    stage: str
    model: str
    status: str
    error_class: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema_version": "1",
            "seq": self.seq,
            "mission_id": self.mission_id,
            "kind": self.kind,
            "stage": self.stage,
            "model": self.model,
            "status": self.status,
        }
        if self.error_class is not None:
            value["error_class"] = self.error_class
        return value


@dataclass(frozen=True)
class ReplayProof:
    mission_id: str
    events: tuple[MissionEvent, ...]
    completed_stages: tuple[str, ...]
    replacement_model: str
    terminal_status: str

    @property
    def digest(self) -> str:
        data = {
            "mission_id": self.mission_id,
            "events": [e.to_dict() for e in self.events],
            "completed_stages": list(self.completed_stages),
            "replacement_model": self.replacement_model,
            "terminal_status": self.terminal_status,
        }
        return (
            "sha256:"
            + hashlib.sha256(
                json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        )


def run_forced_failover_proof(plane: MemoryPlane, *, mission_id: str | None = None) -> ReplayProof:
    mission = mission_id or f"mission-267-{uuid4().hex}"
    events: list[MissionEvent] = []

    def emit(kind: str, stage: str, model: str, status: str, error: str | None = None) -> None:
        events.append(MissionEvent(len(events) + 1, mission, kind, stage, model, status, error))

    session = ExecutionSession.create(
        mission,
        {"task": "offline proof", "requirements": {"required": ["tools"]}},
        steps=[("prepare", "prepare"), ("execute", "execute"), ("publish", "publish")],
        plane=plane,
        model_id="provider-a/model-a",
    )
    session.start(plane)
    emit("stage_started", "prepare", "provider-a/model-a", "running")
    session.complete_step(plane, "prepare", model_id="provider-a/model-a")
    emit("stage_completed", "prepare", "provider-a/model-a", "completed")
    emit("stage_started", "execute", "provider-a/model-a", "running")
    session.fail_step(plane, "execute", error="HTTP 429", model_id="provider-a/model-a")
    emit("provider_failure", "execute", "provider-a/model-a", "failed", "rate_limited")
    replacement = ModelPassport(
        provider="provider-b",
        model_id="model-b",
        auth_state="authorized",
        context_window=16000,
        tool_support=True,
        token_cost_per_1k=1.0,
        availability_state="eligible",
    )
    FailoverEngine().failover(
        session,
        plane,
        provider="provider-a",
        model_id="model-a",
        error_class="rate_limited",
        status_code=429,
        candidates=[replacement],
    )
    emit("failover_selected", "execute", replacement.key, "rebound")
    session.complete_step(plane, "execute", model_id=replacement.key)
    emit("stage_completed", "execute", replacement.key, "completed")
    session.complete_step(plane, "publish", model_id=replacement.key)
    emit("stage_completed", "publish", replacement.key, "completed")
    return ReplayProof(
        mission, tuple(events), tuple(session.completed_steps), replacement.key, session.state
    )


def replay_proof(proof: ReplayProof) -> ReplayProof:
    if not proof.events or [event.seq for event in proof.events] != list(
        range(1, len(proof.events) + 1)
    ):
        raise ValueError("event sequence is incomplete or non-contiguous")
    if not any(
        event.kind == "provider_failure" and event.error_class == "rate_limited"
        for event in proof.events
    ):
        raise ValueError("replay lacks forced rate-limit failure")
    selected = [event.model for event in proof.events if event.kind == "failover_selected"]
    if selected != [proof.replacement_model]:
        raise ValueError("replay replacement does not match selected model")
    completed = tuple(event.stage for event in proof.events if event.kind == "stage_completed")
    if completed != proof.completed_stages:
        raise ValueError("replayed completed stages differ")
    return proof
