"""RED tests for Codex 0.161.0 memory hooks (lane B).

Fixtures under tests/fixtures/codex/v0_161_0/ are SYNTHETIC: shaped like Codex
hook stdin JSON but not yet observed live. Installer import path is
``verdict.memory_harness.codex`` (moved out of memory_bridge by the E0 extract).
"""

from __future__ import annotations

import json
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
USER_HOOK = {"matcher": "Bash", "hooks": [{"type": "command", "command": "my-own-audit --log", "timeout": 5}]}


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


def test_fixtures_are_marked_synthetic() -> None:
    files = sorted(FIXTURES.glob("*.json"))
    assert files
    for f in files:
        payload = json.loads(f.read_text("utf-8"))
        assert payload["_synthetic"] is True, f.name
        for key in ("session_id", "cwd", "hook_event_name", "transcript_path", "turn_id"):
            assert key in payload, (f.name, key)


def test_installer_reads_stdin_not_tmp_files(codex_home: Path, tmp_path: Path) -> None:
    hooks_file = codex_home / "hooks.json"
    codex_installer.install_codex_hooks(hooks_file, tmp_path / "plane.db")
    commands = _verdict_commands(json.loads(hooks_file.read_text("utf-8")))
    assert commands
    for cmd in commands:
        assert "/tmp/codex_" not in cmd
        assert "$(cat" not in cmd
        assert "verdict hook codex-event" in cmd


def test_installer_registers_every_codex_event(codex_home: Path, tmp_path: Path) -> None:
    hooks_file = codex_home / "hooks.json"
    codex_installer.install_codex_hooks(hooks_file, tmp_path / "plane.db")
    data = json.loads(hooks_file.read_text("utf-8"))
    for event in CODEX_EVENTS:
        cmds = [h["command"] for g in data["hooks"].get(event, []) for h in g["hooks"]]
        assert any(f"verdict hook codex-event {event}" in c for c in cmds), event


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
                    "hooks": [{"type": "command", "command": "verdict hook recall '$(cat /tmp/codex_session_prompt)'"}],
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
    # Nothing has been observed live yet, so nothing may claim "supported".
    assert set(levels.values()) <= {"partial", "unknown"}
    assert "Hook events:" in harness_codex.format_certify(report)
