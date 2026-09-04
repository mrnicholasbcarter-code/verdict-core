"""Normalize third-party security-scanner output into `Finding` objects.

Implements the remaining scope of T004: turning real pip-audit, bandit, and
npm audit JSON into `verdict.release.evidence.Finding` entries so they can be
assembled into a `LaunchGateEvidenceRecord`.

Field shapes below are taken from each tool's own source, not guessed:
- pip-audit: `pip_audit/_format/json.py` (`JsonFormat.format`) walks
  `AuditResult.[dependency].vulns[]`, each a `VulnerabilityResult` with `id`,
  `fix_versions`, `aliases`, `description` — there is no severity field
  anywhere in pip-audit's own data model. Every pip-audit finding is
  therefore mapped to "high": pip-audit only reports vulnerabilities that a
  registry (OSV/PyPI) already confirmed exist, and under-classifying a real,
  confirmed vulnerability as low-severity would risk a silent pass (FR-009).
- bandit: `bandit/formatters/json.py` emits `results[]`, each with `test_id`,
  `test_name`, `issue_severity` (UNDEFINED/LOW/MEDIUM/HIGH), `issue_text`,
  `filename`, `line_number`.
- npm audit: `@npmcli/arborist/lib/vuln.js` (`Vulnerability.toJSON`) emits
  `vulnerabilities[pkgName]` with `name`, `severity`
  (info/low/moderate/high/critical), `via` (advisory objects or, for
  transitive metavulns, plain dependency-name strings).
- CycloneDX (`cyclonedx-py environment` / `@cyclonedx/cyclonedx-npm`): both
  produce a CycloneDX JSON document with top-level `bomFormat`,
  `specVersion`, and a `components` array — confirmed by running both tools
  against this project and inspecting their real output.
"""

from __future__ import annotations

from typing import Any, Literal

from verdict.release.evidence import Finding, SBOMArtifact

_BANDIT_SEVERITY: dict[str, str] = {
    "HIGH": "high",
    "MEDIUM": "medium",
    "LOW": "low",
    "UNDEFINED": "info",
}

_NPM_SEVERITY: dict[str, str] = {
    "critical": "critical",
    "high": "high",
    "moderate": "medium",
    "low": "low",
    "info": "info",
}


def findings_from_pip_audit_json(data: dict[str, Any]) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    for dependency in data.get("dependencies", []):
        pkg_name = dependency.get("name", "unknown")
        for vuln in dependency.get("vulns", []):
            vuln_id = vuln.get("id", "unknown")
            findings.append(
                Finding(
                    id=f"pip-audit:{pkg_name}:{vuln_id}",
                    severity="high",
                    title=f"{pkg_name}: {vuln_id}",
                    source_check="dependency_scan",
                )
            )
    return tuple(findings)


def findings_from_bandit_json(data: dict[str, Any]) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    for result in data.get("results", []):
        test_id = result.get("test_id", "unknown")
        filename = result.get("filename", "unknown")
        line_number = result.get("line_number", 0)
        severity = _BANDIT_SEVERITY.get(result.get("issue_severity", "UNDEFINED"), "info")
        findings.append(
            Finding(
                id=f"bandit:{test_id}:{filename}:{line_number}",
                severity=severity,  # type: ignore[arg-type]
                title=result.get("issue_text", test_id),
                source_check="sast",
            )
        )
    return tuple(findings)


def findings_from_npm_audit_json(data: dict[str, Any]) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    for pkg_name, vuln in data.get("vulnerabilities", {}).items():
        severity = _NPM_SEVERITY.get(vuln.get("severity", "info"), "info")
        via = vuln.get("via", [])
        advisory = next((v for v in via if isinstance(v, dict)), None)
        title = advisory.get("title") if advisory else pkg_name
        source = advisory.get("source") if advisory else pkg_name
        findings.append(
            Finding(
                id=f"npm-audit:{pkg_name}:{source}",
                severity=severity,  # type: ignore[arg-type]
                title=title or pkg_name,
                source_check="dependency_scan",
            )
        )
    return tuple(findings)


def sbom_artifact_from_cyclonedx_json(
    ecosystem: Literal["python", "node"], file_path: str, data: dict[str, Any]
) -> SBOMArtifact:
    """Build an `SBOMArtifact` from a parsed CycloneDX JSON document.

    Callers are responsible for producing an `SBOMArtifact` with
    `generation_status="failed"` themselves when the generating tool exits
    non-zero or its output file is missing — there is nothing to parse in
    that case, so it is not this function's job (FR-009: a failed generation
    must never be silently dropped from the evidence record).
    """
    return SBOMArtifact(
        ecosystem=ecosystem,
        format_version=data["specVersion"],
        file_path=file_path,
        component_count=len(data.get("components", [])),
        generation_status="ok",
    )
