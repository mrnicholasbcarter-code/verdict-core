"""Proof that the public offline demo script emits a clean COMPLETE run."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from verdict.orchestration.receipt import completion_verdict, verify_run_receipt

ROOT = Path(__file__).resolve().parents[1]


def test_demo_scenario_script_produces_complete_clean_receipt(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "demo_scenario.py"), str(runs_dir)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    output_lines = [line for line in completed.stdout.splitlines() if line.strip()]
    assert len(output_lines) == 1
    run_dir = Path(output_lines[0])
    assert run_dir == (runs_dir / "offline-flagship-failover").resolve()

    receipt = json.loads((run_dir / "receipt.json").read_text())
    outcome, _reason = completion_verdict(receipt)
    assert outcome == "COMPLETE"
    assert verify_run_receipt(run_dir) == []
