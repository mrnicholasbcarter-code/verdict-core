"""Tests for verdict.orchestration.ready_gate (BOD-157 AC4/AC5)."""

from __future__ import annotations

from verdict.orchestration.ready_gate import (
    DepEvidence,
    ReadyDecision,
    ReadyVerdict,
    ready_decision,
)

DUMMY_SHA = "a" * 40


# ── Rule 1: automation:manual → EXCLUDED ──────────────────────────────────


class TestManualLabelExcluded:
    def test_manual_label_excludes(self) -> None:
        result = ready_decision(
            labels=frozenset({"automation:manual"}), deps=[], main_sha=DUMMY_SHA
        )
        assert result.verdict is ReadyVerdict.EXCLUDED
        assert "automation:manual" in result.reason

    def test_manual_label_excludes_even_with_ready_deps(self) -> None:
        dep = DepEvidence(
            identifier="BOD-100",
            linear_done=True,
            merge_commit_on_main=True,
            verification_record_present=True,
        )
        result = ready_decision(
            labels=frozenset({"automation:manual", "priority:high"}), deps=[dep], main_sha=DUMMY_SHA
        )
        assert result.verdict is ReadyVerdict.EXCLUDED

    def test_other_labels_do_not_exclude(self) -> None:
        result = ready_decision(
            labels=frozenset({"priority:high", "team:core"}), deps=[], main_sha=DUMMY_SHA
        )
        assert result.verdict is ReadyVerdict.READY


# ── Rule 2: no deps → READY ──────────────────────────────────────────────


class TestNoDepsReady:
    def test_no_deps_ready(self) -> None:
        result = ready_decision(labels=frozenset(), deps=[], main_sha=DUMMY_SHA)
        assert result.verdict is ReadyVerdict.READY
        assert "no dependencies" in result.reason

    def test_no_deps_with_labels_ready(self) -> None:
        result = ready_decision(labels=frozenset({"priority:urgent"}), deps=[], main_sha=DUMMY_SHA)
        assert result.verdict is ReadyVerdict.READY


# ── Rule 3: dep MAIN_VERIFIED → READY ────────────────────────────────────


class TestDepMainVerifiedReady:
    def test_single_verified_dep(self) -> None:
        dep = DepEvidence(
            identifier="BOD-100",
            linear_done=True,
            merge_commit_on_main=True,
            verification_record_present=True,
        )
        result = ready_decision(labels=frozenset(), deps=[dep], main_sha=DUMMY_SHA)
        assert result.verdict is ReadyVerdict.READY
        assert "MAIN_VERIFIED" in result.reason

    def test_multiple_verified_deps(self) -> None:
        deps = [
            DepEvidence("BOD-100", True, True, True),
            DepEvidence("BOD-101", True, True, True),
            DepEvidence("BOD-102", True, True, True),
        ]
        result = ready_decision(labels=frozenset(), deps=deps, main_sha=DUMMY_SHA)
        assert result.verdict is ReadyVerdict.READY


# ── Rule 4: dep Done in Linear but not verified on main → WAIT ───────────


class TestDepDoneNotVerifiedWait:
    def test_done_but_not_on_main(self) -> None:
        dep = DepEvidence(
            identifier="BOD-200",
            linear_done=True,
            merge_commit_on_main=False,
            verification_record_present=False,
        )
        result = ready_decision(labels=frozenset(), deps=[dep], main_sha=DUMMY_SHA)
        assert result.verdict is ReadyVerdict.WAIT
        assert "BOD-200" in result.reason
        assert "Done in Linear but not MAIN_VERIFIED" in result.reason

    def test_done_merge_on_main_but_no_verification(self) -> None:
        dep = DepEvidence(
            identifier="BOD-201",
            linear_done=True,
            merge_commit_on_main=True,
            verification_record_present=False,
        )
        result = ready_decision(labels=frozenset(), deps=[dep], main_sha=DUMMY_SHA)
        assert result.verdict is ReadyVerdict.WAIT
        assert "BOD-201" in result.reason

    def test_done_not_on_main_but_has_verification(self) -> None:
        dep = DepEvidence(
            identifier="BOD-202",
            linear_done=True,
            merge_commit_on_main=False,
            verification_record_present=True,
        )
        result = ready_decision(labels=frozenset(), deps=[dep], main_sha=DUMMY_SHA)
        assert result.verdict is ReadyVerdict.WAIT
        assert "BOD-202" in result.reason


# ── Rule 5: missing/unknown dep data → WAIT (fail-closed) ────────────────


class TestMissingEvidenceWait:
    def test_all_none(self) -> None:
        dep = DepEvidence(identifier="BOD-300")
        result = ready_decision(labels=frozenset(), deps=[dep], main_sha=DUMMY_SHA)
        assert result.verdict is ReadyVerdict.WAIT
        assert "unknown/missing evidence" in result.reason

    def test_merge_unknown(self) -> None:
        dep = DepEvidence(
            identifier="BOD-301",
            linear_done=False,
            merge_commit_on_main=None,
            verification_record_present=True,
        )
        result = ready_decision(labels=frozenset(), deps=[dep], main_sha=DUMMY_SHA)
        assert result.verdict is ReadyVerdict.WAIT
        assert "BOD-301" in result.reason

    def test_verification_unknown(self) -> None:
        dep = DepEvidence(
            identifier="BOD-302",
            linear_done=False,
            merge_commit_on_main=True,
            verification_record_present=None,
        )
        result = ready_decision(labels=frozenset(), deps=[dep], main_sha=DUMMY_SHA)
        assert result.verdict is ReadyVerdict.WAIT
        assert "BOD-302" in result.reason


# ── Mixed dependency scenarios ────────────────────────────────────────────


class TestMixedDeps:
    def test_one_verified_one_waiting(self) -> None:
        deps = [
            DepEvidence("BOD-400", True, True, True),  # verified
            DepEvidence("BOD-401", True, False, False),  # done but not verified
        ]
        result = ready_decision(labels=frozenset(), deps=deps, main_sha=DUMMY_SHA)
        assert result.verdict is ReadyVerdict.WAIT
        assert "BOD-401" in result.reason
        assert "BOD-400" not in result.reason

    def test_one_verified_one_unknown(self) -> None:
        deps = [
            DepEvidence("BOD-410", True, True, True),
            DepEvidence("BOD-411"),  # all unknown
        ]
        result = ready_decision(labels=frozenset(), deps=deps, main_sha=DUMMY_SHA)
        assert result.verdict is ReadyVerdict.WAIT
        assert "BOD-411" in result.reason

    def test_multiple_waiting_all_reasons_listed(self) -> None:
        deps = [DepEvidence("BOD-420", True, False, False), DepEvidence("BOD-421")]
        result = ready_decision(labels=frozenset(), deps=deps, main_sha=DUMMY_SHA)
        assert result.verdict is ReadyVerdict.WAIT
        assert "BOD-420" in result.reason
        assert "BOD-421" in result.reason


# ── DepEvidence.main_verified property ────────────────────────────────────


class TestDepEvidenceMainVerified:
    def test_both_true(self) -> None:
        d = DepEvidence("x", True, True, True)
        assert d.main_verified is True

    def test_merge_false(self) -> None:
        d = DepEvidence("x", True, False, True)
        assert d.main_verified is False

    def test_verification_false(self) -> None:
        d = DepEvidence("x", True, True, False)
        assert d.main_verified is False

    def test_both_none(self) -> None:
        d = DepEvidence("x")
        assert d.main_verified is False

    def test_merge_none(self) -> None:
        d = DepEvidence("x", True, None, True)
        assert d.main_verified is False

    def test_verification_none(self) -> None:
        d = DepEvidence("x", True, True, None)
        assert d.main_verified is False


# ── Verdict enum values ──────────────────────────────────────────────────


class TestVerdictEnum:
    def test_string_values(self) -> None:
        assert ReadyVerdict.READY == "READY"
        assert ReadyVerdict.EXCLUDED == "EXCLUDED"
        assert ReadyVerdict.WAIT == "WAIT"

    def test_decision_is_frozen(self) -> None:
        d = ReadyDecision(verdict=ReadyVerdict.READY, reason="ok")
        assert d.verdict is ReadyVerdict.READY
        assert d.reason == "ok"
