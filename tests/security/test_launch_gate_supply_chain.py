"""Integration tests for the US1 supply-chain evidence fields (T013-T015).

`sbom`, `provenance`, and `dynamic_check` on `LaunchGateEvidenceRecord` are
required (non-defaulted) fields — see `verdict/release/evidence.py`'s
`_unresolved_failures` — so both scenarios below are exercised against the
real gate logic, not a mock: a critical dynamic-check finding must block
(FR-003), and a failed SBOM generation must block and stay recorded as
evidence rather than being dropped from the record (FR-009).
"""

from __future__ import annotations

from verdict.release.evidence import (
    CheckResult,
    DynamicCheckResult,
    Finding,
    LaunchGateEvidenceRecord,
    MemoryBoundaryTestResult,
    ProvenanceAttestation,
    SBOMArtifact,
)

_PASS = CheckResult(status="pass")


def _clean_record(**overrides: object) -> LaunchGateEvidenceRecord:
    defaults: dict[str, object] = dict(
        release_ref="v1.2.3",
        sbom=(
            SBOMArtifact(
                ecosystem="python",
                format_version="1.6",
                file_path="sbom-python.cdx.json",
                component_count=42,
                generation_status="ok",
            ),
            SBOMArtifact(
                ecosystem="node",
                format_version="1.6",
                file_path="sbom-node.cdx.json",
                component_count=17,
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


def test_clean_supply_chain_evidence_passes() -> None:
    assert _clean_record().overall_status == "pass"


def test_critical_dynamic_check_finding_blocks_even_when_status_is_pass() -> None:
    finding = Finding(
        id="ZAP-1", severity="critical", title="SQL Injection", source_check="dynamic_check"
    )
    record = _clean_record(
        dynamic_check=DynamicCheckResult(
            target="http://127.0.0.1:8000", status="pass", findings=(finding,)
        )
    )
    assert record.overall_status == "blocked"
    # FR-009: the finding stays on the record, it is not dropped to force a pass.
    assert finding in record.dynamic_check.findings


def test_failed_sbom_generation_blocks_and_is_recorded_not_skipped() -> None:
    failed_sbom = SBOMArtifact(
        ecosystem="node",
        format_version="1.6",
        file_path="sbom-node.cdx.json",
        component_count=0,
        generation_status="failed",
    )
    record = _clean_record(
        sbom=(
            SBOMArtifact(
                ecosystem="python",
                format_version="1.6",
                file_path="sbom-python.cdx.json",
                component_count=42,
                generation_status="ok",
            ),
            failed_sbom,
        )
    )
    assert record.overall_status == "blocked"
    # The failed artifact is still present in the evidence record, not dropped.
    assert failed_sbom in record.sbom
    assert any(s.generation_status == "failed" for s in record.sbom)


def test_target_failed_to_start_blocks_and_dynamic_check_stays_on_record() -> None:
    dynamic_check = DynamicCheckResult(
        target="http://127.0.0.1:8000", status="target_failed_to_start"
    )
    record = _clean_record(dynamic_check=dynamic_check)
    assert record.overall_status == "blocked"
    assert record.dynamic_check is dynamic_check
