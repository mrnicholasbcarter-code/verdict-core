"""Contract tests for the portable operational-loop execution packet."""

from __future__ import annotations

import json
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from verdict.execution_packet import (
    ExecutionPacket,
    ExecutionPacketError,
    ExecutionPacketStore,
    PacketTransition,
    ProofLevel,
    capture_source_binding,
    capture_worktree_digest,
)
from verdict.receipt_store import ReceiptStore

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
            "lock_digests": {"uv.lock": DIGEST_B},
        },
        "intent": {
            "goal": "Represent missing headroom as unknown.",
            "non_goals": ["Implement provider quota clients."],
            "acceptance": ["Missing endpoint never reports 100 percent."],
            "limitations": ["Quota may remain unknown."],
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
        "verification": {
            "argv": ["uv", "run", "pytest", "-q", "tests/test_headroom.py"],
            "timeout_seconds": 120,
        },
        "decisions": [{"ref": "spec.md", "digest": DIGEST_A}],
        "context_refs": [
            {"ref": "verdict/headroom.py", "digest": DIGEST_B, "proof_level": "source-only"}
        ],
        "tasks": [
            {
                "task_id": "T1",
                "description": "Implement fail-closed headroom.",
                "status": "pending",
                "dependencies": [],
            }
        ],
        "route_attempts": [],
        "failure_history": [],
        "transitions": [],
        "checkpoint_refs": [],
        "receipt_refs": [],
        "next_safe_action": "Run the focused red test.",
        "proof_level": "source-only",
    }


def test_packet_round_trip_binds_all_continuation_fields_and_digest() -> None:
    packet = ExecutionPacket.from_dict(packet_payload())

    restored = ExecutionPacket.from_dict(packet.to_dict())

    assert restored == packet
    assert packet.integrity_digest.startswith("sha256:")
    assert packet.source["dirty_digest"] == DIGEST_A
    assert packet.intent["non_goals"] == ("Implement provider quota clients.",)
    assert packet.authority["owned_paths"] == ("verdict/headroom.py", "tests/test_headroom.py")
    assert packet.verification["argv"][-1] == "tests/test_headroom.py"
    assert packet.context_refs[0]["proof_level"] is ProofLevel.SOURCE_ONLY
    assert packet.tasks[0]["status"] == "pending"
    assert packet.next_safe_action == "Run the focused red test."


def test_model_substitution_does_not_change_immutable_packet_identity() -> None:
    packet = ExecutionPacket.from_dict(packet_payload())
    rebound = packet.for_model("openrouter/stealth/ox-alpha")

    assert rebound.integrity_digest == packet.integrity_digest
    assert rebound.executing_model == "openrouter/stealth/ox-alpha"
    assert packet.executing_model is None


def test_transition_is_append_only_idempotent_and_updates_task_state() -> None:
    packet = ExecutionPacket.from_dict(packet_payload())
    transition = PacketTransition(
        transition_id="transition-test-started",
        task_id="T1",
        from_state="pending",
        to_state="active",
        reason="red test started",
        evidence_refs=("receipt-1",),
    )

    advanced = packet.transition(transition)
    replayed = advanced.transition(transition)

    assert advanced.tasks[0]["status"] == "active"
    assert advanced.packet_version == 2
    assert replayed == advanced


def test_changed_immutable_input_requires_new_version_and_refuses_resume() -> None:
    packet = ExecutionPacket.from_dict(packet_payload())
    changed = deepcopy(packet_payload())
    changed["intent"] = {**changed["intent"], "goal": "A different goal."}  # type: ignore[arg-type]

    with pytest.raises(ExecutionPacketError, match="immutable packet drift"):
        packet.validate_resume(ExecutionPacket.from_dict(changed))

    revised = packet.revise(intent=changed["intent"], reason="owner changed goal")
    assert revised.packet_version == packet.packet_version + 1
    assert revised.parent_integrity_digest == packet.integrity_digest
    assert revised.intent["goal"] == "A different goal."


def test_unknown_fields_are_rejected() -> None:
    payload = packet_payload()
    payload["surprise"] = True

    with pytest.raises(ExecutionPacketError, match="unknown field"):
        ExecutionPacket.from_dict(payload)


def test_packet_and_transition_receipts_are_idempotent_and_secret_free() -> None:
    packet = ExecutionPacket.from_dict(packet_payload())
    store = ReceiptStore(":memory:")

    first = packet.persist(store)
    replay = packet.persist(store)
    transition = PacketTransition(
        transition_id="transition-test-started",
        task_id="T1",
        from_state="pending",
        to_state="active",
        reason="red test started",
    )
    advanced, event = packet.persist_transition(store, transition)

    assert replay.receipt_id == first.receipt_id
    assert first.receipt_type == "manifest"
    assert first.payload["integrity_digest"] == packet.integrity_digest
    assert event.parent_receipt_id == first.receipt_id
    assert event.payload["transition"]["to_state"] == "active"
    assert advanced.receipt_refs[-1] == event.receipt_id
    serialized = json.dumps([first.payload, event.payload])
    for forbidden in ("prompt", "completion", "messages", "api_key", "tool_arguments"):
        assert forbidden not in serialized


def test_machine_readable_schema_accepts_canonical_packet() -> None:
    packet = ExecutionPacket.from_dict(packet_payload())
    schema_path = (
        Path(__file__).parents[1]
        / "specs"
        / "272-operational-routing-loop"
        / "contracts"
        / "execution-packet.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    assert list(Draft202012Validator(schema).iter_errors(packet.to_dict())) == []


def test_packet_file_store_create_inspect_validate_transition_and_resume(tmp_path: Path) -> None:
    store = ExecutionPacketStore(tmp_path)
    packet = ExecutionPacket.from_dict(packet_payload())

    path = store.create(packet)
    inspected = store.inspect(path)
    validated = store.validate(path)
    advanced = store.transition(
        path,
        PacketTransition(
            transition_id="transition-test-started",
            task_id="T1",
            from_state="pending",
            to_state="active",
            reason="red test started",
        ),
    )
    resumed = store.resume(path, executing_model="cc/claude-sonnet-5")

    assert path == tmp_path / "packet-headroom-unknown.json"
    assert inspected.integrity_digest == packet.integrity_digest
    assert validated == packet
    assert advanced.tasks[0]["status"] == "active"
    assert resumed.executing_model == "cc/claude-sonnet-5"
    assert resumed.integrity_digest == advanced.integrity_digest


def test_file_store_refuses_overwrite_and_tampered_packet(tmp_path: Path) -> None:
    store = ExecutionPacketStore(tmp_path)
    packet = ExecutionPacket.from_dict(packet_payload())
    path = store.create(packet)

    with pytest.raises(ExecutionPacketError, match="already exists"):
        store.create(packet)

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["next_safe_action"] = "tampered"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ExecutionPacketError, match="integrity digest mismatch"):
        store.validate(path)


def test_worktree_digest_changes_when_tracked_or_untracked_content_changes(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=tmp_path, check=True)

    clean = capture_worktree_digest(tmp_path)
    (tmp_path / "tracked.txt").write_text("changed\n", encoding="utf-8")
    tracked = capture_worktree_digest(tmp_path)
    (tmp_path / "untracked.txt").write_text("one\n", encoding="utf-8")
    untracked_one = capture_worktree_digest(tmp_path)
    (tmp_path / "untracked.txt").write_text("two\n", encoding="utf-8")
    untracked_two = capture_worktree_digest(tmp_path)

    assert len({clean, tracked, untracked_one, untracked_two}) == 4
    assert all(
        item.startswith("sha256:") for item in (clean, tracked, untracked_one, untracked_two)
    )


def test_source_binding_uses_exact_git_identity_and_lockfile_digests(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "tracked.txt").write_text("base\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text("lock\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt", "uv.lock"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=tmp_path, check=True)

    source = capture_source_binding(
        tmp_path, repository="git@example.com:example/repo.git", lock_paths=("uv.lock",)
    )

    assert source["repository"] == "git@example.com:example/repo.git"
    assert source["worktree"] == str(tmp_path.resolve())
    assert (
        source["commit"]
        == subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True
        ).stdout.strip()
    )
    assert source["branch"] in {"main", "master"}
    assert source["dirty_digest"] == capture_worktree_digest(tmp_path)
    assert source["lock_digests"]["uv.lock"].startswith("sha256:")
