"""
Property tests for the eligibility-gate default construction fix: eligibility invariants

These tests verify key safety invariants in the eligibility and selection logic:
- Protected work with no availability source must fail closed
- Availability source None + protected + protected_fail_closed → no admission
- Multiple candidates: protected semantics apply uniformly
"""

from verdict.eligibility import EligibilityGate
from verdict.models import ModelInfo


def test_property_protected_fail_closed_admits_nothing_without_availability():
    """
    INVARIANT: When protected=True and availability_source=None and
    protected_fail_closed=True, NO candidate should ever be admitted,
    regardless of catalog state.

    This is the fail-closed safety property: we cannot verify runtime
    availability, so we exclude all candidates for protected work.
    """
    gate = EligibilityGate(
        availability_source=None,  # No live availability
        protected_fail_closed=True,
    )

    # Test with various candidate states
    test_cases = [
        ModelInfo(id="gpt-4", provider="openai", capabilities=["chat"], is_available=True),
        ModelInfo(id="claude-3", provider="anthropic", capabilities=["chat"], is_available=True),
        ModelInfo(id="gemini", provider="google", capabilities=["chat"], is_available=False),
        ModelInfo(id="unknown-model", provider="unknown", capabilities=[], is_available=True),
    ]

    for candidate in test_cases:
        result = gate.evaluate(
            candidates=[candidate],
            protected=True,  # Protected work
            dev_mode=False,
        )

        # INVARIANT: no candidate should be admitted
        assert len(result.admitted) == 0, (
            f"SAFETY VIOLATION: {candidate.id} admitted in protected fail-closed mode "
            f"without availability verification. This breaks the fail-closed guarantee."
        )

        # All records should show RUNTIME_TRUTH_ABSENT verdict
        for record in result.records:
            assert not record.admitted
            assert "protected work" in record.reason.lower()


def test_property_unprotected_with_no_availability_admits_catalog_candidates():
    """
    INVARIANT: When protected=False and availability_source=None,
    candidates in the catalog should be admitted (fail-open for explore/explain).

    This is the complement of fail-closed: non-protected work can proceed
    with catalog state when runtime verification is unavailable.
    """
    gate = EligibilityGate(availability_source=None, protected_fail_closed=True)

    candidates = [
        ModelInfo(id="gpt-4", provider="openai", capabilities=["chat"], is_available=True),
        ModelInfo(id="claude-3", provider="anthropic", capabilities=["chat"], is_available=True),
    ]

    result = gate.evaluate(
        candidates=candidates,
        protected=False,  # Not protected
        dev_mode=False,
    )

    # Should admit available candidates
    assert len(result.admitted) == 2
    for record in result.records:
        assert record.admitted


def test_property_protected_fail_closed_applies_uniformly_to_candidate_pool():
    """
    INVARIANT: The protected fail-closed property applies uniformly to all
    candidates in the pool, regardless of provider or capabilities.

    No candidate can slip through on a technicality.
    """
    gate = EligibilityGate(availability_source=None, protected_fail_closed=True)

    # Diverse candidate pool
    candidates = [
        ModelInfo(id="gpt-4", provider="openai", capabilities=["chat", "vision"]),
        ModelInfo(id="claude-3-opus", provider="anthropic", capabilities=["chat"]),
        ModelInfo(id="gemini-pro", provider="google", capabilities=["chat", "code"]),
        ModelInfo(id="llama-3", provider="meta", capabilities=["chat"]),
        ModelInfo(id="mixtral", provider="mistral", capabilities=["chat"]),
    ]

    result = gate.evaluate(candidates=candidates, protected=True, dev_mode=False)

    # INVARIANT: zero candidates admitted
    assert len(result.admitted) == 0, (
        "Protected fail-closed must reject ALL candidates uniformly when "
        "availability_source is None, regardless of provider/capabilities."
    )

    # All records must show consistent fail-closed verdict
    assert len(result.records) == len(candidates)
    for record in result.records:
        assert not record.admitted
        assert "protected work" in record.reason.lower()
        assert "availability" in record.reason.lower()
