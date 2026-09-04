#!/usr/bin/env python3
"""Record one explicit, attributed waiver in a launch-gate evidence JSON file."""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from verdict.release.evidence import (
    CheckResult,
    DynamicCheckResult,
    Finding,
    LaunchGateEvidenceRecord,
    MemoryBoundaryTestResult,
    ProvenanceAttestation,
    SBOMArtifact,
)
from verdict.release.waivers import Waiver

_MAX_INPUT_BYTES = 16 * 1024 * 1024


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _timestamp(value: Any, field: str) -> datetime:
    raw = _string(value, field).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed


def _finding(value: Any, field: str) -> Finding:
    item = _mapping(value, field)
    return Finding(
        id=_string(item.get("id"), f"{field}.id"),
        severity=_string(item.get("severity"), f"{field}.severity"),  # type: ignore[arg-type]
        title=_string(item.get("title"), f"{field}.title"),
        source_check=_string(item.get("source_check"), f"{field}.source_check"),
    )


def _findings(value: Any, field: str) -> tuple[Finding, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    return tuple(_finding(item, f"{field}[{index}]") for index, item in enumerate(value))


def _check(value: Any, field: str) -> CheckResult:
    item = _mapping(value, field)
    evidence_ref = item.get("evidence_ref")
    if evidence_ref is not None:
        evidence_ref = _string(evidence_ref, f"{field}.evidence_ref")
    return CheckResult(
        status=_string(item.get("status"), f"{field}.status"),  # type: ignore[arg-type]
        findings=_findings(item.get("findings"), f"{field}.findings"),
        evidence_ref=evidence_ref,
    )


def _record(value: Any) -> LaunchGateEvidenceRecord:
    item = _mapping(value, "evidence")
    sbom_raw = item.get("sbom")
    if not isinstance(sbom_raw, list):
        raise ValueError("evidence.sbom must be an array")
    sbom = tuple(
        SBOMArtifact(
            ecosystem=_string(raw.get("ecosystem"), f"evidence.sbom[{index}].ecosystem"),  # type: ignore[arg-type]
            format_version=_string(
                raw.get("format_version"), f"evidence.sbom[{index}].format_version"
            ),
            file_path=_string(raw.get("file_path"), f"evidence.sbom[{index}].file_path"),
            component_count=raw.get("component_count"),  # type: ignore[arg-type]
            generation_status=_string(
                raw.get("generation_status"), f"evidence.sbom[{index}].generation_status"
            ),  # type: ignore[arg-type]
        )
        for index, raw_value in enumerate(sbom_raw)
        for raw in (_mapping(raw_value, f"evidence.sbom[{index}]"),)
    )
    provenance_raw = _mapping(item.get("provenance"), "evidence.provenance")
    provenance = ProvenanceAttestation(
        subject_digest=_string(provenance_raw.get("subject_digest"), "provenance.subject_digest"),
        source_revision=_string(
            provenance_raw.get("source_revision"), "provenance.source_revision"
        ),
        build_environment=_string(
            provenance_raw.get("build_environment"), "provenance.build_environment"
        ),
        predicate_type=_string(provenance_raw.get("predicate_type"), "provenance.predicate_type"),
        attestation_url=_string(
            provenance_raw.get("attestation_url"), "provenance.attestation_url"
        ),
    )
    dynamic_raw = _mapping(item.get("dynamic_check"), "evidence.dynamic_check")
    dynamic = DynamicCheckResult(
        target=_string(dynamic_raw.get("target"), "dynamic_check.target"),
        status=_string(dynamic_raw.get("status"), "dynamic_check.status"),  # type: ignore[arg-type]
        findings=_findings(dynamic_raw.get("findings"), "dynamic_check.findings"),
    )
    boundary_raw = item.get("memory_boundary_tests")
    if not isinstance(boundary_raw, list):
        raise ValueError("evidence.memory_boundary_tests must be an array")
    boundaries = tuple(
        MemoryBoundaryTestResult(
            boundary_module=_string(
                raw.get("boundary_module"), f"memory_boundary_tests[{index}].boundary_module"
            ),
            pii_leak_detected=raw.get("pii_leak_detected"),  # type: ignore[arg-type]
            secret_leak_detected=raw.get("secret_leak_detected"),  # type: ignore[arg-type]
            status=_string(raw.get("status"), f"memory_boundary_tests[{index}].status"),  # type: ignore[arg-type]
        )
        for index, raw_value in enumerate(boundary_raw)
        for raw in (_mapping(raw_value, f"evidence.memory_boundary_tests[{index}]"),)
    )
    waivers_raw = item.get("waivers", [])
    if not isinstance(waivers_raw, list):
        raise ValueError("evidence.waivers must be an array")
    waivers = tuple(
        Waiver(
            scope=_string(raw.get("scope"), f"waivers[{index}].scope"),  # type: ignore[arg-type]
            finding_id=raw.get("finding_id"),
            reviewer=_string(raw.get("reviewer"), f"waivers[{index}].reviewer"),
            reason=_string(raw.get("reason"), f"waivers[{index}].reason"),
            recorded_at=_timestamp(raw.get("recorded_at"), f"waivers[{index}].recorded_at"),
            is_emergency_approver=raw.get("is_emergency_approver", False),  # type: ignore[arg-type]
        )
        for index, raw_value in enumerate(waivers_raw)
        for raw in (_mapping(raw_value, f"evidence.waivers[{index}]"),)
    )
    record = LaunchGateEvidenceRecord(
        release_ref=_string(item.get("release_ref"), "evidence.release_ref"),
        sbom=sbom,
        provenance=provenance,
        dynamic_check=dynamic,
        dependency_scan=_check(item.get("dependency_scan"), "evidence.dependency_scan"),
        sast=_check(item.get("sast"), "evidence.sast"),
        memory_boundary_tests=boundaries,
        retention_erasure_test=_check(
            item.get("retention_erasure_test"), "evidence.retention_erasure_test"
        ),
        telemetry_consent_test=_check(
            item.get("telemetry_consent_test"), "evidence.telemetry_consent_test"
        ),
        waivers=waivers,
        generated_at=_timestamp(item.get("generated_at"), "evidence.generated_at"),
    )
    record.validate()
    return record


def record_waiver(
    evidence_path: Path,
    output_path: Path,
    *,
    scope: str,
    reviewer: str,
    reason: str,
    finding_id: str | None,
    is_emergency_approver: bool,
) -> LaunchGateEvidenceRecord:
    if output_path.exists():
        raise ValueError(f"refusing to overwrite existing output: {output_path}")
    if evidence_path.stat().st_size > _MAX_INPUT_BYTES:
        raise ValueError("evidence input exceeds size limit")
    try:
        payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("evidence input is not valid JSON") from exc
    record = _record(payload)
    waiver = Waiver(
        scope=scope,  # type: ignore[arg-type]
        reviewer=reviewer,
        reason=reason,
        finding_id=finding_id,
        is_emergency_approver=is_emergency_approver,
    )
    updated = replace(record, waivers=(*record.waivers, waiver))
    updated.validate()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(updated.to_dict(), indent=2) + "\n", encoding="utf-8")
    return updated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True, help="input evidence JSON")
    parser.add_argument("--output", type=Path, required=True, help="new evidence JSON output")
    parser.add_argument("--scope", choices=("finding", "gate_unavailable"), required=True)
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--finding-id")
    parser.add_argument("--emergency-approver", action="store_true")
    args = parser.parse_args(argv)
    try:
        updated = record_waiver(
            args.evidence,
            args.output,
            scope=args.scope,
            reviewer=args.reviewer,
            reason=args.reason,
            finding_id=args.finding_id,
            is_emergency_approver=args.emergency_approver,
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"recorded {args.scope} waiver; overall_status={updated.overall_status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
