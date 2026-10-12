"""The credential-free orchestration demo runs to COMPLETE offline.

It broke silently twice (BOD-271 unknown capability, BOD-304 agentic gate)
because nothing ran it; this test does, into a temporary directory, never
into the committed proof bundle.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _demo():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(
        "demo_orchestrate", ROOT / "scripts" / "demo_orchestrate.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_demo_runs_to_a_verified_complete_receipt(tmp_path: Path) -> None:
    from verdict.orchestration.receipt import verify_run_receipt

    out = tmp_path / "run"
    assert _demo().main(["--out", str(out)]) == 0
    receipt = json.loads((out / "receipt.json").read_text())
    assert receipt["outcome"] == "COMPLETE", receipt["reason"]
    assert verify_run_receipt(out) == []
