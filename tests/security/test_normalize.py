"""Tests for verdict.release.normalize (T004 tool-output normalization).

Fixtures mirror the real JSON shapes confirmed by reading each tool's own
source: pip_audit/_format/json.py, bandit/formatters/json.py, and
@npmcli/arborist/lib/vuln.js's `toJSON`. The clean-repo shells (no vulns) were
also captured live from this project via `pip-audit --local -f json` and
`npm audit --json` to confirm the top-level keys; a `vulns`/`vulnerabilities`
entry is added synthetically here since this repo currently has none.
"""

from __future__ import annotations

from verdict.release.normalize import (
    findings_from_bandit_json,
    findings_from_npm_audit_json,
    findings_from_pip_audit_json,
    sbom_artifact_from_cyclonedx_json,
)


def test_pip_audit_findings_default_to_high_severity() -> None:
    data = {
        "dependencies": [
            {"name": "aiofile", "version": "3.11.1", "vulns": []},
            {
                "name": "vulnerable-pkg",
                "version": "1.0.0",
                "vulns": [
                    {
                        "id": "PYSEC-2024-1",
                        "fix_versions": ["1.0.1"],
                        "aliases": ["CVE-2024-0001"],
                        "description": "example vulnerability",
                    }
                ],
            },
        ],
        "fixes": [],
    }
    findings = findings_from_pip_audit_json(data)
    assert len(findings) == 1
    assert findings[0].severity == "high"
    assert findings[0].source_check == "dependency_scan"
    assert "vulnerable-pkg" in findings[0].id
    assert "PYSEC-2024-1" in findings[0].id


def test_pip_audit_empty_dependencies_yields_no_findings() -> None:
    assert findings_from_pip_audit_json({"dependencies": [], "fixes": []}) == ()


def test_bandit_findings_map_severity() -> None:
    data = {
        "results": [
            {
                "test_id": "B301",
                "test_name": "blacklist_calls",
                "issue_severity": "HIGH",
                "issue_confidence": "HIGH",
                "issue_text": "Use of unsafe pickle load",
                "filename": "verdict/foo.py",
                "line_number": 42,
            },
            {
                "test_id": "B101",
                "test_name": "assert_used",
                "issue_severity": "UNDEFINED",
                "issue_confidence": "LOW",
                "issue_text": "Use of assert detected",
                "filename": "tests/bar.py",
                "line_number": 7,
            },
        ],
        "errors": [],
        "metrics": {},
    }
    findings = findings_from_bandit_json(data)
    assert len(findings) == 2
    assert findings[0].severity == "high"
    assert findings[0].source_check == "sast"
    assert "B301" in findings[0].id
    assert findings[1].severity == "info"


def test_bandit_no_results_yields_no_findings() -> None:
    assert findings_from_bandit_json({"results": [], "errors": [], "metrics": {}}) == ()


def test_npm_audit_findings_map_moderate_to_medium() -> None:
    data = {
        "auditReportVersion": 2,
        "vulnerabilities": {
            "lodash": {
                "name": "lodash",
                "severity": "moderate",
                "isDirect": True,
                "via": [
                    {
                        "source": 1094123,
                        "name": "lodash",
                        "dependency": "lodash",
                        "title": "Prototype Pollution in lodash",
                        "url": "https://example.invalid/advisories/1094123",
                        "severity": "moderate",
                    }
                ],
                "effects": [],
                "range": "<4.17.21",
                "nodes": ["node_modules/lodash"],
                "fixAvailable": True,
            }
        },
        "metadata": {},
    }
    findings = findings_from_npm_audit_json(data)
    assert len(findings) == 1
    assert findings[0].severity == "medium"
    assert findings[0].source_check == "dependency_scan"
    assert findings[0].title == "Prototype Pollution in lodash"


def test_npm_audit_no_vulnerabilities_yields_no_findings() -> None:
    assert findings_from_npm_audit_json({"vulnerabilities": {}, "metadata": {}}) == ()


def test_npm_audit_transitive_metavuln_via_string_falls_back_to_pkg_name() -> None:
    data = {
        "vulnerabilities": {
            "transitive-pkg": {
                "name": "transitive-pkg",
                "severity": "high",
                "isDirect": False,
                "via": ["upstream-pkg"],
                "effects": [],
                "range": "*",
                "nodes": ["node_modules/transitive-pkg"],
                "fixAvailable": False,
            }
        },
        "metadata": {},
    }
    findings = findings_from_npm_audit_json(data)
    assert len(findings) == 1
    assert findings[0].title == "transitive-pkg"


def test_sbom_artifact_from_cyclonedx_json_python() -> None:
    data = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "components": [{"type": "library", "name": "httpx"}, {"type": "library", "name": "rich"}],
    }
    artifact = sbom_artifact_from_cyclonedx_json("python", "sbom-python.cdx.json", data)
    assert artifact.ecosystem == "python"
    assert artifact.format == "CycloneDX"
    assert artifact.format_version == "1.6"
    assert artifact.component_count == 2
    assert artifact.generation_status == "ok"


def test_sbom_artifact_from_cyclonedx_json_no_components() -> None:
    data = {"bomFormat": "CycloneDX", "specVersion": "1.6"}
    artifact = sbom_artifact_from_cyclonedx_json("node", "sbom-node.cdx.json", data)
    assert artifact.component_count == 0
