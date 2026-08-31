from __future__ import annotations

from pathlib import Path

import pytest

from verdict.context_intelligence import (
    ContextIntelligenceError,
    RetrievalSlice,
    compile_pack,
    plan_slices,
    retrieve_units,
)
from verdict.context_lift import ingest_lift_fact, new_lift_token, plant_lift_workspace
from verdict.context_pack import ContextUnit
from verdict.memory_gate import MemoryGate
from verdict.memory_plane import MemoryPlane

TASK = "Return the unique lift token stored in this project's docs, code, or memory."


def _workspace(tmp_path: Path) -> tuple[Path, str, MemoryPlane]:
    token = new_lift_token()
    root = plant_lift_workspace(tmp_path, token, dummy_files=40)
    plane = MemoryPlane(root / "memory.db")
    ingest_lift_fact(MemoryGate(plane), token)
    return root, token, plane


def _unit(key: str, content: str, slot: str = "evidence") -> ContextUnit:
    import hashlib

    digest = f"sha256:{hashlib.sha256(content.encode()).hexdigest()}"
    return ContextUnit(
        unit_id=f"u:{key}",
        slot_type=slot,  # type: ignore[arg-type]
        key=key,
        content=content,
        source_uri=f"urn:{key}",
        source_digest=digest,
    )


def test_plan_slices_are_deterministic(tmp_path: Path) -> None:
    first = plan_slices(TASK, proof_root=tmp_path)
    second = plan_slices(TASK, proof_root=tmp_path)
    assert first == second
    assert {item.category for item in first} == {"docs", "code", "memory"}


def test_retrieve_planted_facts_and_exclude_bulk(tmp_path: Path) -> None:
    root, token, plane = _workspace(tmp_path)
    try:
        slices = plan_slices(TASK, proof_root=root)
        result = retrieve_units(
            slices, proof_root=root, plane=plane, required_fact=token, task=TASK
        )
        joined = "\n".join(unit.content for unit in result.units)
        assert token in joined
        assert result.file_count >= 20
        assert len(result.units) / result.file_count <= 0.10
        sources = {unit.source_uri for unit in result.units}
        assert not any("noise_00.py" in source for source in sources)
    finally:
        plane.close()


def test_repo_dump_slice_refused(tmp_path: Path) -> None:
    with pytest.raises(ContextIntelligenceError, match="dump") as exc:
        retrieve_units(
            (RetrievalSlice("dump", "code", query="", root=str(tmp_path.resolve()), max_units=8),),
            proof_root=tmp_path,
        )
    assert exc.value.code == "repo_dump_refused"


def test_docs_omission_named_when_missing(tmp_path: Path) -> None:
    slices = plan_slices(TASK, proof_root=tmp_path)
    result = retrieve_units(slices, proof_root=tmp_path, task=TASK)
    reasons = {(item.category, item.reason) for item in result.omissions}
    assert ("docs", "no_default_location") in reasons or ("docs", "not_found") in reasons


def test_compile_keeps_required_fact_within_budget(tmp_path: Path) -> None:
    token = new_lift_token()
    units = (
        _unit("goal", TASK, "instructions"),
        _unit("docs:adr", f"The unique lift token is: {token}"),
        _unit("noise", "x" * 4000),
    )
    pack, state = compile_pack(
        units, token_budget=800, required_fact=token, candidate_id="free/test"
    )
    assert pack.used_tokens <= 800
    assert token in pack.compiled_prompt
    assert state.required_fact_kept is True
    assert "sk-" not in pack.canonical_json()


def test_compile_refuses_when_required_fact_cannot_fit() -> None:
    token = "fact-" + ("z" * 400)
    units = (_unit("goal", TASK, "instructions"), _unit("docs:adr", token))
    with pytest.raises(ContextIntelligenceError) as exc:
        compile_pack(units, token_budget=40, required_fact=token, candidate_id="free/test")
    assert exc.value.code in {"required_fact_omitted", "required_fact_missing"}


def test_compile_excludes_secrets() -> None:
    token = new_lift_token()
    units = (
        _unit("goal", TASK, "instructions"),
        _unit("docs:adr", f"The unique lift token is: {token}"),
        _unit("leak", "api_key=sk-abcdefghijklmnopqrstuvwxyz0123"),
    )
    pack, _state = compile_pack(
        units, token_budget=800, required_fact=token, candidate_id="free/test"
    )
    assert "sk-abcdefghijklmnopqrstuvwxyz0123" not in pack.compiled_prompt
    assert any(decision.action == "exclude" for decision in pack.decisions)


def test_compile_records_conflicts() -> None:
    token = new_lift_token()
    units = (
        _unit("docs:same", f"The unique lift token is: {token}"),
        _unit("docs:same", "contradictory other content"),
    )
    pack, _state = compile_pack(
        units, token_budget=800, required_fact=token, candidate_id="free/test"
    )
    assert pack.conflicts


def test_compaction_keeps_required_fact() -> None:
    token = new_lift_token()
    units = (
        _unit("goal", TASK, "instructions"),
        _unit("docs:adr", f"The unique lift token is: {token}"),
        _unit("noise", "unrelated " * 200),
    )
    pack, _state = compile_pack(
        units, token_budget=400, required_fact=token, candidate_id="free/test", compaction=True
    )
    assert token in pack.compiled_prompt
    assert pack.used_tokens <= 400
