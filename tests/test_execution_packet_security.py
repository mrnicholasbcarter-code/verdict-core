"""Negative and safety tests for operational-loop execution packets."""

from __future__ import annotations

from copy import deepcopy

import pytest

from verdict.execution_packet import (
    ExecutionPacket,
    ExecutionPacketError,
    PacketTransition,
    UnsupportedSchemaVersionError,
    schema_refusal_receipt,
)

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def packet_payload() -> dict[str, object]:
    return {
        "schema_version": "1",
        "packet_id": "packet-headroom-unknown",
        "packet_version": 1,
        "story_id": "US1",
        "story_version": "1",
        "source": {
            "repository": "git@github.com:example/verdict.git",
            "worktree": "/workspace/verdict",
            "commit": "a" * 40,
            "branch": "feature/operational-loop",
            "dirty_digest": DIGEST_A,
            "lock_digests": {},
        },
        "intent": {
            "goal": "Represent missing headroom as unknown.",
            "non_goals": ["Provider clients."],
            "acceptance": ["No fabricated capacity."],
            "limitations": [],
        },
        "authority": {
            "owned_paths": ["verdict/headroom.py", "tests/test_headroom.py"],
            "denied_paths": [".env"],
            "tools": ["read", "patch", "test"],
            "network": False,
            "max_spend_usd": 0.25,
            "max_concurrency": 1,
            "max_attempts": 2,
            "destructive": False,
            "production": False,
        },
        "verification": {"argv": ["pytest", "tests/test_headroom.py"], "timeout_seconds": 120},
        "decisions": [],
        "context_refs": [{"ref": "verdict/headroom.py", "digest": DIGEST_B}],
        "tasks": [
            {"task_id": "T1", "description": "Implement.", "status": "pending", "dependencies": []}
        ],
        "route_attempts": [],
        "failure_history": [],
        "transitions": [],
        "checkpoint_refs": [],
        "receipt_refs": [],
        "next_safe_action": "Run red test.",
        "proof_level": "source-only",
    }


@pytest.mark.parametrize(
    "field",
    ["secret", "api_key", "authorization", "prompt", "completion", "messages", "tool_arguments"],
)
def test_packet_rejects_raw_sensitive_or_conversation_fields(field: str) -> None:
    payload = packet_payload()
    payload["context_refs"] = [{"ref": "x", "digest": DIGEST_B, field: "raw"}]

    with pytest.raises(ExecutionPacketError, match="forbidden field"):
        ExecutionPacket.from_dict(payload)


def test_unsupported_version_and_missing_authority_are_rejected() -> None:
    unsupported = packet_payload()
    unsupported["schema_version"] = "99"
    with pytest.raises(UnsupportedSchemaVersionError) as excinfo:
        ExecutionPacket.from_dict(unsupported)
    receipt = schema_refusal_receipt(excinfo.value)
    assert receipt["encountered_schema_version"] == "99"
    assert receipt["supported_schema_versions"] == ["1"]
    # The refusal is decided at packet validation, before any gateway activity.
    assert receipt["gateway_requests_issued"] == 0

    missing = packet_payload()
    del missing["authority"]
    with pytest.raises(ExecutionPacketError, match="missing field"):
        ExecutionPacket.from_dict(missing)


def test_conflicting_duplicate_transition_is_rejected() -> None:
    packet = ExecutionPacket.from_dict(packet_payload())
    first = PacketTransition(
        transition_id="same", task_id="T1", from_state="pending", to_state="active", reason="start"
    )
    conflicting = PacketTransition(
        transition_id="same",
        task_id="T1",
        from_state="pending",
        to_state="blocked",
        reason="different durable effect",
    )

    with pytest.raises(ExecutionPacketError, match="conflicting duplicate transition"):
        packet.transition(first).transition(conflicting)


@pytest.mark.parametrize(
    "section", ["source", "intent", "authority", "verification", "context_refs"]
)
def test_resume_refuses_each_immutable_digest_boundary(section: str) -> None:
    original = ExecutionPacket.from_dict(packet_payload())
    changed = deepcopy(packet_payload())
    if section == "context_refs":
        changed[section] = [{"ref": "different.py", "digest": DIGEST_B}]
    elif section == "source":
        target = dict(changed[section])  # type: ignore[arg-type]
        target["dirty_digest"] = "sha256:" + "c" * 64
        changed[section] = target
    elif section == "intent":
        target = dict(changed[section])  # type: ignore[arg-type]
        target["goal"] = "A changed goal."
        changed[section] = target
    elif section == "authority":
        target = dict(changed[section])  # type: ignore[arg-type]
        target["owned_paths"] = ["different.py"]
        changed[section] = target
    elif section == "verification":
        target = dict(changed[section])  # type: ignore[arg-type]
        target["argv"] = ["pytest", "different.py"]
        changed[section] = target
    else:
        raise AssertionError(section)

    with pytest.raises(ExecutionPacketError, match="immutable packet drift"):
        original.validate_resume(ExecutionPacket.from_dict(changed))


def test_uncertain_committed_write_blocks_resume() -> None:
    payload = packet_payload()
    payload["transitions"] = [
        {
            "transition_id": "write-1",
            "task_id": "T1",
            "from_state": "active",
            "to_state": "uncertain",
            "reason": "stream disconnected after write",
            "side_effect_kind": "reversible",
            "committed": True,
            "evidence_refs": [],
        }
    ]
    packet = ExecutionPacket.from_dict(payload)

    with pytest.raises(ExecutionPacketError, match="uncertain committed write"):
        packet.validate_resume(packet)
