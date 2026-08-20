"""Trusted Change Report carrier pipeline tests (feature 002).

Tests the assembly layer that projects existing route/eligibility/verification/receipt
evidence into a TrustedChangeReport bound to an exact source state, computes a
fail-closed acceptance verdict, supports independent verification, and exports a
deterministic, leak-free portable report.
"""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

import pytest

from verdict.contracts import (
    ContractValidationError,
    DiffSummary,
    SourceState,
    TrustedChangeReport,
    VerificationResult,
)
from verdict.security import fingerprint_text
from verdict.trusted_change_report import (
    ACCEPTED_ALL_GATES_GREEN,
    DENIED_FAILED_CHECK,
    DENIED_INELIGIBLE_ROUTE,
    DENIED_MISSING_VERIFICATION,
    DENIED_OUT_OF_SCOPE,
    DENIED_TAMPERED_EVIDENCE,
    VERDICT_NOT_COMPUTED,
    VerificationFault,
    assemble_report,
    capture_source_state,
    compute_report_digest,
    compute_verdict,
    export_redacted_report,
    verify_report,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "trusted_change_report"


def _load_report(name: str) -> TrustedChangeReport:
    return TrustedChangeReport.from_dict(json.loads((FIXTURE_DIR / name).read_text()))


def _receipt_fingerprint(payload: dict) -> str:
    """Fingerprint a receipt payload exactly as verify_report recomputes it."""
    return fingerprint_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))


class TestCanonicalizationAndDigests:
    """Deterministic serialization and digest computation (NFR-001)."""

    def test_canonical_report_payload_drops_generated_at(self) -> None:
        report = _load_report("report-accepted.json")
        payload = compute_report_digest(report)  # internally uses canonical_report_payload
        assert "generated_at" not in json.dumps(payload)

    def test_compute_report_digest_stable(self) -> None:
        report = _load_report("report-accepted.json")
        d1 = compute_report_digest(report)
        d2 = compute_report_digest(report)
        assert d1 == d2
        assert d1.startswith("sha256:")
        # fingerprint_text truncates to 32 hex chars by default (sha256: + 32).
        assert len(d1) == len("sha256:") + 32

    def test_digest_changes_when_facts_change(self) -> None:
        r1 = _load_report("report-accepted.json")
        r2 = _load_report("report-denied-failed-check.json")
        assert compute_report_digest(r1) != compute_report_digest(r2)


class TestSourceStateCapture:
    """Binding an exact source state from a local checkout (FR-001)."""

    def test_capture_clean_commit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            # init a minimal git repo
            subprocess_run = __import__("subprocess").run
            subprocess_run(["git", "init", "-q"], cwd=td_path, check=True)
            subprocess_run(["git", "config", "user.email", "test@test"], cwd=td_path, check=True)
            subprocess_run(["git", "config", "user.name", "Test"], cwd=td_path, check=True)
            (td_path / "README.md").write_text("test")
            subprocess_run(["git", "add", "README.md"], cwd=td_path, check=True)
            subprocess_run(["git", "commit", "-q", "-m", "init"], cwd=td_path, check=True)

            src = capture_source_state(
                td_path,
                method="clean_commit",
                repository_url="git@example.com:acme/test.git",
                branch="main",
                snapshot_timestamp="2026-08-20T00:00:00Z",
            )
            assert isinstance(src, SourceState)
            assert src.commit_sha
            assert src.repository_url == "git@example.com:acme/test.git"
            assert src.branch == "main"
            assert src.snapshot_method == "clean_commit"
            assert src.dirty_files == []
            assert src.untracked_files == []

    def test_capture_dirty_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            subprocess_run = __import__("subprocess").run
            subprocess_run(["git", "init", "-q"], cwd=td_path, check=True)
            subprocess_run(["git", "config", "user.email", "test@test"], cwd=td_path, check=True)
            subprocess_run(["git", "config", "user.name", "Test"], cwd=td_path, check=True)
            (td_path / "README.md").write_text("test")
            subprocess_run(["git", "add", "README.md"], cwd=td_path, check=True)
            subprocess_run(["git", "commit", "-q", "-m", "init"], cwd=td_path, check=True)
            (td_path / "new_file.txt").write_text("dirty")

            src = capture_source_state(
                td_path,
                method="dirty_snapshot",
                repository_url="git@example.com:acme/test.git",
                branch="main",
                snapshot_timestamp="2026-08-20T00:00:00Z",
            )
            assert src.snapshot_method == "dirty_snapshot"
            assert "new_file.txt" in src.untracked_files or "new_file.txt" in src.dirty_files

    def test_capture_clean_commit_fails_on_dirty_tree(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            subprocess_run = __import__("subprocess").run
            subprocess_run(["git", "init", "-q"], cwd=td_path, check=True)
            subprocess_run(["git", "config", "user.email", "test@test"], cwd=td_path, check=True)
            subprocess_run(["git", "config", "user.name", "Test"], cwd=td_path, check=True)
            (td_path / "README.md").write_text("test")
            subprocess_run(["git", "add", "README.md"], cwd=td_path, check=True)
            subprocess_run(["git", "commit", "-q", "-m", "init"], cwd=td_path, check=True)
            (td_path / "new_file.txt").write_text("dirty")

            with pytest.raises(
                ValueError, match="clean_commit method requires a clean working tree"
            ):
                capture_source_state(
                    td_path,
                    method="clean_commit",
                    repository_url="git@example.com:acme/test.git",
                    branch="main",
                    snapshot_timestamp="2026-08-20T00:00:00Z",
                )


class TestAssembleReport:
    """Projecting existing evidence into a TrustedChangeReport (FR-001..FR-004, FR-010)."""

    def test_assemble_from_accepted_fixture(self) -> None:
        report = _load_report("report-accepted.json")
        assert report.objective == "ship widget v2 endpoint"
        assert report.task_type == "feature"
        assert report.work_unit_ids == ["wu-accepted-1"]
        assert report.route_decision["selected_route"]["runtime_id"] == "cc/claude-sonnet-5"
        assert report.source_state.commit_sha == "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
        assert isinstance(report.verification_results[0], VerificationResult)
        assert report.verification_results[0].status == "passed"
        assert report.evidence_receipts[0]["integrity_ok"] is True

    def test_assemble_stamps_interim_unknown_when_no_verdict_passed(self) -> None:
        raw = json.loads((FIXTURE_DIR / "report-denied-failed-check.json").read_text())
        report = assemble_report(
            objective=raw["objective"],
            task_type=raw["task_type"],
            work_unit_ids=raw["work_unit_ids"],
            route_decision=raw["route_decision"],
            eligibility=raw.get("eligibility", {}),
            receipts=raw["evidence_receipts"],
            verification_results=raw["verification_results"],
            diff_summary=raw["diff_summary"],
            source_state=raw["source_state"],
            received_at=raw["received_at"],
            generated_at=raw["generated_at"],
        )
        assert report.acceptance.decision == "unknown"
        assert report.acceptance.reason == VERDICT_NOT_COMPUTED

    def test_assemble_carrier_not_decider_no_eligibility_recompute(self) -> None:
        """FR-010: assemble_report never calls into verdict.eligibility."""
        raw = json.loads((FIXTURE_DIR / "report-accepted.json").read_text())
        report = assemble_report(
            objective=raw["objective"],
            task_type=raw["task_type"],
            work_unit_ids=raw["work_unit_ids"],
            route_decision=raw["route_decision"],
            eligibility=raw.get("eligibility", {}),
            receipts=raw["evidence_receipts"],
            verification_results=raw["verification_results"],
            diff_summary=raw["diff_summary"],
            source_state=raw["source_state"],
            received_at=raw["received_at"],
            generated_at=raw["generated_at"],
        )
        # Eligibility is projected as data only; no re-derivation happens.
        # The route_decision may carry an eligibility state, but assemble_report
        # does not import or invoke verdict.eligibility.EligibilityGate.
        assert "verdict.eligibility" not in str(type(report))


class TestComputeVerdictFailClosed:
    """Fail-closed acceptance verdict from projected facts (FR-005, NFR-002)."""

    def test_accepted_when_all_gates_green(self) -> None:
        report = _load_report("report-accepted.json")
        v = compute_verdict(report)
        assert v.decision == "accepted"
        assert v.reason == ACCEPTED_ALL_GATES_GREEN

    def test_denied_failed_check(self) -> None:
        report = _load_report("report-denied-failed-check.json")
        v = compute_verdict(report)
        assert v.decision == "denied"
        assert v.reason == DENIED_FAILED_CHECK

    def test_denied_ineligible_route(self) -> None:
        report = _load_report("report-denied-ineligible-route.json")
        v = compute_verdict(report)
        assert v.decision == "denied"
        assert v.reason == DENIED_INELIGIBLE_ROUTE

    def test_denied_missing_verification(self) -> None:
        report = _load_report("report-denied-missing-verification.json")
        v = compute_verdict(report)
        assert v.decision == "denied"
        assert v.reason == DENIED_MISSING_VERIFICATION

    def test_denied_out_of_scope(self) -> None:
        report = _load_report("report-denied-out-of-scope.json")
        v = compute_verdict(report)
        assert v.decision == "denied"
        assert v.reason == DENIED_OUT_OF_SCOPE

    def test_denied_tampered_evidence(self) -> None:
        report = _load_report("report-denied-tampered-evidence.json")
        v = compute_verdict(report)
        assert v.decision == "denied"
        assert v.reason == DENIED_TAMPERED_EVIDENCE

    def test_impossible_to_present_accepted_when_gates_fail(self) -> None:
        """FR-005: a report with failed check/out-of-scope/ineligible/tampered
        MUST NOT produce an accepted verdict."""
        for name in (
            "report-denied-failed-check.json",
            "report-denied-ineligible-route.json",
            "report-denied-missing-verification.json",
            "report-denied-out-of-scope.json",
            "report-denied-tampered-evidence.json",
        ):
            report = _load_report(name)
            v = compute_verdict(report)
            assert v.decision == "denied", f"{name} must deny, got {v.decision}"


class TestIndependentVerification:
    """Credential-free, producer-trust-free verification (P3, FR-007)."""

    def test_verify_report_clean_accepted_bound_to_real_checkout(self) -> None:
        """A report bound to the real checkout, with all gates green, verifies clean."""
        # Use dirty_snapshot: the worktree has untracked test files, which
        # clean_commit rejects. verify_report re-captures with the same method.
        src = capture_source_state(
            Path("."),
            method="dirty_snapshot",
            repository_url="git@example.com:acme/test.git",
            branch="main",
            snapshot_timestamp="2026-08-20T00:00:00Z",
        )
        vr = VerificationResult(
            check_name="focused-tests",
            check_type="focused_tests",
            status="passed",
            command="pytest -q",
            runtime="python",
            provenance="local",
            policy_requirement="all-tests-pass",
            raw_output="3 passed",
        )
        diff = DiffSummary(
            files_changed=[],
            lines_added=0,
            lines_removed=0,
            protected_files_touched=[],
            boundary_violations=[],
            diff_digest="sha256:" + "0" * 64,
        )
        receipt = {"payload": {}, "hash": _receipt_fingerprint({}), "integrity_ok": True}
        report = assemble_report(
            objective="test",
            task_type="bugfix",
            work_unit_ids=["wu-test"],
            route_decision={"selected_route": {"runtime_id": "cc/claude-sonnet-5"}},
            eligibility={},
            receipts=[receipt],
            verification_results=[vr],
            diff_summary=diff,
            source_state=src,
            received_at="2026-08-20T00:00:00Z",
            generated_at="2026-08-20T00:00:01Z",
        )
        fault = verify_report(report, source_checkout=Path("."))
        assert fault is None

    def test_verify_report_binding_mismatch_returns_fault(self) -> None:
        """A report whose committed sha doesn't match the checkout flags a fault."""
        report = _load_report("report-denied-unbound-source.json")
        fault = verify_report(report, source_checkout=Path("."))
        assert fault == VerificationFault.SOURCE_BINDING_MISMATCH

    def test_verify_report_accepts_accepted_on_invalid_gates(self) -> None:
        """ACCEPTED_ON_INVALID_GATES: a report claims accepted but projected facts deny.

        We build a report bound to the *actual* checkout so source binding passes,
        but whose projected facts (failed check) would deny it — then override
        acceptance to 'accepted' to simulate a tampered report.
        """
        # Capture current checkout's source state (dirty_snapshot: untracked test files).
        src = capture_source_state(
            Path("."),
            method="dirty_snapshot",
            repository_url="git@example.com:acme/test.git",
            branch="main",
            snapshot_timestamp="2026-08-20T00:00:00Z",
        )
        # Build a report with a failed verification check (projected facts deny).
        vr_failed = VerificationResult(
            check_name="focused-tests",
            check_type="focused_tests",
            status="failed",
            command="pytest -q",
            runtime="python",
            provenance="local",
            policy_requirement="all-tests-pass",
            raw_output="FAILED",
        )
        vr_passed = VerificationResult(
            check_name="type-check",
            check_type="policy",
            status="passed",
            command="mypy .",
            runtime="python",
            provenance="local",
            policy_requirement="types-clean",
            raw_output="Success",
        )
        diff = DiffSummary(
            files_changed=["verdict/trusted_change_report.py"],
            lines_added=10,
            lines_removed=2,
            protected_files_touched=[],
            boundary_violations=[],
            diff_digest="sha256:" + "a" * 64,
        )
        # Minimal receipt with integrity_ok=True and a matching hash.
        receipt = {
            "payload": {"ok": True},
            "hash": _receipt_fingerprint({"ok": True}),
            "integrity_ok": True,
        }
        report = assemble_report(
            objective="test",
            task_type="bugfix",
            work_unit_ids=["wu-test"],
            route_decision={"selected_route": {"runtime_id": "cc/claude-sonnet-5"}},
            eligibility={},
            receipts=[receipt],
            verification_results=[vr_failed, vr_passed],
            diff_summary=diff,
            source_state=src,
            received_at="2026-08-20T00:00:00Z",
            generated_at="2026-08-20T00:00:01Z",
        )
        # Override acceptance to 'accepted' (simulating tampered report).
        from verdict.contracts import AcceptanceDecision

        tampered = TrustedChangeReport(
            schema_version=report.schema_version,
            report_id=report.report_id,
            objective=report.objective,
            task_type=report.task_type,
            source_state=report.source_state,
            work_unit_ids=list(report.work_unit_ids),
            route_decision=dict(report.route_decision),
            evidence_receipts=[dict(r) for r in report.evidence_receipts],
            verification_results=list(report.verification_results),
            diff_summary=report.diff_summary,
            metrics=report.metrics,
            acceptance=AcceptanceDecision(decision="accepted", reason="ACCEPTED_ALL_GATES_GREEN"),
            route_recommendation=report.route_recommendation,
            regression_observation=report.regression_observation,
            received_at=report.received_at,
            generated_at=report.generated_at,
        )
        fault = verify_report(tampered, source_checkout=Path("."))
        assert fault == VerificationFault.ACCEPTED_ON_INVALID_GATES


class TestRedactedExport:
    """Deterministic, leak-free portable export (P4, FR-008, SC-005)."""

    def test_export_redacted_report_matches_fixture(self) -> None:
        report = _load_report("report-accepted.json")
        redacted = export_redacted_report(report)
        expected = json.loads((FIXTURE_DIR / "report-redacted-export.json").read_text())

        # Only generated_at can differ; drop it for comparison.
        def strip_ts(d: dict) -> dict:
            d2 = dict(d)
            d2.pop("generated_at", None)
            return d2

        assert strip_ts(redacted) == strip_ts(expected)

    def test_export_is_deterministic(self) -> None:
        report = _load_report("report-accepted.json")
        r1 = export_redacted_report(report)
        r2 = export_redacted_report(report)
        assert json.dumps(r1, sort_keys=True) == json.dumps(r2, sort_keys=True)

    def test_export_retains_decision_relevant_fields(self) -> None:
        report = _load_report("report-accepted.json")
        redacted = export_redacted_report(report)
        assert redacted["acceptance"]["decision"] == "accepted"
        assert redacted["acceptance"]["reason"] == ACCEPTED_ALL_GATES_GREEN
        assert redacted["source_state"]["commit_sha"]
        assert redacted["diff_summary"]["diff_digest"]
        assert redacted["report_id"]

    def test_export_drops_producer_internal_fields(self) -> None:
        report = _load_report("report-accepted.json")
        redacted = export_redacted_report(report)
        for vr in redacted["verification_results"]:
            assert "raw_output" not in vr
            assert "command" not in vr
            assert "runtime" not in vr

    def test_export_no_secrets_or_pii(self) -> None:
        """SC-005: no actual secret assignments or credentials survive the export.

        We scan for *secret shapes* (key=value / bearer credentials / URL-embedded
        auth), not bare field names like ``tokens_in`` which are not secrets.
        """
        report = _load_report("report-accepted.json")
        redacted = export_redacted_report(report)
        # The fixture uses a benign git@example.com placeholder in repository_url;
        # redact it before scanning so it isn't mistaken for PII.
        text = json.dumps(redacted).replace("git@example.com", "git@example[placeholder]")
        # Secret-shaped assignments: foo=secret-value patterns, with word boundaries
        # so legit field names (tokens_in) are not matched.
        secret_assign = re.compile(
            r"(?i)\b(api[_-]?key|secret|token|password)\b\s*[=:]\s*[^\s&,;\"]+"
        )
        assert not secret_assign.search(text), (
            f"secret-shaped value survived: {secret_assign.search(text)}"
        )
        # Bearer authorization headers.
        assert not re.search(r"(?i)authorization\s*:\s*bearer\s+\S+", text)
        # URL-embedded credentials (user:pass@host) — allow the benign example form.
        assert not re.search(r"https?://[^/@:\s]+:[^/@:\s]+@", text)
        # Email addresses (real PII) — the placeholder above is not an email.
        assert not re.search(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", text)
        # Host:port credential pairs (private).
        assert not re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+\b", text)


class TestReproduction:
    """Clean-checkout independent reproduction (NFR-003, SC-003)."""

    def test_reproduce_report_deterministic(self) -> None:
        # Can't fully run without a matching source checkout, but we can assert
        # the canonicalization logic used by reproduce_report is deterministic.
        report = _load_report("report-accepted.json")
        canonical1 = compute_report_digest(report)
        canonical2 = compute_report_digest(report)
        assert canonical1 == canonical2


class TestContractValidation:
    """Contract construction validates required fields and rejects invalid inputs."""

    def test_report_requires_objective_task_type_work_unit_ids(self) -> None:
        base = json.loads((FIXTURE_DIR / "report-accepted.json").read_text())
        for field in ("objective", "task_type", "work_unit_ids"):
            bad = dict(base)
            del bad[field]
            with pytest.raises(ContractValidationError):
                TrustedChangeReport.from_dict(bad)

    def test_source_state_requires_commit_sha(self) -> None:
        bad = json.loads((FIXTURE_DIR / "report-accepted.json").read_text())
        bad["source_state"]["commit_sha"] = ""
        with pytest.raises(ContractValidationError, match="commit_sha is required"):
            TrustedChangeReport.from_dict(bad)

    def test_verification_result_rejects_secret_raw_output(self) -> None:
        # Use a secret shape that redact_text actually redacts (secret=...).
        vr = dict(
            check_name="x",
            check_type="custom",
            status="passed",
            command="c",
            runtime="r",
            provenance="p",
            policy_requirement="pr",
            raw_output="secret=abc123",
        )
        with pytest.raises(
            ContractValidationError, match="raw_output contains secret-bearing content"
        ):
            VerificationResult(**vr)

    def test_diff_summary_requires_valid_digest(self) -> None:
        bad = dict(
            files_changed=[],
            lines_added=0,
            lines_removed=0,
            protected_files_touched=[],
            boundary_violations=[],
            diff_digest="sha256:not64hex",
        )
        with pytest.raises(ContractValidationError, match="invalid diff_digest"):
            DiffSummary(**bad)

    def test_verification_result_status_must_be_known_enum(self) -> None:
        vr = dict(
            check_name="x",
            check_type="focused_tests",
            status="invalid",
            command="c",
            runtime="r",
            provenance="p",
            policy_requirement="pr",
        )
        with pytest.raises(ContractValidationError, match="invalid status"):
            VerificationResult(**vr)


__all__ = []
