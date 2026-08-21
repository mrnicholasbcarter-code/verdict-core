"""Tests for the fail-closed release acceptance-gate verifier."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.verify_gates import GATE_IDS, REPORT_NAME, GateValidationError, main, validate_gates


def _write_report(root: Path, *, statuses: dict[str, str] | None = None) -> None:
    evidence: dict[str, list[str]] = {}
    for gate_id in GATE_IDS:
        path = f"checks/{gate_id}.txt"
        evidence[gate_id] = [path]
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"evidence for {gate_id}\n", encoding="utf-8")
    report = {
        "schema_version": 1,
        "repository": "mrnicholasbcarter-code/verdict-core",
        "commit": "0" * 40,
        "gates": [
            {
                "id": gate_id,
                "status": (statuses or {}).get(gate_id, "PASS"),
                "evidence": evidence[gate_id],
            }
            for gate_id in GATE_IDS
        ],
    }
    (root / REPORT_NAME).write_bytes(
        (json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    )


def test_complete_report_and_evidence_pass(tmp_path: Path) -> None:
    _write_report(tmp_path)

    summary = validate_gates(tmp_path)

    assert summary["gate_count"] == len(GATE_IDS) == 29
    assert summary["passed"] == 29
    assert summary["all_passed"] is True


def test_json_cli_is_machine_readable(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_report(tmp_path)

    assert main(["--evidence-dir", str(tmp_path), "--json"]) == 0
    output = json.loads(capsys.readouterr().out)

    assert output["all_passed"] is True
    assert output["valid"] is True
    assert output["gate_count"] == 29


def test_non_pass_gate_is_valid_but_blocks_release(tmp_path: Path) -> None:
    _write_report(tmp_path, statuses={"G3.2": "BLOCKED"})

    assert main(["--evidence-dir", str(tmp_path)]) == 1
    assert validate_gates(tmp_path)["all_passed"] is False


def test_json_cli_reports_invalid_input_without_claiming_a_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / REPORT_NAME).write_text("{not-json", encoding="utf-8")

    assert main(["--evidence-dir", str(tmp_path), "--json"]) == 1
    output = json.loads(capsys.readouterr().out)

    assert output["valid"] is False
    assert "malformed" in output["error"]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda report: report["gates"].pop(), "every gate ID"),
        (
            lambda report: report["gates"].__setitem__(
                0, {"id": "G9.1", "status": "PASS", "evidence": ["checks/G1.1.txt"]}
            ),
            "every gate ID",
        ),
        (
            lambda report: report["gates"].__setitem__(
                0, {"id": "G1.1", "status": "PASS", "evidence": ["../outside.txt"]}
            ),
            "unsafe evidence path",
        ),
        (
            lambda report: report["gates"].__setitem__(
                0, {"id": "G1.1", "status": "PASS", "evidence": ["checks/missing.txt"]}
            ),
            "missing",
        ),
        (
            lambda report: report["gates"].__setitem__(
                0, {"id": "G1.1", "status": "PASS", "evidence": []}
            ),
            "at least one",
        ),
        (
            lambda report: report["gates"].__setitem__(
                0,
                {
                    "id": "G1.1",
                    "status": "PASS",
                    "evidence": ["checks/G1.1.txt", "checks/G1.1.txt"],
                },
            ),
            "duplicate evidence",
        ),
        (
            lambda report: report["gates"].__setitem__(
                0, {"id": "G1.1", "status": "UNKNOWN", "evidence": ["checks/G1.1.txt"]}
            ),
            "invalid status",
        ),
    ],
)
def test_invalid_report_fails_closed(tmp_path: Path, mutation, message: str) -> None:
    _write_report(tmp_path)
    report_path = tmp_path / REPORT_NAME
    report = json.loads(report_path.read_text(encoding="utf-8"))
    mutation(report)
    report_path.write_text(
        json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8"
    )

    with pytest.raises(GateValidationError, match=message):
        validate_gates(tmp_path)


def test_noncanonical_report_and_symlink_evidence_are_rejected(tmp_path: Path) -> None:
    _write_report(tmp_path)
    report_path = tmp_path / REPORT_NAME
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    with pytest.raises(GateValidationError, match="canonical"):
        validate_gates(tmp_path)

    _write_report(tmp_path)
    link = tmp_path / "checks" / "G1.1.txt"
    real = link.with_name("real.txt")
    link.unlink()
    real.write_text("outside link target\n", encoding="utf-8")
    link.symlink_to(real)
    with pytest.raises(GateValidationError, match="symlink"):
        validate_gates(tmp_path)
