"""LaunchGateEvidenceRecord assembly and schema validation.

Implements FR-008 (every artifact reproducible from a clean checkout) and
FR-009 (an unavailable/degraded check must never be silently treated as a
pass). `overall_status` is derived, never set directly, so a caller cannot
report "pass" while a real failure is unaccounted for.

`Finding.waived` is intentionally NOT a constructor field: whether a finding
is waived is derived solely from `LaunchGateEvidenceRecord.waivers` at
serialization/evaluation time. If it were caller-settable, a critical
finding could be marked waived with no recorded `Waiver`, silently
defeating FR-010 (every bypass must be an explicit, attributed, recorded
waiver).

The bundled schema copy at `verdict/release/schemas/launch_gate_evidence.schema.json`
is the runtime source of truth (the package must work from a built artifact
where `specs/` is not present, per `.dockerignore`). The design copy at
`specs/277-security-privacy-gate/contracts/launch-gate-evidence.schema.json`
is checked for parity against it in `tests/security/test_evidence_schema_parity.py`.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, cast

import jsonschema

from verdict.release.waivers import Waiver

CheckStatus = Literal["pass", "failed", "unavailable", "degraded"]
Severity = Literal["critical", "high", "medium", "low", "info"]

_SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "launch_gate_evidence.schema.json"
_DEFAULT_MEMORY_BOUNDARY_MODULES: tuple[str, ...] = (
    "verdict/memory_gate.py",
    "verdict/memory_plane.py",
    "verdict/memory_bridge.py",
    "verdict/memory_document_adapter.py",
    "verdict/memory_graph_adapter.py",
    "verdict/memory_masterdocs_adapter.py",
    "verdict/memory_session_adapter.py",
)
_MAX_JUNIT_BYTES = 16 * 1024 * 1024
_UNWAIVABLE_SEVERITIES: frozenset[Severity] = frozenset({"critical", "high"})
_OUTAGE_STATUSES: frozenset[str] = frozenset({"unavailable", "degraded"})


def load_schema() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(_SCHEMA_PATH.read_text()))


@dataclass(frozen=True, slots=True)
class Finding:
    id: str
    severity: Severity
    title: str
    source_check: str

    def to_dict(self, *, waived: bool) -> dict[str, Any]:
        return {
            "id": self.id,
            "severity": self.severity,
            "title": self.title,
            "source_check": self.source_check,
            "waived": waived,
        }


@dataclass(frozen=True, slots=True)
class CheckResult:
    status: CheckStatus
    findings: tuple[Finding, ...] = ()
    evidence_ref: str | None = None

    def to_dict(self, *, waived_finding_ids: frozenset[str]) -> dict[str, Any]:
        out: dict[str, Any] = {
            "status": self.status,
            "findings": [f.to_dict(waived=f.id in waived_finding_ids) for f in self.findings],
        }
        if self.evidence_ref is not None:
            out["evidence_ref"] = self.evidence_ref
        return out


@dataclass(frozen=True, slots=True)
class SBOMArtifact:
    ecosystem: Literal["python", "node"]
    format_version: str
    file_path: str
    component_count: int
    generation_status: Literal["ok", "failed"]
    format: Literal["CycloneDX"] = "CycloneDX"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ecosystem": self.ecosystem,
            "format": self.format,
            "format_version": self.format_version,
            "file_path": self.file_path,
            "component_count": self.component_count,
            "generation_status": self.generation_status,
        }


@dataclass(frozen=True, slots=True)
class ProvenanceAttestation:
    subject_digest: str
    source_revision: str
    build_environment: str
    predicate_type: str
    attestation_url: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject_digest": self.subject_digest,
            "source_revision": self.source_revision,
            "build_environment": self.build_environment,
            "predicate_type": self.predicate_type,
            "attestation_url": self.attestation_url,
        }


@dataclass(frozen=True, slots=True)
class DynamicCheckResult:
    target: str
    status: Literal["pass", "blocked", "target_failed_to_start"]
    findings: tuple[Finding, ...] = ()
    scan_type: Literal["zap-baseline"] = "zap-baseline"

    def to_dict(self, *, waived_finding_ids: frozenset[str]) -> dict[str, Any]:
        return {
            "target": self.target,
            "scan_type": self.scan_type,
            "findings": [f.to_dict(waived=f.id in waived_finding_ids) for f in self.findings],
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class MemoryBoundaryTestResult:
    boundary_module: str
    pii_leak_detected: bool
    secret_leak_detected: bool
    status: Literal["pass", "fail"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "boundary_module": self.boundary_module,
            "pii_leak_detected": self.pii_leak_detected,
            "secret_leak_detected": self.secret_leak_detected,
            "status": self.status,
        }


def memory_boundary_results_from_junit_xml(
    report_path: str | Path, *, boundary_modules: tuple[str, ...] = _DEFAULT_MEMORY_BOUNDARY_MODULES
) -> tuple[MemoryBoundaryTestResult, ...]:
    """Convert a bounded pytest JUnit report into fail-closed boundary results.

    A missing, oversized, malformed, or incomplete report produces ``fail`` for
    every requested module. Pytest's ``xfail`` outcomes are represented as
    ``skipped`` elements, so they also fail closed rather than becoming a
    silent pass. Only the stable test class/module names are retained; failure
    text is never copied into the release evidence record.
    """
    modules = tuple(dict.fromkeys(boundary_modules))
    if not modules:
        return ()

    def failed_results() -> tuple[MemoryBoundaryTestResult, ...]:
        return tuple(
            MemoryBoundaryTestResult(
                boundary_module=module,
                pii_leak_detected=False,
                secret_leak_detected=False,
                status="fail",
            )
            for module in modules
        )

    path = Path(report_path)
    try:
        if path.stat().st_size > _MAX_JUNIT_BYTES:
            return failed_results()
        payload = path.read_bytes()
    except OSError:
        return failed_results()
    lowered_payload = payload.lower()
    if (
        len(payload) > _MAX_JUNIT_BYTES
        or b"<!doctype" in lowered_payload
        or b"<!entity" in lowered_payload
    ):
        return failed_results()
    try:
        root = ET.fromstring(payload)
    except (ET.ParseError, UnicodeDecodeError):
        return failed_results()

    cases_by_module: dict[str, list[ET.Element]] = {module: [] for module in modules}
    for case in root.iter("testcase"):
        classname = case.attrib.get("classname", "")
        for module in modules:
            module_name = Path(module).stem
            test_module_names = (module_name, f"test_{module_name}", f"test_{module_name}_boundary")
            if any(
                classname == test_name or classname.endswith(f".{test_name}")
                for test_name in test_module_names
            ):
                cases_by_module[module].append(case)
                break

    results: list[MemoryBoundaryTestResult] = []
    for module in modules:
        cases = cases_by_module[module]
        pii_leak = False
        secret_leak = False
        failed = not cases
        for case in cases:
            outcome = next(
                (child for child in case if child.tag in {"failure", "error", "skipped"}), None
            )
            if outcome is None:
                continue
            failed = True
            details = " ".join(
                value
                for value in (
                    case.attrib.get("name", ""),
                    outcome.attrib.get("type", ""),
                    outcome.attrib.get("message", ""),
                )
                if value
            ).lower()
            if "pii" in details:
                pii_leak = True
            if any(term in details for term in ("secret", "bearer", "basic", "auth", "token")):
                secret_leak = True
            if not any(
                term in details for term in ("pii", "secret", "bearer", "basic", "auth", "token")
            ):
                pii_leak = True
                secret_leak = True
        results.append(
            MemoryBoundaryTestResult(
                boundary_module=module,
                pii_leak_detected=pii_leak,
                secret_leak_detected=secret_leak,
                status="fail" if failed else "pass",
            )
        )
    return tuple(results)


def check_result_from_junit_xml(report_path: str | Path) -> CheckResult:
    """Convert a bounded JUnit report to a fail-closed aggregate check result."""
    path = Path(report_path)
    try:
        if path.stat().st_size > _MAX_JUNIT_BYTES:
            return CheckResult(status="failed", evidence_ref=str(path))
        payload = path.read_bytes()
    except OSError:
        return CheckResult(status="unavailable", evidence_ref=str(path))
    lowered_payload = payload.lower()
    if (
        len(payload) > _MAX_JUNIT_BYTES
        or b"<!doctype" in lowered_payload
        or b"<!entity" in lowered_payload
    ):
        return CheckResult(status="failed", evidence_ref=str(path))
    try:
        root = ET.fromstring(payload)
    except (ET.ParseError, UnicodeDecodeError):
        return CheckResult(status="failed", evidence_ref=str(path))

    cases = list(root.iter("testcase"))
    if not cases:
        return CheckResult(status="failed", evidence_ref=str(path))
    has_failure = any(
        any(child.tag in {"failure", "error", "skipped"} for child in case) for case in cases
    )
    return CheckResult(status="failed" if has_failure else "pass", evidence_ref=str(path))


@dataclass(frozen=True, slots=True)
class LaunchGateEvidenceRecord:
    release_ref: str
    sbom: tuple[SBOMArtifact, ...]
    provenance: ProvenanceAttestation
    dynamic_check: DynamicCheckResult
    dependency_scan: CheckResult
    sast: CheckResult
    memory_boundary_tests: tuple[MemoryBoundaryTestResult, ...]
    retention_erasure_test: CheckResult
    telemetry_consent_test: CheckResult
    waivers: tuple[Waiver, ...] = ()
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def overall_status(self) -> Literal["pass", "blocked"]:
        return "pass" if not self._unresolved_failures() else "blocked"

    def _waived_finding_ids(self) -> frozenset[str]:
        return frozenset(
            w.finding_id for w in self.waivers if w.scope == "finding" and w.finding_id is not None
        )

    def _has_outage_waiver(self) -> bool:
        return any(w.scope == "gate_unavailable" for w in self.waivers)

    def _unresolved_failures(self) -> list[str]:
        """Reasons overall_status cannot be "pass". Empty means clean.

        FR-009: unavailable/degraded is never a silent pass. FR-010: a
        `finding`-scope waiver excuses only its own finding_id. FR-011: a
        `gate_unavailable`-scope waiver excuses unavailable/degraded
        CheckResults (a full gate outage), never a `failed` result.
        """
        waived_finding_ids = self._waived_finding_ids()
        has_outage_waiver = self._has_outage_waiver()

        reasons: list[str] = []

        for sbom_artifact in self.sbom:
            if sbom_artifact.generation_status != "ok":
                reasons.append(f"sbom[{sbom_artifact.ecosystem}].generation_status=failed")

        named_checks: list[tuple[str, CheckResult]] = [
            ("dependency_scan", self.dependency_scan),
            ("sast", self.sast),
            ("retention_erasure_test", self.retention_erasure_test),
            ("telemetry_consent_test", self.telemetry_consent_test),
        ]
        for name, check in named_checks:
            outage_excused = check.status in _OUTAGE_STATUSES and has_outage_waiver
            if check.status != "pass" and not outage_excused:
                reasons.append(f"{name}.status={check.status}")
            for finding in check.findings:
                if (
                    finding.severity in _UNWAIVABLE_SEVERITIES
                    and finding.id not in waived_finding_ids
                ):
                    reasons.append(f"{name}.finding[{finding.id}] unwaived {finding.severity}")

        dynamic_outage_excused = self.dynamic_check.status in _OUTAGE_STATUSES and has_outage_waiver
        if self.dynamic_check.status != "pass" and not dynamic_outage_excused:
            reasons.append(f"dynamic_check.status={self.dynamic_check.status}")
        for finding in self.dynamic_check.findings:
            if finding.severity in _UNWAIVABLE_SEVERITIES and finding.id not in waived_finding_ids:
                reasons.append(f"dynamic_check.finding[{finding.id}] unwaived {finding.severity}")

        for result in self.memory_boundary_tests:
            if result.status != "pass":
                reasons.append(f"memory_boundary_tests[{result.boundary_module}].status=fail")

        return reasons

    def to_dict(self) -> dict[str, Any]:
        waived_finding_ids = self._waived_finding_ids()
        return {
            "release_ref": self.release_ref,
            "generated_at": self.generated_at.isoformat(),
            "sbom": [s.to_dict() for s in self.sbom],
            "provenance": self.provenance.to_dict(),
            "dynamic_check": self.dynamic_check.to_dict(waived_finding_ids=waived_finding_ids),
            "dependency_scan": self.dependency_scan.to_dict(waived_finding_ids=waived_finding_ids),
            "sast": self.sast.to_dict(waived_finding_ids=waived_finding_ids),
            "memory_boundary_tests": [m.to_dict() for m in self.memory_boundary_tests],
            "retention_erasure_test": self.retention_erasure_test.to_dict(
                waived_finding_ids=waived_finding_ids
            ),
            "telemetry_consent_test": self.telemetry_consent_test.to_dict(
                waived_finding_ids=waived_finding_ids
            ),
            "overall_status": self.overall_status,
            "waivers": [w.to_dict() for w in self.waivers],
        }

    def validate(self) -> None:
        """Raise jsonschema.ValidationError if this record violates the contract."""
        jsonschema.validate(instance=self.to_dict(), schema=load_schema())
