import pytest

from verdict.failover_replay_proof import replay_proof, run_forced_failover_proof
from verdict.memory_plane import MemoryPlane


def test_forced_429_completes_and_replays_without_duplicate_commits(tmp_path):
    with MemoryPlane(tmp_path / "proof.db") as plane:
        proof = run_forced_failover_proof(plane)
    replayed = replay_proof(proof)
    assert proof.terminal_status == "completed"
    assert proof.completed_stages == ("prepare", "execute", "publish")
    assert proof.replacement_model == "provider-b/model-b"
    assert replayed.digest == proof.digest
    assert (
        sum(event.kind == "stage_completed" and event.stage == "prepare" for event in proof.events)
        == 1
    )


def test_forced_429_proof_is_repeatable_in_one_memory_plane(tmp_path):
    with MemoryPlane(tmp_path / "proof.db") as plane:
        first = run_forced_failover_proof(plane)
        second = run_forced_failover_proof(plane)

    assert second.mission_id != first.mission_id
    assert first.completed_stages == second.completed_stages
    assert first.replacement_model == second.replacement_model
    assert replay_proof(second).digest == second.digest


def test_replay_rejects_tampered_sequence(tmp_path):
    with MemoryPlane(tmp_path / "proof.db") as plane:
        proof = run_forced_failover_proof(plane)
    events = list(proof.events)
    events[1] = type(events[1])(
        events[1].seq + 7,
        events[1].mission_id,
        events[1].kind,
        events[1].stage,
        events[1].model,
        events[1].status,
        events[1].error_class,
    )
    tampered = type(proof)(
        proof.mission_id,
        tuple(events),
        proof.completed_stages,
        proof.replacement_model,
        proof.terminal_status,
    )
    with pytest.raises(ValueError, match="sequence"):
        replay_proof(tampered)
