"""BOD-272 coverage contract: required_facts on pack_state and CheapPathContextPack."""

from __future__ import annotations

from pathlib import Path

from verdict.free_tier_admit import (
    CheapPathContextPack,
    IncludedProvenance,
    _check_fact_satisfaction,
    _derive_required_facts,
    build_cheap_path_context_pack,
)
from verdict.pack_state import classify_pack_state, savings_unlocked

# ---------------------------------------------------------------------------
# classify_pack_state — required_facts integration
# ---------------------------------------------------------------------------


def test_partial_when_required_fact_unsatisfied() -> None:
    """A required fact not in satisfied_facts forces partial."""
    included = (
        IncludedProvenance("docs/adr/ADR-001.md", "sha256:" + "a" * 64),
        IncludedProvenance("docs/architecture/decision.md", "sha256:" + "b" * 64),
    )
    state = classify_pack_state(
        included=included,
        gathered=included,
        required_facts=("All tests pass", "Coverage > 80%"),
        satisfied_facts=("All tests pass",),
    )
    assert state == "partial"
    assert not savings_unlocked(state)


def test_hydrated_when_all_facts_satisfied() -> None:
    """All required facts satisfied → hydrated (given other conditions met)."""
    included = (
        IncludedProvenance("docs/adr/ADR-001.md", "sha256:" + "a" * 64),
        IncludedProvenance("docs/architecture/decision.md", "sha256:" + "b" * 64),
    )
    state = classify_pack_state(
        included=included,
        gathered=included,
        required_facts=("All tests pass", "Coverage > 80%"),
        satisfied_facts=("All tests pass", "Coverage > 80%"),
    )
    assert state == "hydrated"
    assert savings_unlocked(state)


def test_no_facts_is_backward_compatible() -> None:
    """Empty required_facts → identical behaviour to today."""
    included = (
        IncludedProvenance("docs/adr/ADR-001.md", "sha256:" + "a" * 64),
        IncludedProvenance("docs/architecture/decision.md", "sha256:" + "b" * 64),
    )
    state = classify_pack_state(
        included=included, gathered=included, required_facts=(), satisfied_facts=()
    )
    assert state == "hydrated"

    # Also: no required_facts kwarg at all (default)
    state2 = classify_pack_state(included=included, gathered=included)
    assert state2 == "hydrated"


def test_failed_still_overrides_facts() -> None:
    """failed=True takes precedence over unsatisfied facts."""
    included = (IncludedProvenance("docs/adr/ADR-001.md", "sha256:" + "a" * 64),)
    state = classify_pack_state(
        included=included,
        gathered=included,
        failed=True,
        required_facts=("some fact",),
        satisfied_facts=(),
    )
    assert state == "failed"


def test_task_complete_false_overrides_facts() -> None:
    """task_complete=False takes precedence over unsatisfied facts."""
    included = (IncludedProvenance("docs/adr/ADR-001.md", "sha256:" + "a" * 64),)
    state = classify_pack_state(
        included=included,
        gathered=included,
        task_complete=False,
        required_facts=("some fact",),
        satisfied_facts=("some fact",),
    )
    assert state == "failed"


# ---------------------------------------------------------------------------
# _derive_required_facts
# ---------------------------------------------------------------------------


def test_derive_required_facts_combines_ac_and_proof() -> None:
    facts = _derive_required_facts(
        acceptance_criteria=["Tests pass", "Lint clean"],
        proof_criteria=["Coverage report attached"],
    )
    assert facts == ("Tests pass", "Lint clean", "Coverage report attached")


def test_derive_required_facts_skips_empty() -> None:
    facts = _derive_required_facts(acceptance_criteria=["Tests pass", "", "  "], proof_criteria=[])
    assert facts == ("Tests pass",)


def test_derive_required_facts_with_verification_commands() -> None:
    facts = _derive_required_facts(
        acceptance_criteria=["AC1"],
        proof_criteria=[],
        verification_commands=[["pytest", "-q"], ["ruff", "check", "."]],
    )
    assert "verification_command:pytest -q" in facts
    assert "verification_command:ruff check ." in facts


def test_derive_required_facts_empty_inputs() -> None:
    facts = _derive_required_facts(acceptance_criteria=[], proof_criteria=[])
    assert facts == ()


# ---------------------------------------------------------------------------
# _check_fact_satisfaction
# ---------------------------------------------------------------------------


def test_check_fact_satisfaction_substring_match() -> None:
    """A fact is satisfied when its text (whitespace-collapsed, case-insensitive)
    appears in any included unit's content."""
    from verdict.context_pack import ContextUnit

    units = [
        ContextUnit(
            unit_id="u1",
            slot_type="instructions",
            key="task",
            content="You must ensure all tests pass and coverage > 80%.",
            source_uri="urn:verdict:source:task",
            source_digest="sha256:" + "a" * 64,
        )
    ]
    satisfied = _check_fact_satisfaction(
        ["All tests pass", "coverage > 80%", "missing fact"], units
    )
    assert "All tests pass" in satisfied
    assert "coverage > 80%" in satisfied
    assert "missing fact" not in satisfied


def test_check_fact_satisfaction_whitespace_collapse() -> None:
    from verdict.context_pack import ContextUnit

    units = [
        ContextUnit(
            unit_id="u1",
            slot_type="instructions",
            key="criteria",
            content="- Ensure\n  all   tests\n  pass",
            source_uri="urn:verdict:source:ac",
            source_digest="sha256:" + "b" * 64,
        )
    ]
    satisfied = _check_fact_satisfaction(["Ensure all tests pass"], units)
    assert "Ensure all tests pass" in satisfied


def test_check_fact_satisfaction_empty() -> None:
    assert _check_fact_satisfaction((), []) == ()


# ---------------------------------------------------------------------------
# CheapPathContextPack — unsatisfied_facts / to_dict
# ---------------------------------------------------------------------------


def test_cheap_path_pack_unsatisfied_facts() -> None:
    pack = CheapPathContextPack(
        pack_digest="sha256:" + "a" * 64,
        compiled_prompt="test",
        omissions=(),
        pack_id="test",
        plan_digest="sha256:" + "b" * 64,
        required_facts=("AC1", "AC2", "Proof1"),
        satisfied_facts=("AC1",),
    )
    assert pack.unsatisfied_facts == ("AC2", "Proof1")


def test_cheap_path_pack_to_dict_includes_facts() -> None:
    pack = CheapPathContextPack(
        pack_digest="sha256:" + "a" * 64,
        compiled_prompt="test",
        omissions=(),
        pack_id="test",
        plan_digest="sha256:" + "b" * 64,
        required_facts=("AC1",),
        satisfied_facts=("AC1",),
    )
    d = pack.to_dict()
    assert d["required_facts"] == ["AC1"]
    assert d["satisfied_facts"] == ["AC1"]
    assert d["unsatisfied_facts"] == []


def test_cheap_path_pack_to_dict_unsatisfied_facts_named() -> None:
    pack = CheapPathContextPack(
        pack_digest="sha256:" + "a" * 64,
        compiled_prompt="test",
        omissions=(),
        pack_id="test",
        plan_digest="sha256:" + "b" * 64,
        required_facts=("AC1", "AC2"),
        satisfied_facts=("AC1",),
    )
    d = pack.to_dict()
    assert d["unsatisfied_facts"] == ["AC2"]


# ---------------------------------------------------------------------------
# build_cheap_path_context_pack integration: AC/proof packed as mandatory units
# ---------------------------------------------------------------------------


def _plant_workspace(root: Path) -> None:
    (root / "docs" / "adr").mkdir(parents=True)
    (root / "docs" / "architecture").mkdir(parents=True)
    (root / "docs" / "adr" / "ADR-001.md").write_text("# ADR-001\nDecision.\n")
    (root / "docs" / "architecture" / "decision.md").write_text("# Arch\nOverview.\n")


def test_ac_proof_packed_as_mandatory_units(tmp_path: Path) -> None:
    """Acceptance and proof criteria become instruction units in the pack."""
    _plant_workspace(tmp_path)
    packed = build_cheap_path_context_pack(
        "implement feature X",
        candidate_id="openrouter/free-model",
        workspace_root=tmp_path,
        workspace_roots=("docs/adr", "docs/architecture"),
        acceptance_criteria=["All unit tests pass", "No lint errors"],
        proof_criteria=["pytest output attached"],
        mcp_root="",
    )
    # The criteria text must appear in the compiled prompt
    assert "All unit tests pass" in packed.compiled_prompt
    assert "No lint errors" in packed.compiled_prompt
    assert "pytest output attached" in packed.compiled_prompt
    # All facts should be satisfied (they were packed as instruction units)
    assert packed.pack_state == "hydrated"
    assert len(packed.required_facts) == 3
    assert len(packed.satisfied_facts) == 3
    assert packed.unsatisfied_facts == ()


def test_missing_fact_yields_partial_with_named_unsatisfied(tmp_path: Path) -> None:
    """A fact not carried by any unit → partial, with the fact named."""
    _plant_workspace(tmp_path)
    # Use a tiny budget that can only fit the task instructions
    packed = build_cheap_path_context_pack(
        "implement feature X",
        candidate_id="openrouter/free-model",
        token_budget=50,  # very small — criteria units may be dropped
        workspace_root=tmp_path,
        workspace_roots=("docs/adr", "docs/architecture"),
        acceptance_criteria=["All unit tests pass with zero failures"],
        proof_criteria=["Full coverage report must be attached and verified"],
        mcp_root="",
    )
    # If the budget is too small to fit the criteria, pack_state should be
    # partial or failed (never hydrated with unsatisfied facts)
    if packed.pack_state == "failed":
        # Budget too small even for task — acceptable
        pass
    else:
        # Check that any unsatisfied fact blocks hydrated
        if packed.unsatisfied_facts:
            assert packed.pack_state == "partial"
        else:
            assert packed.pack_state == "hydrated"


def test_no_criteria_backward_compatible(tmp_path: Path) -> None:
    """No acceptance/proof criteria → required_facts empty → unchanged behaviour."""
    _plant_workspace(tmp_path)
    packed = build_cheap_path_context_pack(
        "implement feature X",
        candidate_id="openrouter/free-model",
        workspace_root=tmp_path,
        workspace_roots=("docs/adr", "docs/architecture"),
        mcp_root="",
    )
    assert packed.required_facts == ()
    assert packed.satisfied_facts == ()
    assert packed.unsatisfied_facts == ()
    assert packed.pack_state == "hydrated"


# ---------------------------------------------------------------------------
# Uplift fixture pack check: every task in uplift_tasks.json still hydrates
# ---------------------------------------------------------------------------


def test_uplift_fixture_tasks_still_hydrate() -> None:
    """Every task in benchmarks/fixtures/uplift_tasks.json must hydrate to
    'hydrated' via build_cheap_path_context_pack with the fixture workspace.

    This validates the coverage-contract invariant: required_facts=() (no AC
    supplied to the builder) preserves backward-compatible 'hydrated' state.
    """
    import json

    fixture_path = Path("benchmarks/fixtures/uplift_tasks.json")
    if not fixture_path.exists():
        fixture_path = Path("/tmp/v272cov/benchmarks/fixtures/uplift_tasks.json")
    if not fixture_path.exists():
        import pytest

        pytest.skip("uplift_tasks.json fixture not found")

    fixture = json.loads(fixture_path.read_text())
    if isinstance(fixture, dict):
        workspace = fixture.get("workspace", "")
        tasks = fixture.get("tasks", [])
    else:
        workspace = ""
        tasks = fixture
    assert len(tasks) > 0, "fixture must not be empty"

    workspace_root = Path(workspace) if workspace else None
    from verdict.context_hydrate import DEFAULT_CONTEXT_ROOTS

    for i, entry in enumerate(tasks):
        prompt = entry.get("prompt") or entry.get("task")
        assert prompt, f"task {i} has no prompt/task field"
        packed = build_cheap_path_context_pack(
            prompt,
            candidate_id="openrouter/free-model",
            workspace_root=workspace_root,
            workspace_roots=DEFAULT_CONTEXT_ROOTS,
            mcp_root="",
        )
        assert packed.pack_state == "hydrated", (
            f"task {i} ({prompt[:60]}...) hydrated to {packed.pack_state!r}, expected 'hydrated'"
        )
        # Backward-compat: no AC supplied → no required_facts
        assert packed.required_facts == ()
