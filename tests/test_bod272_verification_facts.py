"""BOD-272 follow-up: verification commands as mandatory facts in production."""

from __future__ import annotations

import json
from pathlib import Path

from verdict.free_tier_admit import (
    IncludedProvenance,
    _derive_required_facts,
    build_cheap_path_context_pack,
)
from verdict.pack_state import classify_pack_state


def _plant_workspace(root: Path) -> None:
    (root / "docs" / "adr").mkdir(parents=True)
    (root / "docs" / "architecture").mkdir(parents=True)
    (root / "docs" / "adr" / "ADR-001.md").write_text("# ADR-001\nDecision.\n")
    (root / "docs" / "architecture" / "decision.md").write_text("# Arch\nOverview.\n")


# ---------------------------------------------------------------------------
# _derive_required_facts includes verification_commands
# ---------------------------------------------------------------------------


def test_verification_commands_become_mandatory_facts() -> None:
    """Each declared verification command becomes a mandatory fact."""
    facts = _derive_required_facts(
        acceptance_criteria=("AC1",),
        proof_criteria=("PC1",),
        verification_commands=[["pytest", "-q"], ["ruff", "check", "."]],
    )
    assert "AC1" in facts
    assert "PC1" in facts
    assert "verification_command:pytest -q" in facts
    assert "verification_command:ruff check ." in facts


def test_no_verification_commands_unchanged() -> None:
    """Absent verification_commands -> same behaviour as before."""
    facts_without = _derive_required_facts(acceptance_criteria=("AC1",), proof_criteria=())
    facts_with_empty = _derive_required_facts(
        acceptance_criteria=("AC1",), proof_criteria=(), verification_commands=()
    )
    assert facts_without == facts_with_empty
    assert "verification_command" not in " ".join(facts_without)


def test_empty_commands_skipped() -> None:
    """Empty or whitespace-only commands are not added as facts."""
    facts = _derive_required_facts(
        acceptance_criteria=(), proof_criteria=(), verification_commands=[[], ["  "]]
    )
    assert len(facts) == 0


# ---------------------------------------------------------------------------
# build_cheap_path_context_pack: verification_commands flow end-to-end
# ---------------------------------------------------------------------------


def test_pack_with_verification_command_hydrates(tmp_path: Path) -> None:
    """A task with a verification command packs the command as a mandatory
    unit and reaches hydrated when the fact is satisfiable."""
    _plant_workspace(tmp_path)
    pack = build_cheap_path_context_pack(
        "Implement the widget",
        candidate_id="test/model-a",
        token_budget=200_000,
        acceptance_criteria=("Widget works",),
        verification_commands=[["pytest", "-q", "tests/"]],
        workspace_root=tmp_path,
        workspace_roots=("docs/adr", "docs/architecture"),
    )
    # The verification_command fact must be in required_facts
    assert any("verification_command:pytest -q tests/" in f for f in pack.required_facts)
    # The fact must be satisfied (content slot carries it)
    assert any("verification_command:pytest -q tests/" in f for f in pack.satisfied_facts)
    # Pack must reach hydrated (task + criteria carried)
    assert pack.pack_state == "hydrated"


def test_unsatisfied_verification_fact_yields_partial() -> None:
    """A verification fact present in required_facts but not in satisfied_facts
    forces classify_pack_state to return partial."""
    included = (
        IncludedProvenance("docs/adr/ADR-001.md", "sha256:" + "a" * 64),
        IncludedProvenance("docs/architecture/decision.md", "sha256:" + "b" * 64),
    )
    state = classify_pack_state(
        included=included,
        gathered=included,
        required_facts=("verification_command:pytest -q",),
        satisfied_facts=(),
    )
    assert state == "partial"


def test_satisfied_verification_fact_allows_hydrated() -> None:
    """When all verification facts are satisfied, pack can reach hydrated."""
    included = (
        IncludedProvenance("docs/adr/ADR-001.md", "sha256:" + "a" * 64),
        IncludedProvenance("docs/architecture/decision.md", "sha256:" + "b" * 64),
    )
    state = classify_pack_state(
        included=included,
        gathered=included,
        required_facts=("verification_command:pytest -q",),
        satisfied_facts=("verification_command:pytest -q",),
    )
    assert state == "hydrated"


def test_pack_no_verification_commands_unchanged(tmp_path: Path) -> None:
    """No verification_commands -> pack behaviour unchanged from baseline."""
    _plant_workspace(tmp_path)
    pack = build_cheap_path_context_pack(
        "Implement the widget",
        candidate_id="test/model-a",
        token_budget=200_000,
        workspace_root=tmp_path,
        workspace_roots=("docs/adr", "docs/architecture"),
    )
    vc_facts = [f for f in pack.required_facts if f.startswith("verification_command:")]
    assert len(vc_facts) == 0


# ---------------------------------------------------------------------------
# Uplift tasks fixture: still hydrates with no regressions
# ---------------------------------------------------------------------------

UPLIFT_FIXTURE = (
    Path(__file__).resolve().parent.parent / "benchmarks" / "fixtures" / "uplift_tasks.json"
)


def test_uplift_tasks_still_hydrate(tmp_path: Path) -> None:
    """The 6 uplift tasks must still hydrate -- no regression from adding
    verification_commands support."""
    if not UPLIFT_FIXTURE.exists():
        import pytest

        pytest.skip(f"fixture not found: {UPLIFT_FIXTURE}")
    data = json.loads(UPLIFT_FIXTURE.read_text())
    tasks = data.get("tasks", data) if isinstance(data, dict) else data
    assert len(tasks) >= 6, f"expected >=6 uplift tasks, got {len(tasks)}"
    for i, entry in enumerate(tasks):
        task_text = entry.get(
            "prompt", entry.get("task", entry.get("objective", f"uplift-task-{i}"))
        )
        ac = entry.get("acceptance_criteria", ())
        pc = entry.get("proof_criteria", ())
        ws = tmp_path / f"uplift-{i}"
        _plant_workspace(ws)
        pack = build_cheap_path_context_pack(
            task_text,
            candidate_id="test/uplift-model",
            token_budget=200_000,
            acceptance_criteria=ac,
            proof_criteria=pc,
            workspace_root=ws,
            workspace_roots=("docs/adr", "docs/architecture"),
        )
        assert pack.pack_state == "hydrated", (
            f"uplift task {i} ({task_text[:40]}...) did not hydrate: {pack.pack_state}"
        )
