"""Tests for the attributed launch-gate waiver CLI (T029)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.record_waiver import record_waiver
from tests.security.test_launch_gate_evidence import _clean_record
from verdict.release.evidence import CheckResult, Finding


def test_record_waiver_appends_attributed_finding_waiver(tmp_path: Path) -> None:
    source = tmp_path / "evidence.json"
    output = tmp_path / "waived-evidence.json"
    finding_id = "F-CLI-1"
    record = _clean_record(
        dependency_scan=CheckResult(
            status="pass",
            findings=(
                Finding(
                    id=finding_id,
                    severity="critical",
                    title="Synthetic finding",
                    source_check="dependency_scan",
                ),
            ),
        )
    )
    source.write_text(json.dumps(record.to_dict()), encoding="utf-8")

    updated = record_waiver(
        source,
        output,
        scope="finding",
        reviewer="reviewer@example.invalid",
        reason="Synthetic test waiver",
        finding_id=finding_id,
        is_emergency_approver=False,
    )

    assert updated.overall_status == "pass"
    assert updated.waivers[-1].finding_id == finding_id
    assert output.exists()
    persisted = json.loads(output.read_text(encoding="utf-8"))
    assert persisted["waivers"][-1]["reviewer"] == "reviewer@example.invalid"
    assert source.read_text(encoding="utf-8") == json.dumps(record.to_dict())


def test_record_waiver_refuses_overwrite(tmp_path: Path) -> None:
    source = tmp_path / "evidence.json"
    output = tmp_path / "existing.json"
    source.write_text(json.dumps(_clean_record().to_dict()), encoding="utf-8")
    output.write_text("preserve", encoding="utf-8")

    with pytest.raises(ValueError, match="refusing to overwrite"):
        record_waiver(
            source,
            output,
            scope="finding",
            reviewer="reviewer@example.invalid",
            reason="Synthetic test waiver",
            finding_id="unused",
            is_emergency_approver=False,
        )
    assert output.read_text(encoding="utf-8") == "preserve"


def test_record_waiver_rejects_unregistered_outage_reviewer(tmp_path: Path) -> None:
    source = tmp_path / "evidence.json"
    output = tmp_path / "waived-evidence.json"
    source.write_text(json.dumps(_clean_record().to_dict()), encoding="utf-8")

    with pytest.raises(ValueError, match="not a registered emergency approver"):
        record_waiver(
            source,
            output,
            scope="gate_unavailable",
            reviewer="not-registered",
            reason="Synthetic outage",
            finding_id=None,
            is_emergency_approver=True,
        )
    assert not output.exists()
