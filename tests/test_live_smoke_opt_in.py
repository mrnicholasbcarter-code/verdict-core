"""The live smoke is opt-in: it refuses to run without VERDICT_LIVE_SMOKE=1."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "live_failover_smoke.py"


def test_live_smoke_refuses_without_explicit_opt_in(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("VERDICT_LIVE_SMOKE", raising=False)
    proc = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True)
    assert proc.returncode == 2
    assert "VERDICT_LIVE_SMOKE=1" in proc.stderr
