from __future__ import annotations

import pytest

from verdict.receipt_store import ReceiptConflictError, ReceiptStore
from verdict.swarm_evidence import MissionEventType, MissionEvidence


def mission() -> MissionEvidence:
    return MissionEvidence.create(
        ReceiptStore(),
        scope="swarm/scope",
        swarm_id="swarm-1",
        event_id="event-root",
        contract_version="swarm-spec/v1",
    )


def test_mission_root_child_replay_and_scoped_reference() -> None:
    evidence = mission()
    event = evidence.append(
        MissionEventType.DISPATCH_ADMITTED,
        event_id="event-1",
        payload={
            "swarm_id": "swarm-1",
            "slice_id": "slice-1",
            "envelope_digest": "digest-1",
            "state": "submitted",
        },
    )

    replay = evidence.replay()

    assert evidence.evidence_ref(event) == event.receipt_id
    assert replay["receipts"][0]["receipt_id"] == evidence.root_receipt_id
    assert replay["receipt_count"] == 2


def test_payload_is_allowlisted_and_sensitive_fields_are_rejected() -> None:
    evidence = mission()

    with pytest.raises(ValueError, match="unsupported evidence payload"):
        evidence.append(
            MissionEventType.FAILURE,
            event_id="event-secret",
            payload={"prompt": "secret prompt", "token": "credential"},
        )


def test_identical_event_retry_is_idempotent_and_conflict_is_rejected() -> None:
    evidence = mission()
    first = evidence.append(
        MissionEventType.STATUS_OBSERVED,
        event_id="event-status",
        payload={"state": "running", "correlation_id": "op-1"},
    )
    retry = evidence.append(
        MissionEventType.STATUS_OBSERVED,
        event_id="event-status",
        payload={"state": "running", "correlation_id": "op-1"},
    )

    assert retry.receipt_id == first.receipt_id
    with pytest.raises(ReceiptConflictError):
        evidence.append(
            MissionEventType.STATUS_OBSERVED,
            event_id="event-status",
            payload={"state": "failed", "correlation_id": "op-1"},
        )


def test_first_terminal_outcome_is_preserved() -> None:
    evidence = mission()
    evidence.append(
        MissionEventType.MISSION_COMPLETED,
        event_id="event-complete",
        payload={"state": "completed"},
    )

    with pytest.raises(ReceiptConflictError):
        evidence.append(
            MissionEventType.MISSION_FAILED, event_id="event-failed", payload={"state": "failed"}
        )


def test_integrity_failure_stops_replay() -> None:
    evidence = mission()
    event = evidence.append(
        MissionEventType.STATUS_OBSERVED, event_id="event-status", payload={"state": "running"}
    )
    conn = evidence.store._get_connection()
    conn.execute(
        "UPDATE receipts SET payload_json = ? WHERE receipt_id = ?",
        ('{"state":"failed"}', event.receipt_id),
    )
    conn.commit()

    with pytest.raises(ValueError, match="integrity"):
        evidence.replay()


def test_persistent_store_replays_after_restart(tmp_path) -> None:
    db_path = tmp_path / "receipts.sqlite"
    first = MissionEvidence.create(
        ReceiptStore(db_path),
        scope="swarm/scope",
        swarm_id="swarm-1",
        event_id="event-root",
        contract_version="swarm-spec/v1",
    )
    first.append(
        MissionEventType.VERIFICATION,
        event_id="event-check",
        payload={"check_id": "pytest", "passed": True},
    )

    reopened = MissionEvidence(ReceiptStore(db_path), first.scope, first.root_receipt_id)
    assert reopened.replay()["receipt_count"] == 2
