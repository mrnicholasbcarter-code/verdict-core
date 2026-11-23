"""PR selection report runs the real ladder and fails on capability-floor violations."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "selection_report.py"
FIXTURE = ROOT / "benchmarks" / "fixtures" / "selection_inventory.json"


def _run(fixture: Path, tmp_path: Path) -> tuple[int, dict[str, object], str]:
    out = tmp_path / "report.json"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--fixture", str(fixture), "--output-json", str(out)],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    return proc.returncode, json.loads(out.read_text()), proc.stdout


def test_report_selects_within_every_role_floor(
    tmp_path: Path, billing_overlay_subscription: object
) -> None:
    # CP2 removed oauth → SUBSCRIPTION; declare the fixture's oauth providers.
    # The overlay is written to the isolated HOME so the subprocess picks it up.
    billing_overlay_subscription([("kiro", "oauth"), ("claude", "oauth")])  # type: ignore[call-arg]
    code, report, markdown = _run(FIXTURE, tmp_path)
    assert code == 0, markdown
    roles = {row["role"]: row for row in report["roles"]}  # type: ignore[index]
    assert report["violations"] == []
    low = roles["bounded implementation (low risk)"]
    assert low["selected"] == "kr/claude-haiku-4.5"
    for row in roles.values():
        assert row["tier"] is not None and row["tier"] <= row["floor"]
    review = next(r for name, r in roles.items() if name.startswith("independent review"))
    assert not str(review["selected"]).startswith("kr/claude")
    assert "Model selection (offline, real selector)" in markdown


def test_report_fails_when_no_route_meets_a_floor(tmp_path: Path) -> None:
    fixture = json.loads(FIXTURE.read_text())
    fixture["inventory"] = [r for r in fixture["inventory"] if "haiku" in r["id"]]
    weak = tmp_path / "weak.json"
    weak.write_text(json.dumps(fixture))
    code, report, _ = _run(weak, tmp_path)
    assert code == 1
    assert report["violations"]
