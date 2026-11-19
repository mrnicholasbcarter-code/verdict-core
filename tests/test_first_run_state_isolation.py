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


# Only call forms that build a path. Help text and docstrings that merely
# mention "~/.verdict/..." are plain strings and do not match.
_HOME_STATE = re.compile(
    r"Path\.home\(\)\)?\s*/\s*\"\.verdict\""
    r"|Path\(\s*[\"']~/\.verdict"
    r"|expanduser\(\s*[\"']~/\.verdict"
    r"|os\.path\.join\([^)]*[\"']\.verdict[\"']"
)


def test_no_module_builds_state_path_from_path_home() -> None:
    """Catches a ``~/.verdict`` default (``Path.home() / ".verdict"``, ``Path("~/.verdict")``,
    ``expanduser("~/.verdict")``, ``os.path.join(..., ".verdict")``) that bypasses verdict_home()."""
    offenders = [
        f"{path.relative_to(ROOT)}:{n}"
        for path in sorted((ROOT / "verdict").rglob("*.py"))
        if "openspec_vendor" not in path.parts
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if _HOME_STATE.search(line) and not line.lstrip().startswith("#")
    ]
    assert offenders == [], f"use verdict.paths.verdict_home(): {offenders}"


def test_runtime_state_dir_default_follows_verdict_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches the runtime state default ignoring VERDICT_HOME (was ``Path("~/.verdict/runtime")``)."""
    from verdict.runtime_contract import default_runtime_state_dir

    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("VERDICT_RUNTIME_STATE_DIR", raising=False)
    assert default_runtime_state_dir() == tmp_path / "state" / "runtime"
    monkeypatch.setenv("VERDICT_RUNTIME_STATE_DIR", str(tmp_path / "rt"))
    assert default_runtime_state_dir() == tmp_path / "rt"


def _decision_log_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    import verdict.paths as paths

    monkeypatch.setattr(paths, "_legacy_notice_done", False)
    cwd = tmp_path / "repo"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("VERDICT_HOME", str(state))
    return cwd / "verdict-decisions.jsonl", state / "verdict-decisions.jsonl"


_ROW = json.dumps({"decision": {"tier": 0, "model": "m"}, "effective_tier": 0}) + "\n"


def test_both_logs_reads_state_dir_and_notices_once_on_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Catches a user's older ./verdict-decisions.jsonl history vanishing from reports silently."""
    from verdict.paths import resolve_decision_log_for_read

    legacy, state = _decision_log_setup(tmp_path, monkeypatch)
    legacy.write_text(_ROW)
    state.write_text(_ROW)
    assert resolve_decision_log_for_read(None) == str(state)
    assert resolve_decision_log_for_read(None) == str(state)
    out, err = capsys.readouterr()
    assert out == ""
    assert err.count("\n") == 1
    assert "not being read" in err
    assert "--log_path ./verdict-decisions.jsonl" in err
    assert "LLMGATE_LOG_PATH=./verdict-decisions.jsonl" in err


def test_stats_cli_with_both_logs_keeps_stdout_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The notice goes to stderr only; stdout is the report from the state-dir log."""
    from verdict.cli import cmd_stats

    legacy, state = _decision_log_setup(tmp_path, monkeypatch)
    legacy.write_text(_ROW * 3)
    state.write_text(_ROW)
    cmd_stats(None)
    out, err = capsys.readouterr()
    assert "legacy decision log" not in out
    assert "verdict-decisions.jsonl" in err
    assert "Total Requests" in out and " 1 " in out


@pytest.mark.parametrize("case", ["legacy_only", "explicit", "env", "neither"])
def test_no_notice_without_a_hidden_legacy_log(
    case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only-legacy reads legacy; an explicit path or no log at all prints nothing."""
    from verdict.paths import resolve_decision_log_for_read

    legacy, state = _decision_log_setup(tmp_path, monkeypatch)
    if case == "legacy_only":
        legacy.write_text(_ROW)
        assert resolve_decision_log_for_read(None) == "verdict-decisions.jsonl"
    elif case == "explicit":
        legacy.write_text(_ROW)
        state.write_text(_ROW)
        assert resolve_decision_log_for_read("custom.jsonl") == "custom.jsonl"
    elif case == "env":
        legacy.write_text(_ROW)
        state.write_text(_ROW)
        monkeypatch.setenv("LLMGATE_LOG_PATH", "./verdict-decisions.jsonl")
        assert resolve_decision_log_for_read(None) == "./verdict-decisions.jsonl"
    else:
        assert resolve_decision_log_for_read(None) == str(state)
    assert capsys.readouterr().err == ""
