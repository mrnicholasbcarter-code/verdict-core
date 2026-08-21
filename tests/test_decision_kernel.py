"""Deterministic DecisionKernel facade tests (feature 003, VER-002 #219).

Covers the four decision journeys P1-P4 plus the adversarial/advisory legs:

* P1 (FR-001/FR-002/SC-003): the authority accepts a fully-eligible decision,
  the id is stable across identical calls, and the receipt verifies from inputs.
* P2 (FR-005/SC-002): advisory ranking reorders admitted candidates only; it
  cannot admit an excluded candidate, drop membership, or change the hard outcome.
* P3 (FR-006/FR-007/NFR-002): denied (no capable candidate) and degraded
  (preferred fail-closed, surviving subset) are reachable; absent/unavailable
  truth under a protected task excludes (fail-closed).
* P4 (FR-004/SC-001): independent verification detects every tamper class from
  inputs alone with no producer trust.

All tests are credential-free and offline (FR-009, NFR-001/NFR-003): the
``no_network`` fixture monkeypatches subprocess/network entrypoints to fail
loudly, proving the kernel performs no network or credential access.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from verdict.adaptive_ranker import AdaptiveRanker, RankerOutput
from verdict.availability import AvailabilityCandidate, AvailabilityReport, AvailabilityState
from verdict.contracts import TaskSpec
from verdict.decision_kernel import (
    OUTCOME_ACCEPTED,
    OUTCOME_DEGRADED,
    OUTCOME_DENIED,
    AdvisoryInput,
    VerificationFault,
    decide,
    verify_decision,
)
from verdict.decision_kernel_demo import (
    build_demo_decision,
    build_demo_decision_with_advisory_promoting_excluded,
    degraded_decision,
    degraded_inputs,
    demo_inputs,
    denied_decision,
    denied_inputs,
)
from verdict.eligibility import EligibilityResult
from verdict.models import ModelInfo

# ---------------------------------------------------------------------------
# Helpers to build minimal in-memory inputs (no network, no credentials).
# ---------------------------------------------------------------------------


def _capable_catalog() -> list[ModelInfo]:
    return [
        ModelInfo(
            id="p/sonnet", provider="p", capability_tier=3, capabilities=frozenset({"code-review"})
        ),
        ModelInfo(
            id="p/haiku", provider="p", capability_tier=2, capabilities=frozenset({"code-review"})
        ),
    ]


def _task_review() -> TaskSpec:
    """A code-review task; constructed directly (no from_dict -> no enum check)."""

    return TaskSpec(
        objective="Review a small diff for correctness",
        task_type="review",
        required_capabilities=["code-review"],
        privacy="standard",
        risk="low",
    )


def _report(model: ModelInfo, state: AvailabilityState) -> AvailabilityReport:
    cands = (AvailabilityCandidate(model=model, state=state, source="fixture"),)
    return AvailabilityReport(
        candidates=cands,
        eligible=cands if state is AvailabilityState.READY else (),
        source="fixture",
        freshness_seconds=0.0,
    )


def _healthy_truth(catalog: list[ModelInfo]) -> dict[str, AvailabilityReport]:
    return {m.id: _report(m, AvailabilityState.READY) for m in catalog}


# ---------------------------------------------------------------------------
# Adversarial ranker stubs (P2): override rank() to attempt policy weakening.
# ---------------------------------------------------------------------------


def _make_ranker(ranked: list[ModelInfo]) -> AdaptiveRanker:
    """An AdaptiveRanker whose .rank() returns ``ranked`` verbatim.

    The facade's membership-invariant reorder must refuse any candidate in
    ``ranked`` that the hard gate did not admit (SC-002, 100% of attempts).
    """

    ranker = AdaptiveRanker()
    ids = [m.id for m in ranked]

    def _rank(
        self: AdaptiveRanker, eligibility_result: EligibilityResult, task_spec: Any
    ) -> RankerOutput:
        return RankerOutput(
            ranked=ranked,
            scores={mid: 1.0 for mid in ids},
            reasoning={mid: "reorder" for mid in ids},
            candidate_set_hash="",
            eligibility_hash="",
            mode=self.config.mode,
            shadow=True,
            version="test-stub",
            canary_policy=self.config.canary_policy,
        )

    # Bound method with the captured ``ranked``.
    ranker.rank = _rank.__get__(ranker, AdaptiveRanker)  # type: ignore[method-assign]
    return ranker


# ---------------------------------------------------------------------------
# NFR-003 / FR-009: no network, no credentials, no producer trust.
# ---------------------------------------------------------------------------


def _raise_net(*_a: Any, **_k: Any) -> Any:  # pragma: no cover - guard
    raise AssertionError("network access forbidden (NFR-003, FR-009)")


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if any test exercises network or subprocess access."""

    monkeypatch.setattr("socket.socket", _raise_net, raising=False)
    monkeypatch.setattr("urllib.request.urlopen", _raise_net, raising=False)
    monkeypatch.setattr("http.client.HTTPConnection", _raise_net, raising=False)

    def _fail(*_a: Any, **_k: Any) -> None:
        raise AssertionError("subprocess forbidden (NFR-003)")

    monkeypatch.setattr("subprocess.run", _fail, raising=False)
    monkeypatch.setattr("subprocess.Popen", _fail, raising=False)


# ---------------------------------------------------------------------------
# P1: authority accepts; id stable; receipt verifies from inputs.
# ---------------------------------------------------------------------------


class TestP1Accepted:
    """P1 -- the verifier accepts a fully-eligible decision and trusts its receipt."""

    def test_accepted_outcome_and_admitted_set(self, no_network: None) -> None:
        record = build_demo_decision()
        assert record.outcome == OUTCOME_ACCEPTED
        assert len(record.admitted) >= 1
        assert isinstance(record.exclusions, list)
        assert record.decision_id.startswith("sha256:")

    def test_id_and_digest_stable_across_calls(self, no_network: None) -> None:
        a = build_demo_decision()
        b = build_demo_decision()
        # SC-003: byte-equal decision_id + integrity_digest across identical calls.
        assert a.decision_id == b.decision_id
        assert a.receipt.integrity_digest == b.receipt.integrity_digest
        assert a.receipt.admitted_set == b.receipt.admitted_set
        assert a.receipt.exclusions == b.receipt.exclusions

    def test_receipt_independent_verify_clean(self, no_network: None) -> None:
        record = build_demo_decision()
        # SC-001: verify from inputs alone, no producer trust.
        assert verify_decision(record, **demo_inputs()) is None

    def test_rdc_projection_carries_decision_id_and_receipt(self, no_network: None) -> None:
        record = build_demo_decision()
        rc = record.route_contract
        # FR-008: additive fields projected onto the existing contract.
        assert rc.decision_id == record.decision_id
        assert isinstance(rc.receipt, dict)
        assert rc.receipt.get("decision_id") == record.decision_id
        assert rc.receipt.get("integrity_digest") == record.receipt.integrity_digest


# ---------------------------------------------------------------------------
# P2: advisory cannot weaken hard policy.
# ---------------------------------------------------------------------------


class TestP2AdvisoryCannotWeaken:
    """P2 -- hard policy survives advisory influence (SC-002)."""

    def test_advisory_membership_invariant(self, no_network: None) -> None:
        base = build_demo_decision()
        adv = build_demo_decision_with_advisory_promoting_excluded()
        # FR-005: admitted membership + exclusion list identical advisory-on/off.
        assert {c.id for c in base.admitted} == {c.id for c in adv.admitted}
        assert {x["model_id"] for x in base.exclusions} == {x["model_id"] for x in adv.exclusions}
        # Only the advisory_influence differs (advisory-on records it).
        assert adv.advisory_influence is not None
        assert base.advisory_influence is None or base.advisory_influence != adv.advisory_influence

    def test_advisory_cannot_change_id(self, no_network: None) -> None:
        # The id binds membership, not order; advisory reorder must not perturb it.
        base = build_demo_decision()
        adv = build_demo_decision_with_advisory_promoting_excluded()
        assert base.decision_id == adv.decision_id
        assert adv.receipt.advisory_consulted is not None

    def test_advisory_reorder_preserves_all_admitted(self, no_network: None) -> None:
        catalog = _capable_catalog()
        truth = _healthy_truth(catalog)
        reversed_ranker = _make_ranker(list(reversed(catalog)))
        advisory = AdvisoryInput(
            ranker=reversed_ranker, requested_tier=3, escalation=(None, None), label="reverse"
        )
        record = decide(
            task_spec=_task_review(),
            policy_version="1",
            candidates=catalog,
            availability_truth=truth,
            protected=False,
            advisory=advisory,
        )
        # Advisory reordered both admitted candidates; membership unchanged.
        assert {c.id for c in record.admitted} == {m.id for m in catalog}
        assert record.outcome == OUTCOME_ACCEPTED
        assert record.advisory_influence is not None

    def test_advisory_promoting_excluded_is_refused(self, no_network: None) -> None:
        """An adversarial ranker that tries to admit an excluded candidate is refused."""

        catalog = _capable_catalog()
        sonnet, haiku = catalog
        # Only haiku is healthy; sonnet is excluded (availability fail-closed).
        truth = {
            sonnet.id: _report(sonnet, AvailabilityState.UNAVAILABLE),
            haiku.id: _report(haiku, AvailabilityState.READY),
        }
        # The adversarial ranker returns the excluded sonnet FIRST.
        promoting = _make_ranker([sonnet, haiku])
        advisory = AdvisoryInput(
            ranker=promoting, requested_tier=3, escalation=(None, None), label="adversarial"
        )
        record = decide(
            task_spec=_task_review(),
            policy_version="1",
            candidates=catalog,
            availability_truth=truth,
            protected=False,
            advisory=advisory,
        )
        # SC-002 (100% of attempts): the excluded sonnet is NOT admitted.
        admitted_ids = {c.id for c in record.admitted}
        assert sonnet.id not in admitted_ids
        assert haiku.id in admitted_ids
        # Outcome + membership unchanged from the advisory-off baseline.
        base = decide(
            task_spec=_task_review(),
            policy_version="1",
            candidates=catalog,
            availability_truth=truth,
            protected=False,
        )
        assert record.outcome == base.outcome
        assert {c.id for c in record.admitted} == {c.id for c in base.admitted}

    def test_advisory_cannot_change_accepted_to_denied(self, no_network: None) -> None:
        """Advisory influence never flips a hard accepted/denied outcome (FR-005)."""

        catalog = _capable_catalog()
        truth = _healthy_truth(catalog)
        # Ranker that returns an EMPTY ordered list (attempts to drop everything).
        empty_ranker = _make_ranker([])
        advisory = AdvisoryInput(
            ranker=empty_ranker, requested_tier=3, escalation=(None, None), label="drop-all"
        )
        record = decide(
            task_spec=_task_review(),
            policy_version="1",
            candidates=catalog,
            availability_truth=truth,
            protected=False,
            advisory=advisory,
        )
        # Advisory cannot drop membership: both remain admitted; still accepted.
        assert {c.id for c in record.admitted} == {m.id for m in catalog}
        assert record.outcome == OUTCOME_ACCEPTED


# ---------------------------------------------------------------------------
# P3: denied + degraded + unavailable fail-closed.
# ---------------------------------------------------------------------------


class TestP3DeniedDegraded:
    """P3 -- operator sees denied/degraded; unavailable truth under protected closes."""

    def test_denied_no_capable_candidate(self, no_network: None) -> None:
        record = denied_decision()
        assert record.outcome == OUTCOME_DENIED
        assert record.admitted == []
        assert len(record.exclusions) >= 1  # capability exclusions surfaced
        # SC-001: verify reproduces from the denied record's own inputs.
        assert verify_decision(record, **denied_inputs()) is None

    def test_degraded_preferred_down_subset_survives(self, no_network: None) -> None:
        record = degraded_decision()
        assert record.outcome == OUTCOME_DEGRADED
        assert len(record.admitted) >= 1  # surviving subset (haiku)
        # The preferred candidate (sonnet) is in exclusions with a fail-closed state.
        excl_ids = {x["model_id"] for x in record.exclusions}
        assert excl_ids, "degraded must surface the fail-closed exclusion"
        # The surviving subset is the non-excluded candidate the demo admits.
        assert verify_decision(record, **degraded_inputs()) is None

    def test_protected_absent_truth_excludes(self, no_network: None) -> None:
        """Absent availability truth under a protected task excludes (FR-006, NFR-002)."""

        catalog = _capable_catalog()
        record = decide(
            task_spec=_task_review(),
            policy_version="1",
            candidates=catalog,
            availability_truth={},
            protected=True,
        )
        assert record.outcome == OUTCOME_DENIED
        assert record.admitted == []
        assert len(record.exclusions) >= 1

    def test_protected_unavailable_truth_excludes_preferred(self, no_network: None) -> None:
        """An unavailable candidate under a protected task is excluded, not admitted."""

        catalog = _capable_catalog()
        sonnet, haiku = catalog
        truth = {
            sonnet.id: _report(sonnet, AvailabilityState.UNAVAILABLE),
            haiku.id: _report(haiku, AvailabilityState.READY),
        }
        record = decide(
            task_spec=_task_review(),
            policy_version="1",
            candidates=catalog,
            availability_truth=truth,
            protected=True,
        )
        assert sonnet.id not in {c.id for c in record.admitted}
        assert haiku.id in {c.id for c in record.admitted}

    def test_unknown_state_never_reads_as_accepted_under_protected(self, no_network: None) -> None:
        """FR-006: an unknown-state candidate is excluded when protected."""

        catalog = _capable_catalog()
        truth = {m.id: _report(m, AvailabilityState.UNKNOWN) for m in catalog}
        record = decide(
            task_spec=_task_review(),
            policy_version="1",
            candidates=catalog,
            availability_truth=truth,
            protected=True,
        )
        assert record.outcome != OUTCOME_ACCEPTED
        assert record.admitted == []


# ---------------------------------------------------------------------------
# P4: independent verification detects every tamper class from inputs alone.
# ---------------------------------------------------------------------------


class TestP4IndependentVerify:
    """P4 -- auditor verifies from inputs alone (SC-001)."""

    def test_clean_verify_none(self, no_network: None) -> None:
        record = build_demo_decision()
        assert verify_decision(record, **demo_inputs()) is None

    def test_tampered_policy_version_divergent(self, no_network: None) -> None:
        record = build_demo_decision()
        inputs = demo_inputs()
        inputs["policy_version"] = "tampered"
        assert verify_decision(record, **inputs) is VerificationFault.DECISION_ID_DIVERGENT

    def test_tampered_receipt_integrity_tampered(self, no_network: None) -> None:
        record = build_demo_decision()
        # Mutate the receipt's admitted_set so its integrity_digest no longer matches.
        # DecisionReceipt is frozen; mutate via object.__setattr__ on a copied list.
        tampered = [*list(record.receipt.admitted_set), "p/ghost"]
        object.__setattr__(record.receipt, "admitted_set", tampered)
        assert verify_decision(record, **demo_inputs()) is VerificationFault.RECEIPT_TAMPERED

    def test_tampered_receipt_outcome_tampered(self, no_network: None) -> None:
        record = build_demo_decision()
        # Flip the receipt's outcome (in the digest) -> internal inconsistency.
        object.__setattr__(record.receipt, "outcome", "denied:spoofed")
        assert verify_decision(record, **demo_inputs()) is VerificationFault.RECEIPT_TAMPERED

    def test_accepted_on_invalid(self, no_network: None) -> None:
        """ACCEPTED_ON_INVALID: record claims accepted but recompute denies, id match.

        ``outcome`` is NOT part of the decision_id payload and lives on the mutable
        DecisionRecord (not the frozen receipt). Flip ONLY ``record.outcome`` on a
        denied record: the receipt stays self-consistent (RECEIPT_TAMPERED does not
        fire), the id recomputes identically (DECISION_ID_DIVERGENT does not fire),
        but the recomputed outcome is denied while the record claims accepted.
        """
        record = denied_decision()
        assert record.outcome == OUTCOME_DENIED
        # Attacker flips the claimed outcome without touching the receipt or id.
        object.__setattr__(record, "outcome", OUTCOME_ACCEPTED)
        result = verify_decision(record, **denied_inputs())
        assert result is VerificationFault.ACCEPTED_ON_INVALID

    def test_verify_no_credentials_no_network(self, no_network: None) -> None:
        # Re-verifying must not require any producer runtime state.
        record = build_demo_decision()
        # Drop every cached global the authority may hold; verify still passes.
        from verdict import decision_kernel as dk

        dk._BASELINE_RANKER = None  # reset cached ranker (tested in isolation)
        assert verify_decision(record, **demo_inputs()) is None

    def test_verify_str_fault_names(self, no_network: None) -> None:
        # VerificationFault.__str__ returns the member name (quickstart contract).
        assert str(VerificationFault.DECISION_ID_DIVERGENT) == "DECISION_ID_DIVERGENT"
        assert str(VerificationFault.RECEIPT_TAMPERED) == "RECEIPT_TAMPERED"
        assert str(VerificationFault.ACCEPTED_ON_INVALID) == "ACCEPTED_ON_INVALID"


# ---------------------------------------------------------------------------
# Determinism lineage (NFR-001): offline, order-invariant, metadata-eliding.
# ---------------------------------------------------------------------------


class TestDeterminism:
    """NFR-001 -- identical inputs produce identical id/receipt; volatile fields elided."""

    def test_canonical_task_spec_excludes_metadata(self, no_network: None) -> None:
        from verdict.decision_kernel import compute_canonical_task_spec

        base = _task_review()
        noisy = dataclasses.replace(base, metadata={"correlation_id": "volatile-xyz"})
        # Metadata (volatile provenance) must not perturb the canonical task spec.
        assert compute_canonical_task_spec(base) == compute_canonical_task_spec(noisy)

    def test_catalog_digest_binds_identity_snapshot(self, no_network: None) -> None:
        """FR-004: the catalog digest binds the supplied candidate snapshot.

        Identical snapshot -> identical digest (the caller-supplied order is part
        of the bound "candidate catalog snapshot"; advisory *reorder of admitted
        candidates* never perturbs the id because the id binds admitted MEMBERSHIP,
        not order -- that is exercised in P2).
        """
        from verdict.decision_kernel import compute_candidate_catalog_digest

        catalog = _capable_catalog()
        assert compute_candidate_catalog_digest(catalog) == compute_candidate_catalog_digest(
            list(catalog)
        )
        # A namedtuple-equivalent constructed from the same identity must match.
        again = [
            ModelInfo(
                id=m.id,
                provider=m.provider,
                capability_tier=m.capability_tier,
                capabilities=m.capabilities,
            )
            for m in catalog
        ]
        assert compute_candidate_catalog_digest(catalog) == compute_candidate_catalog_digest(again)
