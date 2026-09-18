"""BOD-106 pack_state classifier: empty / partial / hydrated / failed."""

from __future__ import annotations

from verdict.free_tier_admit import IncludedProvenance, NamedOmission
from verdict.pack_state import classify_pack_state, savings_unlocked


def test_empty_when_zero_includes_even_with_digest_and_omissions() -> None:
    state = classify_pack_state(
        included=(),
        gathered=(),
        omissions=(NamedOmission(name="docs/adr", reason="source_missing"),),
    )
    assert state == "empty"
    assert not savings_unlocked(state)


def test_hydrated_when_present_adr_and_architecture_are_included() -> None:
    included = (
        IncludedProvenance("docs/adr/ADR-001.md", "sha256:" + "a" * 64),
        IncludedProvenance("docs/architecture/decision.md", "sha256:" + "b" * 64),
    )
    gathered = included
    state = classify_pack_state(included=included, gathered=gathered, omissions=())
    assert state == "hydrated"
    assert savings_unlocked(state)


def test_hydrated_when_missing_architecture_root_is_named_omission_only() -> None:
    """Invent-never: absent roots do not block hydrated."""
    included = (IncludedProvenance("docs/adr/ADR-001.md", "sha256:" + "a" * 64),)
    state = classify_pack_state(
        included=included,
        gathered=included,
        omissions=(NamedOmission(name="docs/architecture", reason="source_missing"),),
    )
    assert state == "hydrated"


def test_readme_is_optional_for_hydrated() -> None:
    included = (
        IncludedProvenance("docs/adr/ADR-001.md", "sha256:" + "a" * 64),
        IncludedProvenance("docs/architecture/decision.md", "sha256:" + "b" * 64),
    )
    gathered = (*included, IncludedProvenance("README.md", "sha256:" + "c" * 64))
    omissions = (NamedOmission(name="README.md", reason="input_budget_exhausted"),)
    state = classify_pack_state(included=included, gathered=gathered, omissions=omissions)
    assert state == "hydrated"


def test_partial_when_architecture_exists_but_was_budget_omitted() -> None:
    included = (IncludedProvenance("docs/adr/ADR-001.md", "sha256:" + "a" * 64),)
    gathered = (
        included[0],
        IncludedProvenance("docs/architecture/decision.md", "sha256:" + "b" * 64),
    )
    omissions = (
        NamedOmission(name="docs/architecture/decision.md", reason="input_budget_exhausted"),
    )
    state = classify_pack_state(included=included, gathered=gathered, omissions=omissions)
    assert state == "partial"
    assert not savings_unlocked(state)


def test_failed_takes_precedence() -> None:
    included = (
        IncludedProvenance("docs/adr/ADR-001.md", "sha256:" + "a" * 64),
        IncludedProvenance("docs/architecture/decision.md", "sha256:" + "b" * 64),
    )
    assert classify_pack_state(included=included, gathered=included, failed=True) == "failed"
    assert not savings_unlocked("failed")
    assert not savings_unlocked(None)
    assert not savings_unlocked("empty")
