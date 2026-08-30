from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from verdict.receipt_store import ReceiptRecord, ReceiptStore

SWARM_EVIDENCE_VERSION = "swarm-evidence/v1"

_LIFECYCLE_EVENT_TYPES = frozenset(
    {
        "validation",
        "dispatch_admitted",
        "dispatch_rejected",
        "stage_started",
        "stage_completed",
        "stage_failed",
        "checkpoint_committed",
        "status_observed",
        "pause",
        "resume",
        "cancel",
        "capability_denied",
        "verification",
        "failure",
        "mission_completed",
        "mission_failed",
    }
)


class MissionEventType(str, Enum):
    VALIDATION = "validation"
    DISPATCH_ADMITTED = "dispatch_admitted"
    DISPATCH_REJECTED = "dispatch_rejected"
    STAGE_STARTED = "stage_started"
    STAGE_COMPLETED = "stage_completed"
    STAGE_FAILED = "stage_failed"
    CHECKPOINT_COMMITTED = "checkpoint_committed"
    STATUS_OBSERVED = "status_observed"
    PAUSE = "pause"
    RESUME = "resume"
    CANCEL = "cancel"
    CAPABILITY_DENIED = "capability_denied"
    VERIFICATION = "verification"
    CONFLICT_RESOLVED = "conflict_resolved"
    FAILURE = "failure"
    MISSION_COMPLETED = "mission_completed"
    MISSION_FAILED = "mission_failed"
    EXPORTED = "exported"
    REPLAY_STARTED = "replay_started"
    REPLAY_COMPLETED = "replay_completed"
    REPLAY_FAILED = "replay_failed"
    INTEGRITY_FAILED = "integrity_failed"


_TERMINAL_EVENTS = {
    MissionEventType.MISSION_COMPLETED: "completed",
    MissionEventType.MISSION_FAILED: "failed",
}
_ALLOWED_PAYLOAD_KEYS = frozenset(
    {
        "schema_version",
        "event_type",
        "swarm_id",
        "slice_id",
        "envelope_digest",
        "contract_version",
        "operation_id",
        "correlation_id",
        "evidence_ref",
        "decision_digest",
        "state",
        "prior_state",
        "new_state",
        "category",
        "code",
        "reason",
        "retryable",
        "check_id",
        "passed",
        "resource_ref",
        "candidate_digests",
        "selected_digest",
        "policy_id",
        "policy_version",
        "tie_break",
    }
)


def _allowlisted_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    unknown = set(payload) - _ALLOWED_PAYLOAD_KEYS
    if unknown:
        raise ValueError(f"unsupported evidence payload field(s): {', '.join(sorted(unknown))}")
    return {key: payload[key] for key in sorted(payload)}


@dataclass(frozen=True)
class MissionEvidence:
    store: ReceiptStore
    scope: str
    root_receipt_id: str

    @classmethod
    def create(
        cls, store: ReceiptStore, *, scope: str, swarm_id: str, event_id: str, contract_version: str
    ) -> MissionEvidence:
        root = store.put_receipt(
            "execution",
            scope,
            {
                "schema_version": SWARM_EVIDENCE_VERSION,
                "swarm_id": swarm_id,
                "contract_version": contract_version,
            },
            receipt_id=f"mission-{swarm_id}",
            event_id=event_id,
            event_type=MissionEventType.VALIDATION.value,
            idempotency_key=event_id,
            allowlist=("schema_version", "swarm_id", "contract_version"),
        )
        return cls(store=store, scope=scope, root_receipt_id=root.receipt_id)

    def append(
        self, event_type: MissionEventType, *, event_id: str, payload: Mapping[str, Any]
    ) -> ReceiptRecord:
        return self.store.append_event(
            self.root_receipt_id,
            scope=self.scope,
            payload=_allowlisted_payload({**payload, "event_type": event_type.value}),
            event_id=event_id,
            event_type=event_type.value,
            terminal_outcome=_TERMINAL_EVENTS.get(event_type),
        )

    def evidence_ref(self, record: ReceiptRecord) -> str:
        if record.scope != self.scope:
            raise ValueError("evidence reference is outside the mission scope")
        return record.receipt_id

    def replay(self) -> dict[str, Any]:
        integrity = self.store.verify_integrity(scope=self.scope)
        if not integrity.get("valid", False):
            raise ValueError("evidence integrity verification failed")
        return self.store.replay(scope=self.scope, root_receipt_id=self.root_receipt_id)

    def projections(self) -> dict[str, Any]:
        replay = self.replay()
        lifecycle: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        terminal: dict[str, Any] | None = None
        for record in replay["receipts"]:
            event_type = record["payload"].get("event_type") or record.get("event_type")
            payload = dict(record["payload"])
            if event_type in _LIFECYCLE_EVENT_TYPES:
                lifecycle.append({"receipt_id": record["receipt_id"], "event_type": event_type, "payload": payload})
            if event_type == MissionEventType.CONFLICT_RESOLVED.value:
                conflicts.append({"receipt_id": record["receipt_id"], "payload": payload})
            if event_type in (MissionEventType.MISSION_COMPLETED.value, MissionEventType.MISSION_FAILED.value) and terminal is None:
                terminal = {"receipt_id": record["receipt_id"], "event_type": event_type, "payload": payload}
        return {"lifecycle": lifecycle, "conflicts": conflicts, "terminal": terminal}

    def export(self) -> dict[str, Any]:
        integrity = self.store.verify_integrity(scope=self.scope)
        if not integrity.get("valid", False):
            raise ValueError("evidence integrity verification failed")
        return self.store.export(scope=self.scope, include_payload=True)


__all__ = ["SWARM_EVIDENCE_VERSION", "MissionEventType", "MissionEvidence"]
