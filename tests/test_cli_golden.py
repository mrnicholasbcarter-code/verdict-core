"""BOD-187: every offline `--json` CLI contract stays byte-identical across the CLI migration.

Each case runs `python -m verdict <argv>` in an isolated HOME/cwd with copied input
fixtures, then compares stdout (after normalizing the scratch paths) and the exit
code with the captured baseline in tests/fixtures/cli_golden/expected/.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "fixtures" / "cli_golden"
CASES: list[dict[str, Any]] = json.loads((GOLDEN / "cases.json").read_text())["cases"]
# Mutating cases rewrite their inputs; each case gets a fresh copy.


def _normalize(text: str, mapping: dict[str, str]) -> str:
    for token, value in sorted(mapping.items(), key=lambda kv: -len(kv[1])):
        text = text.replace(value, token)
    return text


def _canonical(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text.strip()


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_cli_json_contract_is_unchanged(case: dict[str, Any], tmp_path: Path) -> None:
    home, run, inputs = tmp_path / "home", tmp_path / "run", tmp_path / "in"
    home.mkdir()
    run.mkdir()
    shutil.copytree(GOLDEN / "inputs", inputs)
    for produced in case.get("produces", []):  # outputs the command must create itself
        (inputs / produced).unlink(missing_ok=True)
    (inputs / "run").mkdir(exist_ok=True)
    mapping = {"{HOME}": str(home), "{RUN}": str(run), "{IN}": str(inputs), "{REPO}": str(ROOT)}
    argv = [part for arg in case["argv"] for part in [arg]]
    for token, value in mapping.items():
        argv = [a.replace(token, value) for a in argv]
    env = {k: v for k, v in os.environ.items() if not k.startswith(("VERDICT_", "LLMGATE_"))}
    env.pop("XDG_CONFIG_HOME", None)
    env.update(
        {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "NO_COLOR": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "OMNIROUTE_BASE_URL": "http://127.0.0.1:9",
            "VERDICT_GATEWAY": "http://127.0.0.1:9",
        }
    )
    proc = subprocess.run(
        [sys.executable, "-m", "verdict", *argv],
        cwd=run,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    expected = (GOLDEN / "expected" / f"{case['name']}.json").read_text()
    assert proc.returncode == case["exit_code"], proc.stderr[-800:]
    assert _canonical(_normalize(proc.stdout, mapping)) == _canonical(expected)
