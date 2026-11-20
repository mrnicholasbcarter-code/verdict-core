"""First-run state isolation: state goes under the resolved state directory only.

Regressions for the v1 first-run gaps G1 (fixture demo wrote live-looking proof
into VERDICT_HOME), G1b / #1136 (SessionLedger ignored VERDICT_HOME) and G6b
(``verdict route`` wrote ``verdict-decisions.jsonl`` into the working directory).

L1a regressions: fixture and test data never reach live ledgers or stores.
- demo_orchestrate.py wrote session-evidence and passport store rows into the
  user's VERDICT_HOME because it lacked scoped_state_env.
- prime_supervisor.py built passport_store from Path.home()/.verdict regardless
  of VERDICT_HOME, so a custom VERDICT_HOME was ignored.
- VERDICT_SESSION_EVIDENCE was missing from _HOST_STATE_ENV in conftest.py.
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
    ``expanduser("~/.verdict")``, ``os.path.join(..., ".verdict")``) that bypasses verdict_home().

    Scans both ``verdict/`` (the package) and ``scripts/`` (operator scripts that
    also run in the user's environment and must respect VERDICT_HOME).
    """
    scan_roots = [(ROOT / "verdict").rglob("*.py"), (ROOT / "scripts").rglob("*.py")]
    offenders = [
        f"{path.relative_to(ROOT)}:{n}"
        for paths in scan_roots
        for path in sorted(paths)
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


# ---------------------------------------------------------------------------
# L1a: fixture and test data never reach live ledgers or stores
# ---------------------------------------------------------------------------


def test_demo_orchestrate_leaves_session_evidence_and_passport_store_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Catches scripts/demo_orchestrate.py writing fixture rows into the user's
    real session-evidence.jsonl and prove-at-rest/state.json.

    Pre-populate both files under a fresh VERDICT_HOME, run the demo CLI, then
    assert the bytes are unchanged (fixture run leaves live stores byte-identical).
    """
    state = tmp_path / "state"
    home = tmp_path / "home"
    home.mkdir()
    # Pre-populate session evidence with one row so a byte-compare is non-trivial.
    evidence_file = state / "session-evidence.jsonl"
    state.mkdir(parents=True)
    passport_dir = state / "prove-at-rest"
    passport_dir.mkdir(parents=True)
    passport_file = passport_dir / "state.json"
    _sentinel_evidence = '{"route_id":"canary/sentinel","outcome":"pass","kind":"session_canary_v1","failure_class":null,"verified_by_controller":true,"at":"2026-01-01T00:00:00+00:00","source":"test"}\n'
    _sentinel_passport = '{"format":"prove-at-rest-v1","entries":{}}\n'
    evidence_file.write_text(_sentinel_evidence, encoding="utf-8")
    passport_file.write_text(_sentinel_passport, encoding="utf-8")

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("VERDICT_HOME", str(state))
    monkeypatch.setattr("sys.argv", ["verdict", "demo", "--json", "--worker-seconds", "0"])
    from verdict.cli import main

    main()
    # The demo must leave both files byte-identical to their pre-run content.
    assert evidence_file.read_text(encoding="utf-8") == _sentinel_evidence, (
        "demo_orchestrate wrote to the user's session-evidence.jsonl"
    )
    assert passport_file.read_text(encoding="utf-8") == _sentinel_passport, (
        "demo_orchestrate wrote to the user's prove-at-rest/state.json"
    )
    assert not (home / ".verdict").exists() or _files(home / ".verdict") == [], (
        "demo_orchestrate wrote to the unscoped HOME/.verdict"
    )


def test_prime_supervisor_passport_store_follows_verdict_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches prime_supervisor.py ignoring VERDICT_HOME for the passport store path.

    When VERDICT_HOME differs from HOME/.verdict, _build_intelligence_service_from_config
    must look for the passport store under VERDICT_HOME, not under HOME/.verdict.
    """
    import importlib.util as _ilu

    home = tmp_path / "home"
    state = tmp_path / "state"
    home.mkdir()
    # Passport store only under VERDICT_HOME, not under HOME/.verdict.
    passport_path = state / "prove-at-rest" / "state.json"
    passport_path.parent.mkdir(parents=True)
    passport_path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("VERDICT_HOME", str(state))
    # Load the module fresh so the monkeypatched env is in effect.
    path = ROOT / "scripts" / "prime_supervisor.py"
    spec = _ilu.spec_from_file_location("_ps_l1a", path)
    assert spec is not None and spec.loader is not None
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # _build_intelligence_service_from_config reads passport_store via verdict_home().
    # We mock IntelligenceService to capture the passport_store_path kwarg.
    captured: dict[str, object] = {}

    class _FakeIS:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    # The function uses a local import: `from verdict.intelligence import IntelligenceService`.
    # Patching mod.IntelligenceService has no effect — the local import fetches the
    # name from the verdict.intelligence module at call time.  Patch there instead.
    import verdict.intelligence as _vi

    monkeypatch.setattr(_vi, "IntelligenceService", _FakeIS)
    # bootstrap fails (no config), so the function raises ControllerLaunchError.
    # That's fine — we only care about whether the passport_store was resolved
    # from VERDICT_HOME before the bootstrap check; since passport_store is built
    # AFTER bootstrap, we need to inject a fake bootstrap too.
    from unittest.mock import MagicMock

    fake_bootstrap = MagicMock()
    fake_bootstrap.primary_model = "fake/model"
    fake_bootstrap.provider_configs.return_value = []
    fake_bootstrap.profile = "default"
    fake_bootstrap.log_path = str(tmp_path / "decisions.jsonl")

    def fake_resolve(**kwargs: object) -> object:
        return fake_bootstrap

    monkeypatch.setattr("verdict.provider_bootstrap.resolve_provider_bootstrap", fake_resolve)
    monkeypatch.setattr("verdict.provider_bootstrap.load_credential_store_env", lambda: {})
    # Call the function; it will construct _FakeIS with the passport_store_path kwarg.
    mod._build_intelligence_service_from_config(repo=tmp_path, state_dir=tmp_path)
    # The passport_store_path must come from VERDICT_HOME, not HOME/.verdict.
    expected = passport_path
    actual = captured.get("passport_store_path")
    assert actual == expected, (
        f"expected passport_store_path={expected}, got {actual!r}; "
        "prime_supervisor.py must use verdict_home() for the passport store"
    )


def test_session_evidence_env_var_isolated_in_conftest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches VERDICT_SESSION_EVIDENCE missing from _HOST_STATE_ENV in conftest.

    If an operator exports VERDICT_SESSION_EVIDENCE pointing at their real
    ~/.verdict/session-evidence.jsonl, the autouse isolator must drop it so
    tests resolve the path through the isolated HOME, not the real one.
    """
    # Simulate an operator export that conftest should have stripped.
    real_path = tmp_path / "real-evidence.jsonl"
    # By the time this test runs, conftest._isolate_host_state_env has already
    # deleted VERDICT_SESSION_EVIDENCE from os.environ. Assert it is gone.
    assert "VERDICT_SESSION_EVIDENCE" not in os.environ, (
        "VERDICT_SESSION_EVIDENCE leaked from the operator environment into the "
        "test suite; add it to _HOST_STATE_ENV in tests/conftest.py"
    )
    # Confirm SessionLedger() resolves through the already-isolated HOME.
    from verdict.orchestration.session_evidence import SessionLedger

    ledger = SessionLedger()
    assert ".verdict" in str(ledger.path), "SessionLedger path must be under .verdict"
    assert str(real_path) not in str(ledger.path), (
        "SessionLedger resolved to the real file despite VERDICT_SESSION_EVIDENCE isolation"
    )
