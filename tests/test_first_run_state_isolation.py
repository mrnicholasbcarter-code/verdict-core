"""First-run state isolation: state goes under the resolved state directory only.

Regressions for the v1 first-run gaps G1 (fixture demo wrote live-looking proof
into VERDICT_HOME), G1b / #1136 (SessionLedger ignored VERDICT_HOME) and G6b
(``verdict route`` wrote ``verdict-decisions.jsonl`` into the working directory).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _files(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def test_fixture_demo_writes_nothing_admission_or_proof_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Catches the demo appending real_task passes, healthy passports and a
    dangling verified-runs entry to the user's state (fixture != live proof)."""
    from verdict.cli import main

    home = tmp_path / "home"
    state = tmp_path / "state"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("VERDICT_HOME", str(state))
    monkeypatch.setattr("sys.argv", ["verdict", "demo", "--json", "--worker-seconds", "0"])
    main()
    assert json.loads(capsys.readouterr().out)["mode"] == "offline"
    assert not state.exists() or _files(state) == []
    assert not (home / ".verdict").exists() or _files(home / ".verdict") == []


def test_session_ledger_default_follows_verdict_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches #1136: the default ledger was Path.home()/.verdict even with VERDICT_HOME set."""
    from verdict.orchestration.session_evidence import SessionLedger

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("VERDICT_SESSION_EVIDENCE", raising=False)
    assert SessionLedger().path == tmp_path / "state" / "session-evidence.jsonl"


def test_route_decision_log_goes_to_state_dir_not_cwd(tmp_path: Path) -> None:
    """Catches ``verdict route`` dropping verdict-decisions.jsonl into the caller's repo."""
    cwd = tmp_path / "repo"
    cwd.mkdir()
    state = tmp_path / "state"
    env = {
        k: v for k, v in os.environ.items() if not k.startswith(("VERDICT_", "LLMGATE_", "XDG_"))
    }
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "VERDICT_HOME": str(state),
            "OMNIROUTE_BASE_URL": "http://127.0.0.1:9/v1",
            "PYTHONPATH": str(ROOT),
            "NO_COLOR": "1",
        }
    )
    subprocess.run(
        [sys.executable, "-m", "verdict", "route", "--allow-offline", "--terse", "x"],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert _files(cwd) == []
    assert (state / "verdict-decisions.jsonl").is_file()


def test_decision_log_default_and_override(tmp_path: Path) -> None:
    """Default is under the state dir; LLMGATE_LOG_PATH still overrides it."""
    from verdict.provider_bootstrap import resolve_provider_bootstrap

    base = {"OMNIROUTE_BASE_URL": "http://127.0.0.1:20128", "VERDICT_HOME": str(tmp_path / "s")}
    absent = tmp_path / "absent.yaml"
    default = resolve_provider_bootstrap(env=base, config_path=absent)
    assert default.log_path == str(tmp_path / "s" / "verdict-decisions.jsonl")
    custom = resolve_provider_bootstrap(
        env={**base, "LLMGATE_LOG_PATH": "x.jsonl"}, config_path=absent
    )
    assert custom.log_path == "x.jsonl"


_HOME_STATE = re.compile(r"Path\.home\(\)\)?\s*/\s*\"\.verdict\"")


def test_no_module_builds_state_path_from_path_home() -> None:
    """Catches a new ``Path.home() / ".verdict"`` default that bypasses verdict_home()."""
    offenders = [
        f"{path.relative_to(ROOT)}:{n}"
        for path in sorted((ROOT / "verdict").rglob("*.py"))
        if "openspec_vendor" not in path.parts
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if _HOME_STATE.search(line) and not line.lstrip().startswith("#")
    ]
    assert offenders == [], f"use verdict.paths.verdict_home(): {offenders}"
