"""The benchmark job comments on a pull request only when the PR changes a result."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "pr_check_comment.py"
LOW_ROLE = "bounded implementation (low risk)"
HIGH_ROLE = "high-risk implementation"


def _gate(*, passed: bool = True) -> dict[str, object]:
    slow_p95 = 900 if passed else 9_000_000
    return {
        "metrics": {"thresholds_passed": passed, "sample_count": 50},
        "benchmarks": [
            {
                "name": "contract_roundtrip",
                "summary": {"p95": 700, "samples": 25, "unit": "ns"},
                "thresholds": {"p95_ns_max": 5_000_000},
                "threshold_passed": True,
            },
            {
                "name": "dispatcher_eligibility",
                "summary": {"p95": slow_p95, "samples": 25, "unit": "ns"},
                "thresholds": {"p95_ns_max": 5_000_000},
                "threshold_passed": passed,
            },
        ],
    }


def _selection(
    low: str = "kr/claude-haiku-4.5", violations: list[str] | None = None
) -> dict[str, object]:
    return {
        "fixture": "selection_inventory.json",
        "roles": [
            {"role": LOW_ROLE, "selected": low, "tier": 3, "floor": 3},
            {"role": HIGH_ROLE, "selected": "cc/claude-sonnet-4.6", "tier": 1, "floor": 1},
        ],
        "violations": violations or [],
    }


def _run(
    tmp_path: Path,
    *,
    gate: dict[str, object] | None,
    head: dict[str, object] | None,
    base: dict[str, object] | None,
) -> tuple[int, str, str]:
    """Run the script. A ``None`` payload means that report file does not exist."""
    output = tmp_path / "comment.md"
    args = [sys.executable, str(SCRIPT), "--output-md", str(output)]
    for flag, payload in (("--gate-json", gate), ("--head-json", head), ("--base-json", base)):
        path = tmp_path / f"{flag.strip('-')}.json"
        if payload is not None:
            path.write_text(json.dumps(payload))
        args += [flag, str(path)]
    proc = subprocess.run(args, capture_output=True, text=True, cwd=ROOT)
    return proc.returncode, output.read_text(), proc.stdout


def test_no_comment_when_the_pull_request_changes_nothing(tmp_path: Path) -> None:
    code, comment, _ = _run(tmp_path, gate=_gate(), head=_selection(), base=_selection())
    assert code == 0
    assert comment == ""


def test_comment_names_only_the_role_whose_selection_changed(tmp_path: Path) -> None:
    code, comment, _ = _run(
        tmp_path, gate=_gate(), head=_selection(low="cx/gpt-5.4"), base=_selection()
    )
    assert code == 0
    assert LOW_ROLE in comment
    assert "kr/claude-haiku-4.5" in comment and "cx/gpt-5.4" in comment
    assert HIGH_ROLE not in comment
    assert "not the live gateway" in comment


def test_comment_names_only_the_timing_case_over_its_limit(tmp_path: Path) -> None:
    code, comment, _ = _run(
        tmp_path, gate=_gate(passed=False), head=_selection(), base=_selection()
    )
    assert code == 0
    assert "dispatcher_eligibility" in comment
    assert "9.00 ms" in comment and "5.00 ms" in comment
    assert "contract_roundtrip" not in comment


def test_comment_reports_a_capability_floor_violation(tmp_path: Path) -> None:
    broken = _selection(violations=[f"{HIGH_ROLE}: no route selected"])
    code, comment, _ = _run(tmp_path, gate=_gate(), head=broken, base=_selection())
    assert code == 0
    assert f"{HIGH_ROLE}: no route selected" in comment


def test_missing_base_report_claims_no_selection_change(tmp_path: Path) -> None:
    code, comment, stdout = _run(
        tmp_path, gate=_gate(), head=_selection(low="cx/gpt-5.4"), base=None
    )
    assert code == 0
    assert comment == ""
    assert "not compared" in stdout


def test_missing_timing_report_is_reported_not_treated_as_a_pass(tmp_path: Path) -> None:
    code, comment, _ = _run(tmp_path, gate=None, head=_selection(), base=_selection())
    assert code == 0
    assert "Timing check" in comment and "no report" in comment
