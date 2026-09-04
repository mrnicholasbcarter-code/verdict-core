"""Foundational tests for LaunchGateEvidenceRecord assembly (T004-T006).

Covers overall_status derivation (FR-009 no-silent-pass) and waiver
attribution (FR-010/FR-011), against the real bundled schema — no mocks.
"""

from __future__ import annotations

import pytest

import verdict.release.emergency_approvers as emergency_approvers_module
from verdict.release.emergency_approvers import is_emergency_approver
from verdict.release.evidence import (
    CheckResult,
    DynamicCheckResult,
    Finding,
    LaunchGateEvidenceRecord,
    MemoryBoundaryTestResult,
    ProvenanceAttestation,
    SBOMArtifact,
)
from verdict.release.waivers import Waiver, WaiverValidationError

_PASS = CheckResult(status="pass")


def _clean_record(**overrides: object) -> LaunchGateEvidenceRecord:
    defaults: dict[str, object] = dict(
        release_ref="v1.2.3",
        sbom=(
            SBOMArtifact(
                ecosystem="python",
                format_version="1.5",
                file_path="sbom-python.cdx.json",
                component_count=42,
                generation_status="ok",
            ),
        ),
        provenance=ProvenanceAttestation(
            subject_digest="sha256:" + "0" * 64,
            source_revision="abc123def456",
            build_environment="github-actions",
            predicate_type="https://slsa.dev/provenance/v1",
            attestation_url="https://example.invalid/attestations/1",
        ),
        dynamic_check=DynamicCheckResult(target="http://127.0.0.1:8000", status="pass"),
        dependency_scan=_PASS,
        sast=_PASS,
        memory_boundary_tests=(
            MemoryBoundaryTestResult(
                boundary_module="memory_gate",
                pii_leak_detected=False,
                secret_leak_detected=False,
                status="pass",
            ),
        ),
        retention_erasure_test=_PASS,
        telemetry_consent_test=_PASS,
    )
    defaults.update(overrides)
    return LaunchGateEvidenceRecord(**defaults)  # type: ignore[arg-type]


def test_clean_record_passes_and_validates_against_schema() -> None:
    record = _clean_record()
    assert record.overall_status == "pass"
    record.validate()  # no jsonschema.ValidationError


def test_unavailable_check_is_not_a_silent_pass() -> None:
    record = _clean_record(sast=CheckResult(status="unavailable"))
    assert record.overall_status == "blocked"


def test_degraded_dynamic_check_is_not_a_silent_pass() -> None:
    record = _clean_record(
        dynamic_check=DynamicCheckResult(
            target="http://127.0.0.1:8000", status="target_failed_to_start"
        )
    )
    assert record.overall_status == "blocked"


def test_unwaived_critical_finding_blocks() -> None:
    finding = Finding(id="F-1", severity="critical", title="RCE", source_check="dependency_scan")
    record = _clean_record(dependency_scan=CheckResult(status="pass", findings=(finding,)))
    assert record.overall_status == "blocked"


def test_finding_waiver_excuses_only_its_own_finding_id() -> None:
    matched = Finding(id="F-1", severity="critical", title="RCE", source_check="dependency_scan")
    unmatched = Finding(id="F-2", severity="high", title="Other", source_check="dependency_scan")
    waiver = Waiver(scope="finding", finding_id="F-1", reviewer="alice", reason="false positive")

    excused = _clean_record(
        dependency_scan=CheckResult(status="pass", findings=(matched,)), waivers=(waiver,)
    )
    assert excused.overall_status == "pass"

    still_blocked = _clean_record(
        dependency_scan=CheckResult(status="pass", findings=(matched, unmatched)), waivers=(waiver,)
    )
    assert still_blocked.overall_status == "blocked"


def test_gate_unavailable_waiver_excuses_outage_but_not_a_real_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(emergency_approvers_module, "EMERGENCY_APPROVERS", frozenset({"alice"}))

    outage_waiver = Waiver(
        scope="gate_unavailable",
        reviewer="alice",
        reason="CI infra outage",
        is_emergency_approver=True,
    )

    excused = _clean_record(sast=CheckResult(status="unavailable"), waivers=(outage_waiver,))
    assert excused.overall_status == "pass"

    # A real "failed" result is not an infra outage; the waiver must not excuse it.
    still_blocked = _clean_record(sast=CheckResult(status="failed"), waivers=(outage_waiver,))
    assert still_blocked.overall_status == "blocked"


def test_gate_unavailable_waiver_rejects_non_emergency_reviewer() -> None:
    with pytest.raises(WaiverValidationError):
        Waiver(
            scope="gate_unavailable",
            reviewer="not-an-approver",
            reason="CI infra outage",
            is_emergency_approver=True,
        )


def test_gate_unavailable_waiver_requires_is_emergency_approver_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(emergency_approvers_module, "EMERGENCY_APPROVERS", frozenset({"alice"}))
    with pytest.raises(WaiverValidationError):
        Waiver(scope="gate_unavailable", reviewer="alice", reason="outage")


def test_finding_scope_waiver_requires_finding_id() -> None:
    with pytest.raises(WaiverValidationError):
        Waiver(scope="finding", reviewer="alice", reason="false positive")


def test_finding_scope_waiver_rejects_finding_id_none_explicitly_set() -> None:
    with pytest.raises(WaiverValidationError):
        Waiver(scope="finding", finding_id=None, reviewer="alice", reason="x")


def test_is_emergency_approver_false_for_unknown_handle() -> None:
    assert is_emergency_approver("definitely-not-registered") is False
