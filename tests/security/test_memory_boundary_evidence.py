"""JUnit-to-evidence conversion tests for memory boundary results (T021)."""

from __future__ import annotations

from pathlib import Path

from verdict.release.evidence import (
    check_result_from_junit_xml,
    memory_boundary_results_from_junit_xml,
)

_MODULES = ("verdict/memory_gate.py", "verdict/memory_bridge.py", "verdict/memory_plane.py")


def test_junit_boundary_results_preserve_passes_and_fail_closed_outcomes(tmp_path: Path) -> None:
    report = tmp_path / "security-junit.xml"
    report.write_text(
        """<?xml version="1.0"?>
<testsuites>
  <testsuite name="security">
    <testcase classname="tests.security.test_memory_gate_boundary" name="pass" />
    <testcase classname="tests.security.test_memory_gate_boundary" name="xfail">
      <skipped type="pytest.xfail" message="secret boundary gap" />
    </testcase>
    <testcase classname="tests.security.test_memory_bridge_boundary" name="pass" />
    <testcase classname="tests.security.test_memory_plane_boundary" name="error">
      <error type="RuntimeError" message="fixture setup failed" />
    </testcase>
  </testsuite>
</testsuites>
""",
        encoding="utf-8",
    )

    results = memory_boundary_results_from_junit_xml(report, boundary_modules=_MODULES)

    assert [result.boundary_module for result in results] == list(_MODULES)
    assert results[0].status == "fail"
    assert results[0].secret_leak_detected is True
    assert results[1].status == "pass"
    assert results[2].status == "fail"
    assert results[2].pii_leak_detected is True
    assert results[2].secret_leak_detected is True


def test_missing_or_malformed_junit_report_fails_closed(tmp_path: Path) -> None:
    missing = memory_boundary_results_from_junit_xml(
        tmp_path / "missing.xml", boundary_modules=_MODULES
    )
    assert all(result.status == "fail" for result in missing)

    malformed_path = tmp_path / "malformed.xml"
    malformed_path.write_text("<testsuites>", encoding="utf-8")
    malformed = memory_boundary_results_from_junit_xml(malformed_path, boundary_modules=_MODULES)
    assert all(result.status == "fail" for result in malformed)


def test_junit_doctype_is_rejected_before_xml_parsing(tmp_path: Path) -> None:
    report = tmp_path / "unsafe.xml"
    report.write_text(
        "<!DOCTYPE testsuites [<!ENTITY secret 'synthetic'>]><testsuites />", encoding="utf-8"
    )

    results = memory_boundary_results_from_junit_xml(report, boundary_modules=_MODULES)

    assert all(result.status == "fail" for result in results)


def test_privacy_check_result_fails_on_skipped_or_missing_report(tmp_path: Path) -> None:
    passing = tmp_path / "passing.xml"
    passing.write_text(
        "<testsuite><testcase classname='tests.privacy' name='pass' /></testsuite>",
        encoding="utf-8",
    )
    skipped = tmp_path / "skipped.xml"
    skipped.write_text(
        "<testsuite><testcase classname='tests.privacy' name='skip'>"
        "<skipped type='pytest.xfail' /></testcase></testsuite>",
        encoding="utf-8",
    )

    assert check_result_from_junit_xml(passing).status == "pass"
    assert check_result_from_junit_xml(skipped).status == "failed"
    assert check_result_from_junit_xml(tmp_path / "missing.xml").status == "unavailable"
