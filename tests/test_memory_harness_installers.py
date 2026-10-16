"""Characterization pins for the per-harness memory installers.

The assertions record current installer outputs, isolated-home files, idempotence,
collision handling, and uninstall. They were written against the unmoved
implementations and must keep passing after the move.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from verdict.memory_bridge import configure_memory_bridge, uninstall_memory_bridge
from verdict.memory_harness.claude import install_claude_hooks
from verdict.memory_harness.codex import install_codex_hooks
from verdict.memory_plane import MemoryPlane

BRIDGE = (
    "\n\n# Verdict Unified Memory Bridge\n"
    "- All sessions, context, and code graphs share one local-first MemoryPlane.\n"
    "- Query memory prior to task execution: `verdict memory search '<query>'`.\n"
    "- Export session records on completion: `verdict memory put <key> <content>`.\n"
)

# Claude hooks are stdin-driven (lane A): one command per lifecycle event, no
# /tmp prompt files. Legacy /tmp commands are kept only as migration inputs.
LEGACY_CLAUDE_PRE_BASH = (
    "verdict hook record 'claude_pre_bash' "
    "'$(cat /tmp/claude_last_prompt 2>/dev/null || echo \"bash command\")' "
    "--namespace sessions --source claude 2>/dev/null || true"
)
CLAUDE_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "SubagentStop",
    "PreCompact",
    "Stop",
    "SessionEnd",
)


def _claude_cmd(event: str) -> str:
    return f"python -m verdict.harness_claude hook {event}"


CODEX_POST = (
    "verdict hook record 'codex_session' "
    "'$(cat /tmp/codex_last_prompt 2>/dev/null || echo \"no prompt captured\")' "
    "--namespace sessions --source codex 2>/dev/null || true"
)
CODEX_RECALL = (
    "verdict hook recall '$(cat /tmp/codex_session_prompt 2>/dev/null || echo \"\")' "
    "--json --limit 20 2>/dev/null || true"
)


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Isolated home and cwd. Never the operator home or a configured plane path."""
    monkeypatch.delenv("VERDICT_MEMORY_PLANE_PATH", raising=False)
    monkeypatch.delenv("VERDICT_HOME", raising=False)
    home = tmp_path / "home"
    cwd = tmp_path / "repo"
    home.mkdir()
    cwd.mkdir()
    (home / "keep.txt").write_text("unrelated", encoding="utf-8")
    (cwd / "README.md").write_text("unrelated", encoding="utf-8")
    return home, cwd


def _plane(tmp_path: Path) -> MemoryPlane:
    return MemoryPlane(tmp_path / "injected-plane.db")


def _commands(matchers: list[dict[str, object]]) -> list[str]:
    found: list[str] = []
    for matcher in matchers:
        hooks = matcher.get("hooks", [])
        assert isinstance(hooks, list)
        for hook in hooks:
            assert isinstance(hook, dict)
            found.append(str(hook.get("command", "")))
    return found


def test_claude_hooks_write_canonical_commands_and_ignore_shared_db(tmp_path: Path) -> None:
    settings = tmp_path / "nested" / "settings.json"
    unused = tmp_path / "not-created" / "shared.db"

    install_claude_hooks(settings, unused)

    assert settings.is_file()
    assert not unused.exists()
    assert not unused.parent.exists()
    document = json.loads(settings.read_text(encoding="utf-8"))
    assert set(document) == {"hooks"}
    assert list(document["hooks"]) == list(CLAUDE_EVENTS)
    for event in CLAUDE_EVENTS:
        assert document["hooks"][event] == [
            {"matcher": "", "hooks": [{"type": "command", "command": _claude_cmd(event)}]}
        ]
    assert "/tmp/claude_" not in settings.read_text(encoding="utf-8")
    assert str(unused) not in settings.read_text(encoding="utf-8")


def test_claude_hooks_are_idempotent_and_keep_unrelated_settings(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    original = {
        "theme": "dark",
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {"type": "command", "command": "echo user"},
                        {"type": "command", "command": LEGACY_CLAUDE_PRE_BASH},
                    ],
                },
                {"matcher": "Read", "hooks": [{"type": "command", "command": "echo keep-read"}]},
            ],
            "Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "echo stop"}]}],
        },
    }
    settings.write_text(json.dumps(original), encoding="utf-8")

    install_claude_hooks(settings, tmp_path / "shared.db")
    first = settings.read_text(encoding="utf-8")
    install_claude_hooks(settings, tmp_path / "shared.db")
    second = settings.read_text(encoding="utf-8")

    assert first == second
    document = json.loads(second)
    assert document["theme"] == "dark"
    assert document["hooks"]["Stop"][0] == original["hooks"]["Stop"][0]
    assert _commands(document["hooks"]["Stop"]).count(_claude_cmd("Stop")) == 1
    pre = document["hooks"]["PreToolUse"]
    assert pre[0] == {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo user"}]}
    assert LEGACY_CLAUDE_PRE_BASH not in _commands(pre)
    assert _commands(pre).count(_claude_cmd("PreToolUse")) == 1
    assert pre[1] == {
        "matcher": "Read",
        "hooks": [{"type": "command", "command": "echo keep-read"}],
    }


def test_claude_hooks_replace_invalid_json(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text("{not json", encoding="utf-8")

    install_claude_hooks(settings, tmp_path / "shared.db")

    document = json.loads(settings.read_text(encoding="utf-8"))
    assert "PreToolUse" in document["hooks"]
    assert "not json" not in settings.read_text(encoding="utf-8")


def test_codex_hooks_write_canonical_commands_and_ignore_shared_db(tmp_path: Path) -> None:
    hooks = tmp_path / "nested" / "hooks.json"
    unused = tmp_path / "not-created" / "shared.db"

    install_codex_hooks(hooks, unused)

    assert hooks.is_file()
    assert not unused.exists()
    document = json.loads(hooks.read_text(encoding="utf-8"))
    assert document["hooks"]["PostToolUse"][0]["matcher"] == "Write|Edit|Bash"
    assert document["hooks"]["PostToolUse"][0]["hooks"][0]["timeout"] == 10
    assert _commands(document["hooks"]["PostToolUse"]) == [CODEX_POST]
    assert document["hooks"]["SessionStart"][0]["matcher"] == "startup|resume"
    assert document["hooks"]["SessionStart"][0]["hooks"][0]["timeout"] == 10
    assert _commands(document["hooks"]["SessionStart"]) == [CODEX_RECALL]
    assert str(unused) not in hooks.read_text(encoding="utf-8")


def test_codex_hooks_keep_user_entries_and_noncanonical_verdict_commands(tmp_path: Path) -> None:
    hooks = tmp_path / "hooks.json"
    quoted = CODEX_RECALL.replace("'", '"')
    original = {
        "version": 1,
        "hooks": {
            "PostToolUse": [
                {"matcher": "Custom", "hooks": [{"type": "command", "command": "echo user-codex"}]}
            ],
            "SessionStart": [
                {
                    "matcher": "startup|resume",
                    "hooks": [{"type": "command", "command": quoted, "timeout": 10}],
                }
            ],
        },
    }
    hooks.write_text(json.dumps(original), encoding="utf-8")

    install_codex_hooks(hooks, tmp_path / "shared.db")
    first = hooks.read_text(encoding="utf-8")
    install_codex_hooks(hooks, tmp_path / "shared.db")

    assert hooks.read_text(encoding="utf-8") == first
    document = json.loads(first)
    assert document["version"] == 1
    post = _commands(document["hooks"]["PostToolUse"])
    assert "echo user-codex" in post
    assert post.count(CODEX_POST) == 1
    session = _commands(document["hooks"]["SessionStart"])
    assert session.count(quoted) == 1
    assert CODEX_RECALL not in session


def test_codex_hooks_replace_invalid_json(tmp_path: Path) -> None:
    hooks = tmp_path / "hooks.json"
    hooks.write_text("{not json", encoding="utf-8")

    install_codex_hooks(hooks, tmp_path / "shared.db")

    document = json.loads(hooks.read_text(encoding="utf-8"))
    assert _commands(document["hooks"]["PostToolUse"]) == [CODEX_POST]
    assert "not json" not in hooks.read_text(encoding="utf-8")


def test_configure_writes_claude_and_codex_files_under_isolated_home(
    workspace: tuple[Path, Path], tmp_path: Path
) -> None:
    home, cwd = workspace
    (cwd / "CLAUDE.md").write_text("# Mine\n", encoding="utf-8")
    (cwd / ".codex").mkdir()
    (cwd / ".codex" / "AGENTS.md").write_text("user agents\n", encoding="utf-8")

    result = configure_memory_bridge(
        ["codex", "claude"], plane=_plane(tmp_path), home_dir=home, cwd=cwd
    )

    assert result["status"] == "success"
    assert result["configured_tools"] == ["codex", "claude"]
    assert result["synced_session_records"] == 0
    assert result["memory_db_path"] == str(home.resolve() / ".verdict" / "memory.db")
    assert (cwd / "CLAUDE.md").read_text(encoding="utf-8") == "# Mine\n" + BRIDGE
    assert (cwd / ".codex" / "AGENTS.md").read_text(encoding="utf-8") == "user agents\n" + BRIDGE
    claude_settings = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert _claude_cmd("PreToolUse") in _commands(claude_settings["hooks"]["PreToolUse"])
    codex_hooks = json.loads((home / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    assert CODEX_POST in _commands(codex_hooks["hooks"]["PostToolUse"])
    assert (home / "keep.txt").read_text(encoding="utf-8") == "unrelated"
    assert (cwd / "README.md").read_text(encoding="utf-8") == "unrelated"

    configure_memory_bridge(["codex", "claude"], plane=_plane(tmp_path), home_dir=home, cwd=cwd)
    assert (cwd / "CLAUDE.md").read_text(encoding="utf-8") == "# Mine\n" + BRIDGE
    assert (cwd / ".codex" / "AGENTS.md").read_text(encoding="utf-8") == "user agents\n" + BRIDGE
    assert (cwd / "CLAUDE.md").read_text(encoding="utf-8").count(
        "Verdict Unified Memory Bridge"
    ) == 1


def test_configure_skips_instruction_when_marker_already_present(
    workspace: tuple[Path, Path], tmp_path: Path
) -> None:
    home, cwd = workspace
    (cwd / "CLAUDE.md").write_text("already Verdict Unified Memory Bridge here\n", encoding="utf-8")

    configure_memory_bridge(["claude"], plane=_plane(tmp_path), home_dir=home, cwd=cwd)

    assert (cwd / "CLAUDE.md").read_text(encoding="utf-8") == (
        "already Verdict Unified Memory Bridge here\n"
    )


def test_configure_hermes_overwrites_bridge_file_and_skips_missing_dir(
    workspace: tuple[Path, Path], tmp_path: Path
) -> None:
    home, cwd = workspace

    missing = configure_memory_bridge(["hermes"], plane=_plane(tmp_path), home_dir=home, cwd=cwd)
    assert missing["configured_tools"] == ["hermes"]
    assert not (home / ".hermes").exists()

    hermes = home / ".hermes"
    hermes.mkdir()
    (hermes / "user.json").write_text("keep-me", encoding="utf-8")
    (hermes / "verdict_bridge.json").write_text('{"user": true}', encoding="utf-8")

    written = configure_memory_bridge(["hermes"], plane=_plane(tmp_path), home_dir=home, cwd=cwd)
    bridge = hermes / "verdict_bridge.json"
    expected = json.dumps({"memory_plane": written["memory_db_path"]})
    assert bridge.read_text(encoding="utf-8") == expected
    assert (hermes / "user.json").read_text(encoding="utf-8") == "keep-me"
    configure_memory_bridge(["hermes"], plane=_plane(tmp_path), home_dir=home, cwd=cwd)
    assert bridge.read_text(encoding="utf-8") == expected


def test_configure_prime_writes_nothing_and_pi_stays_separate(
    workspace: tuple[Path, Path], tmp_path: Path
) -> None:
    home, cwd = workspace
    prime = home / ".prime" / "agent"
    prime.mkdir(parents=True)
    models = prime / "models.json"
    models.write_text('{"user": true}', encoding="utf-8")

    prime_result = configure_memory_bridge(
        ["prime"], plane=_plane(tmp_path), home_dir=home, cwd=cwd
    )
    assert prime_result["configured_tools"] == []
    assert models.read_text(encoding="utf-8") == '{"user": true}'
    assert list(prime.iterdir()) == [models]

    pi_result = configure_memory_bridge(["pi"], plane=_plane(tmp_path), home_dir=home, cwd=cwd)
    assert pi_result["configured_tools"] == ["pi"]
    pi_cfg = json.loads((cwd / ".pi" / "memory_bridge.json").read_text(encoding="utf-8"))
    assert pi_cfg == {
        "enabled": True,
        "memory_plane": pi_result["memory_db_path"],
        "auto_sync": True,
    }
    assert not (home / ".prime" / "verdict_bridge.json").exists()


def test_configure_tolerates_a_malformed_session_file(
    workspace: tuple[Path, Path], tmp_path: Path
) -> None:
    home, cwd = workspace
    sessions = home / ".codex" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "bad.jsonl").write_text("{not json\n", encoding="utf-8")

    result = configure_memory_bridge(["codex"], plane=_plane(tmp_path), home_dir=home, cwd=cwd)

    assert result["status"] == "success"
    assert result["configured_tools"] == ["codex"]
    assert result["synced_session_records"] == 0


def test_uninstall_removes_only_its_targets_and_is_idempotent_without_purge(
    workspace: tuple[Path, Path], tmp_path: Path
) -> None:
    home, cwd = workspace
    configure_memory_bridge(
        ["codex", "claude", "hermes", "mcp"], plane=_plane(tmp_path), home_dir=home, cwd=cwd
    )
    hermes = home / ".hermes"
    hermes.mkdir()
    (hermes / "verdict_bridge.json").write_text('{"memory_plane": "x"}', encoding="utf-8")
    verdict_dir = home / ".verdict"
    verdict_dir.mkdir(exist_ok=True)
    (verdict_dir / "keep.dat").write_text("keep", encoding="utf-8")
    settings = home / ".claude" / "settings.json"
    hooks = home / ".codex" / "hooks.json"
    claude_md = cwd / "CLAUDE.md"
    settings_before = settings.read_bytes()
    hooks_before = hooks.read_bytes()
    claude_before = claude_md.read_bytes()

    first = uninstall_memory_bridge(home, cwd, purge_data=False)

    assert first == {
        "status": "success",
        "uninstalled_targets": [".mcp.json", ".claude/settings.json (hooks)"],
    }
    assert not (cwd / ".mcp.json").exists()
    assert settings.read_bytes() == settings_before
    assert hooks.read_bytes() == hooks_before
    assert claude_before == claude_md.read_bytes()
    assert (hermes / "verdict_bridge.json").read_text(encoding="utf-8") == '{"memory_plane": "x"}'
    assert (verdict_dir / "keep.dat").read_text(encoding="utf-8") == "keep"
    assert (home / "keep.txt").read_text(encoding="utf-8") == "unrelated"

    second = uninstall_memory_bridge(home, cwd, purge_data=False)
    assert second["uninstalled_targets"] == [".claude/settings.json (hooks)"]
    assert settings.read_bytes() == settings_before


def test_uninstall_purge_removes_verdict_dir_and_leaves_unrelated_files(
    workspace: tuple[Path, Path],
) -> None:
    home, cwd = workspace
    verdict_dir = home / ".verdict"
    verdict_dir.mkdir()
    (verdict_dir / "memory.db").write_text("db", encoding="utf-8")
    (cwd / ".mcp.json").write_text("{}", encoding="utf-8")

    result = uninstall_memory_bridge(home, cwd, purge_data=True)

    assert result["uninstalled_targets"] == [".verdict", ".mcp.json"]
    assert not verdict_dir.exists()
    assert (home / "keep.txt").read_text(encoding="utf-8") == "unrelated"
    assert (cwd / "README.md").read_text(encoding="utf-8") == "unrelated"
