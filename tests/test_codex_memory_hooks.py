"""RED tests for Codex 0.161.0 memory hooks (lane B).

Fixtures under tests/fixtures/codex/v0_161_0/ are SYNTHETIC: shaped like Codex
hook stdin JSON but not yet observed live. Installer import path is
``verdict.memory_harness.codex`` (moved out of memory_bridge by the E0 extract).
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from verdict import harness_codex
from verdict.memory_harness import codex as codex_installer

FIXTURES = Path(__file__).parent / "fixtures" / "codex" / "v0_161_0"
CODEX_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PermissionRequest",
    "PreCompact",
    "PostCompact",
    "SubagentStart",
    "SubagentStop",
    "Stop",
    "SessionEnd",
)
USER_HOOK = {
    "matcher": "Bash",
    "hooks": [{"type": "command", "command": "my-own-audit --log", "timeout": 5}],
}


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text("utf-8"))


def _verdict_commands(data: dict) -> list[str]:
    return [
        hook["command"]
        for groups in data.get("hooks", {}).values()
        for group in groups
        for hook in group.get("hooks", [])
        if "verdict" in hook.get("command", "")
    ]


@pytest.fixture
def codex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    ch = home / ".codex"
    ch.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(ch))
    return ch


LIVE_EVENTS = {
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "Stop",
    "SessionEnd",
}


def test_fixtures_are_marked_synthetic_or_live() -> None:
    files = sorted(FIXTURES.glob("*.json"))
    assert files
    for f in files:
        payload = json.loads(f.read_text("utf-8"))
        live = f.name.startswith("live_")
        assert payload.get("_live" if live else "_synthetic") is True, f.name
        for key in ("session_id", "cwd", "hook_event_name", "transcript_path"):
            assert key in payload, (f.name, key)
        assert "/home/nick" not in f.read_text("utf-8")


def test_live_fixtures_cover_supported_events() -> None:
    seen = {
        json.loads(f.read_text("utf-8"))["hook_event_name"] for f in FIXTURES.glob("live_*.json")
    }
    assert seen == LIVE_EVENTS
    report = _certify_levels()
    assert {e for e, lvl in report.items() if lvl == "supported"} == LIVE_EVENTS


def test_live_payloads_translate_with_provenance() -> None:
    records = [
        codex_installer.event_provenance(json.loads(f.read_text("utf-8")))
        for f in FIXTURES.glob("live_*.json")
    ]
    assert all(r["session_id"] and r["cwd"] for r in records)
    assert len({r["event_hash"] for r in records}) == len(records)
    assert {r["event"] for r in records} == LIVE_EVENTS


def test_installer_reads_stdin_not_tmp_files(codex_home: Path, tmp_path: Path) -> None:
    hooks_file = codex_home / "hooks.json"
    codex_installer.install_codex_hooks(hooks_file, tmp_path / "plane.db")
    commands = _verdict_commands(json.loads(hooks_file.read_text("utf-8")))
    assert commands
    for cmd in commands:
        assert "/tmp/codex_" not in cmd
        assert "$(cat" not in cmd
        assert cmd.startswith(f"{shlex.quote(sys.executable)} -m verdict.memory_harness.codex ")


def test_installer_registers_every_codex_event(codex_home: Path, tmp_path: Path) -> None:
    hooks_file = codex_home / "hooks.json"
    codex_installer.install_codex_hooks(hooks_file, tmp_path / "plane.db")
    data = json.loads(hooks_file.read_text("utf-8"))
    for event in CODEX_EVENTS:
        cmds = [h["command"] for g in data["hooks"].get(event, []) for h in g["hooks"]]
        assert any(c == codex_installer.hook_command(event) for c in cmds), event


def test_hook_command_uses_the_installing_interpreter(monkeypatch: pytest.MonkeyPatch) -> None:
    # pipx/uv installs: a bare `python` on PATH would not have verdict.
    monkeypatch.setattr(sys, "executable", "/opt/pipx venvs/verdict/bin/python")
    cmd = codex_installer.hook_command("Stop")
    assert cmd == "'/opt/pipx venvs/verdict/bin/python' -m verdict.memory_harness.codex Stop"
    assert shlex.split(cmd)[0] == "/opt/pipx venvs/verdict/bin/python"


def test_reinstall_replaces_legacy_bare_python_hooks(codex_home: Path, tmp_path: Path) -> None:
    hooks_file = codex_home / "hooks.json"
    legacy = {
        "type": "command",
        "command": "python -m verdict.memory_harness.codex Stop",
        "timeout": 10,
    }
    hooks_file.write_text(
        json.dumps({"hooks": {"Stop": [{"hooks": [legacy]}, USER_HOOK]}}), "utf-8"
    )
    codex_installer.install_codex_hooks(hooks_file, tmp_path / "plane.db")
    data = json.loads(hooks_file.read_text("utf-8"))
    stop = [h["command"] for g in data["hooks"]["Stop"] for h in g["hooks"]]
    assert stop.count(codex_installer.hook_command("Stop")) == 1
    assert "python -m verdict.memory_harness.codex Stop" not in stop
    assert "my-own-audit --log" in stop


# Ownership is an exact parsed match: [python, "-m", module, Event]. These mention
# the module but are user hooks and must survive uninstall and reinstall.
CODEX_FOREIGN_COMMANDS = (
    "logger -- 'python -m verdict.memory_harness.codex Stop'",
    "sh -c 'python -m verdict.memory_harness.codex Stop'",
    "python -m verdict.memory_harness.codex Stop --extra-arg",
    "python -m verdict.memory_harness.codex_extra Stop",
    "echo -m verdict.memory_harness.codex Stop",
    "ipython -m verdict.memory_harness.codex Stop",
    "python -m verdict.memory_harness.codex NotACodexEvent",
    "python -m verdict.memory_harness.codex 'Stop",
    "logger -- 'verdict hook recall $(cat /tmp/codex_session_prompt)'",
)
CODEX_OWNED_COMMANDS = (
    f"{shlex.quote(sys.executable)} -m verdict.memory_harness.codex Stop",
    "'/opt/pipx venvs/verdict/bin/python' -m verdict.memory_harness.codex Stop",
    "python -m verdict.memory_harness.codex Stop",
    "python3 -m verdict.memory_harness.codex Stop",
    "/usr/bin/python3.12 -m verdict.memory_harness.codex Stop",
    ".venv/bin/python -m verdict.memory_harness.codex Stop",
    "verdict hook recall '$(cat /tmp/codex_session_prompt)'",
)


def _stop_hooks(*commands: str) -> dict:
    hooks = [{"type": "command", "command": c, "timeout": 5} for c in commands]
    return {"hooks": {"Stop": [{"hooks": hooks}]}}


def _stop_commands(hooks_file: Path) -> list[str]:
    data = json.loads(hooks_file.read_text("utf-8"))
    return [h["command"] for g in data["hooks"]["Stop"] for h in g["hooks"]]


@pytest.mark.parametrize("foreign", CODEX_FOREIGN_COMMANDS)
def test_uninstall_preserves_foreign_hooks_that_mention_the_module(
    codex_home: Path, foreign: str
) -> None:
    hooks_file = codex_home / "hooks.json"
    hooks_file.write_text(json.dumps(_stop_hooks(foreign)), "utf-8")
    codex_installer.uninstall_codex_hooks(hooks_file)
    assert json.loads(hooks_file.read_text("utf-8")) == _stop_hooks(foreign)


@pytest.mark.parametrize("foreign", CODEX_FOREIGN_COMMANDS)
def test_reinstall_migration_preserves_foreign_hooks(
    codex_home: Path, tmp_path: Path, foreign: str
) -> None:
    hooks_file = codex_home / "hooks.json"
    legacy = "python -m verdict.memory_harness.codex Stop"
    hooks_file.write_text(json.dumps(_stop_hooks(foreign, legacy)), "utf-8")
    codex_installer.install_codex_hooks(hooks_file, tmp_path / "plane.db")
    stop = _stop_commands(hooks_file)
    assert foreign in stop
    assert legacy not in stop
    assert stop.count(codex_installer.hook_command("Stop")) == 1


@pytest.mark.parametrize("owned", CODEX_OWNED_COMMANDS)
def test_pinned_bare_and_relative_verdict_hooks_are_owned(
    codex_home: Path, tmp_path: Path, owned: str
) -> None:
    hooks_file = codex_home / "hooks.json"
    hooks_file.write_text(json.dumps(_stop_hooks(owned, "my-own-audit --log")), "utf-8")
    codex_installer.uninstall_codex_hooks(hooks_file)
    assert json.loads(hooks_file.read_text("utf-8")) == _stop_hooks("my-own-audit --log")
    hooks_file.write_text(json.dumps(_stop_hooks(owned, "my-own-audit --log")), "utf-8")
    codex_installer.install_codex_hooks(hooks_file, tmp_path / "plane.db")
    stop = _stop_commands(hooks_file)
    assert stop.count(codex_installer.hook_command("Stop")) == 1
    assert "my-own-audit --log" in stop
    assert owned not in stop or owned == codex_installer.hook_command("Stop")


def test_two_sessions_in_different_projects_stay_isolated() -> None:
    a = codex_installer.event_provenance(_load("session_start_project_a.json"))
    b = codex_installer.event_provenance(_load("session_start_project_b.json"))
    assert a["session_id"] != b["session_id"]
    assert a["cwd"] != b["cwd"]
    assert a["project"] != b["project"]
    assert a["event"] == b["event"] == "SessionStart"
    assert a["event_hash"] != b["event_hash"]
    again = codex_installer.event_provenance(_load("session_start_project_a.json"))
    assert again["event_hash"] == a["event_hash"]


def test_provenance_hash_covers_turn_id() -> None:
    payload = _load("post_tool_use_project_a.json")
    base = codex_installer.event_provenance(payload)
    changed = dict(payload, turn_id="turn-other")
    assert codex_installer.event_provenance(changed)["event_hash"] != base["event_hash"]


def test_install_is_idempotent(codex_home: Path, tmp_path: Path) -> None:
    hooks_file = codex_home / "hooks.json"
    codex_installer.install_codex_hooks(hooks_file, tmp_path / "plane.db")
    first = hooks_file.read_text("utf-8")
    codex_installer.install_codex_hooks(hooks_file, tmp_path / "plane.db")
    assert hooks_file.read_text("utf-8") == first


def test_install_upgrades_legacy_tmp_hooks(codex_home: Path, tmp_path: Path) -> None:
    hooks_file = codex_home / "hooks.json"
    legacy = {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup|resume",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "verdict hook recall '$(cat /tmp/codex_session_prompt)'",
                        }
                    ],
                }
            ]
        }
    }
    hooks_file.write_text(json.dumps(legacy), "utf-8")
    codex_installer.install_codex_hooks(hooks_file, tmp_path / "plane.db")
    assert "/tmp/codex_" not in hooks_file.read_text("utf-8")


def test_install_preserves_user_hooks_and_config(codex_home: Path, tmp_path: Path) -> None:
    hooks_file = codex_home / "hooks.json"
    hooks_file.write_text(json.dumps({"hooks": {"PreToolUse": [USER_HOOK]}, "other": 1}), "utf-8")
    config = codex_home / "config.toml"
    config.write_text('model = "x"\n', "utf-8")
    codex_installer.install_codex_hooks(hooks_file, tmp_path / "plane.db")
    data = json.loads(hooks_file.read_text("utf-8"))
    assert USER_HOOK in data["hooks"]["PreToolUse"]
    assert data["other"] == 1
    assert config.read_text("utf-8") == 'model = "x"\n'


def test_uninstall_removes_only_verdict_entries(codex_home: Path, tmp_path: Path) -> None:
    hooks_file = codex_home / "hooks.json"
    hooks_file.write_text(json.dumps({"hooks": {"PreToolUse": [USER_HOOK]}}), "utf-8")
    codex_installer.install_codex_hooks(hooks_file, tmp_path / "plane.db")
    codex_installer.uninstall_codex_hooks(hooks_file)
    data = json.loads(hooks_file.read_text("utf-8"))
    assert _verdict_commands(data) == []
    assert data["hooks"]["PreToolUse"] == [USER_HOOK]


def _certify(codex_home: Path) -> harness_codex.CertifyReport:
    return harness_codex.certify(
        codex_home=codex_home, force=True, which=lambda _name: "/usr/bin/codex"
    )


def test_certify_does_not_claim_hooks_unsupported(codex_home: Path) -> None:
    report = _certify(codex_home)
    assert report.facets["hooks"] != "unsupported"


def test_certify_reports_per_event_levels(codex_home: Path) -> None:
    report = _certify(codex_home)
    levels = report.hook_events
    assert set(levels) == set(CODEX_EVENTS)
    # Only live-observed events may claim "supported".
    assert {e for e, lvl in levels.items() if lvl == "supported"} == LIVE_EVENTS
    assert set(levels.values()) <= {"supported", "partial", "unknown"}
    assert "Hook events:" in harness_codex.format_certify(report)


def _run_entrypoint(
    tmp_path: Path, event: str, payload: object
) -> tuple[subprocess.CompletedProcess[str], Path]:
    outbox = tmp_path / "outbox"
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "CODEX_HOME": str(tmp_path / "home" / ".codex"),
        codex_installer.OUTBOX_DIR_ENV: str(outbox),
    }
    proc = subprocess.run(
        [sys.executable, "-m", "verdict.memory_harness.codex", event],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=60,
        check=False,
    )
    return proc, outbox / codex_installer.OUTBOX_FILENAME


def test_entrypoint_captures_to_outbox_with_named_gap(tmp_path: Path) -> None:
    payload = _load("session_start_project_a.json")
    proc, outbox = _run_entrypoint(tmp_path, "SessionStart", payload)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
    records = [json.loads(line) for line in outbox.read_text("utf-8").splitlines()]
    assert len(records) == 1
    record = records[0]
    assert record["gap"] == codex_installer.NO_BACKEND_GAP
    assert record["session_id"] == payload["session_id"]
    assert record["event_hash"] == codex_installer.event_provenance(payload)["event_hash"]
    again, _ = _run_entrypoint(tmp_path, "SessionStart", payload)
    assert again.returncode == 0
    assert len(outbox.read_text("utf-8").splitlines()) == 1


def test_entrypoint_launch_gate_fails_closed(tmp_path: Path) -> None:
    payload = dict(_load("session_start_project_a.json"), hook_event_name="Stop")
    proc, outbox = _run_entrypoint(tmp_path, "SessionStart", payload)
    assert proc.returncode == codex_installer.EXIT_GATE_BLOCKED
    assert proc.stdout == ""
    assert "does not match" in proc.stderr
    assert not outbox.exists()


def test_entrypoint_rejects_payload_without_session(tmp_path: Path) -> None:
    proc, outbox = _run_entrypoint(tmp_path, "PreToolUse", {"hook_event_name": "PreToolUse"})
    assert proc.returncode == codex_installer.EXIT_GATE_BLOCKED
    assert "missing session_id" in proc.stderr
    assert not outbox.exists()


def _certify_levels() -> dict[str, str]:
    report = harness_codex.certify(force=True, which=lambda _name: "/usr/bin/codex")
    return dict(report.hook_events)
