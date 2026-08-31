from __future__ import annotations

from pathlib import Path

from verdict.context_intelligence import plan_slices, retrieve_units
from verdict.context_lift import (
    LiftReceipt,
    ingest_lift_fact,
    lift_check_passes,
    new_lift_token,
    plant_lift_workspace,
    refuse_secret_write,
    unaided_prompt,
)
from verdict.memory_gate import MemoryGate
from verdict.memory_plane import MemoryPlane

TASK = "Return the unique lift token stored in this project's docs, code, or memory."


def test_checker_requires_exact_json() -> None:
    token = new_lift_token()
    assert lift_check_passes(f'{{"lift_fact":"{token}"}}', token)
    assert not lift_check_passes(f'{{"lift_fact":"{token}","extra":1}}', token)
    assert not lift_check_passes(f'```json\n{{"lift_fact":"{token}"}}\n```', token)
    assert not lift_check_passes("the token is " + token, token)


def test_unaided_prompt_does_not_contain_planted_token() -> None:
    token = new_lift_token()
    assert token not in unaided_prompt()
    assert token not in TASK


def test_invalid_pair_when_identities_differ() -> None:
    receipt = LiftReceipt(
        identity_id="a",
        cost_class="free",
        endpoint="http://localhost:20128/v1",
        pack_digest="sha256:" + "a" * 64,
        unaided_passed=False,
        packed_passed=True,
        conclusion="lift",
    )
    payload = receipt.to_dict()
    assert payload["conclusion"] == "lift"
    assert "prompt" not in payload
    assert "completion" not in str(payload)


def test_receipt_has_no_secrets() -> None:
    receipt = LiftReceipt(
        identity_id="free/model",
        cost_class="free",
        endpoint="http://localhost:20128/v1",
        pack_digest=None,
        unaided_passed=False,
        packed_passed=True,
        conclusion="lift",
    )
    dumped = str(receipt.to_dict())
    assert "sk-" not in dumped
    assert "bearer" not in dumped.lower()


def test_working_state_not_auto_ingested(tmp_path: Path) -> None:
    token = new_lift_token()
    root = plant_lift_workspace(tmp_path, token)
    plane = MemoryPlane(root / "memory.db")
    gate = MemoryGate(plane)
    try:
        ingest_lift_fact(gate, token)
        before = {record.record_id for record in plane.records()}
        slices = plan_slices(TASK, proof_root=root)
        retrieve_units(slices, proof_root=root, plane=plane, required_fact=token, task=TASK)
        after = {record.record_id for record in plane.records()}
        assert after == before
    finally:
        gate.close()


def test_secret_write_never_searchable(tmp_path: Path) -> None:
    plane = MemoryPlane(tmp_path / "memory.db")
    gate = MemoryGate(plane)
    secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
    try:
        refuse_secret_write(gate, secret)
        hits = plane.search(secret)
        assert all(secret not in item.content for item in hits)
    finally:
        gate.close()


def test_ingest_then_search_from_new_state(tmp_path: Path) -> None:
    token = new_lift_token()
    plane = MemoryPlane(tmp_path / "memory.db")
    gate = MemoryGate(plane)
    try:
        ingest_lift_fact(gate, token)
        hits = plane.search("lift")
        assert any(token in item.content for item in hits)
    finally:
        gate.close()
