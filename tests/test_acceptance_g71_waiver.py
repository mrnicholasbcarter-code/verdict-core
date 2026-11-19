"""The G7.1 operator waiver is narrow: one gate, one variable, recorded as WAIVED."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import generate_gates_report as generator
from scripts.verify_gates import (
    GATE_IDS,
    REPORT_NAME,
    WAIVER_ENV,
    GateValidationError,
    validate_gates,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_report(root: Path, statuses: dict[str, str]) -> None:
    gates = []
    for gate_id in GATE_IDS:
        path = f"checks/{gate_id}.txt"
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"evidence for {gate_id}\n", encoding="utf-8")
        gates.append({"id": gate_id, "status": statuses.get(gate_id, "PASS"), "evidence": [path]})
    report = {
        "schema_version": 1,
        "repository": "mrnicholasbcarter-code/verdict-core",
        "commit": "0" * 40,
        "gates": gates,
    }
    (root / REPORT_NAME).write_bytes(
        (json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    )


def test_waived_g71_counts_toward_release_only_while_the_variable_is_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_report(tmp_path, {"G7.1": "WAIVED"})
    monkeypatch.setenv(WAIVER_ENV, "true")

    summary = validate_gates(tmp_path)

    assert summary["waived"] == 1
    assert summary["passed"] == len(GATE_IDS) - 1
    assert summary["all_passed"] is True


@pytest.mark.parametrize("value", [None, "", "false", "TRUE", "1"])
def test_waived_g71_is_invalid_without_the_exact_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    _write_report(tmp_path, {"G7.1": "WAIVED"})
    if value is None:
        monkeypatch.delenv(WAIVER_ENV, raising=False)
    else:
        monkeypatch.setenv(WAIVER_ENV, value)

    with pytest.raises(GateValidationError, match="WAIVED"):
        validate_gates(tmp_path)


def test_no_other_gate_can_be_waived(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_report(tmp_path, {"G7.3": "WAIVED"})
    monkeypatch.setenv(WAIVER_ENV, "true")

    with pytest.raises(GateValidationError, match=r"G7\.3 is WAIVED"):
        validate_gates(tmp_path)


def test_failed_g71_still_blocks_with_the_variable_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_report(tmp_path, {"G7.1": "FAIL"})
    monkeypatch.setenv(WAIVER_ENV, "true")

    assert validate_gates(tmp_path)["all_passed"] is False


def _quickstart_log(root: Path, last_result: str) -> generator.Gate:
    (root / "quickstart_test.log").write_text(f"probe failed\n{last_result}\n", encoding="utf-8")
    return generator.Gate(
        "G7.1", "The quickstart runs end to end", artifacts=("quickstart_test.log",)
    )


def test_generator_cli_reports_a_waived_g71(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _quickstart_log(tmp_path, "RESULT: WAIVED (VERDICT_ACCEPTANCE_WAIVE_G71=true)")
    monkeypatch.setenv(WAIVER_ENV, "true")

    assert generator.main(["--evidence-dir", str(tmp_path), "--repo-root", str(REPO_ROOT)]) == 0

    report = json.loads((tmp_path / REPORT_NAME).read_text(encoding="utf-8"))
    statuses = {gate["id"]: gate["status"] for gate in report["gates"]}
    assert statuses["G7.1"] == "WAIVED"
    assert list(statuses.values()).count("WAIVED") == 1
    assert "1 waived" in capsys.readouterr().err


def test_resolver_marks_a_waived_log_waived_only_when_waivable(tmp_path: Path) -> None:
    gate = _quickstart_log(tmp_path, "RESULT: WAIVED (VERDICT_ACCEPTANCE_WAIVE_G71=true)")

    assert generator._resolve_artifacts(gate, tmp_path, waivable=True).status == "WAIVED"
    assert generator._resolve_artifacts(gate, tmp_path).status == "FAIL"


def test_resolver_never_waives_a_plain_failure(tmp_path: Path) -> None:
    gate = _quickstart_log(tmp_path, "RESULT: FAIL (exit 1)")

    assert generator._resolve_artifacts(gate, tmp_path, waivable=True).status == "FAIL"


def test_waiver_applies_to_g71_only_and_needs_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(generator.WAIVER_ENV, raising=False)
    assert generator.waiver_active("G7.1") is False

    monkeypatch.setenv(generator.WAIVER_ENV, "true")
    assert generator.waiver_active("G7.1") is True
    assert generator.waiver_active("G7.2") is False
    assert generator.WAIVER_ENV == WAIVER_ENV


def test_quickstart_step_waives_only_through_the_variable_and_is_not_advisory() -> None:
    body = (REPO_ROOT / ".github/workflows/acceptance-gates.yml").read_text(encoding="utf-8")
    steps = [step for step in generator._workflow_steps(body) if "scripts/quickstart.py" in step]

    assert len(steps) == 1
    step = steps[0]
    assert "vars.VERDICT_ACCEPTANCE_WAIVE_G71" in step
    assert "set -o pipefail" in step
    assert "RESULT: WAIVED" in step
    assert "exit 1" in step
    assert not generator._is_advisory(step)
    for name in ("Generate the gate report", "Verify the acceptance gates"):
        matched = [s for s in generator._workflow_steps(body) if name in s]
        assert len(matched) == 1 and "vars.VERDICT_ACCEPTANCE_WAIVE_G71" in matched[0]
