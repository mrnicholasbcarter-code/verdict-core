"""Codex memory-bridge installer.

Behavior matches the former ``verdict.memory_bridge._install_codex_hooks``.
``shared_db`` is accepted and unused; the hooks do not embed the plane path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from verdict.memory_harness._normalize import _normalize_hook_command


def install_codex_hooks(hooks_file: Path, shared_db: Path) -> None:
    """Install Verdict hooks in Codex hooks.json."""
    import json as _json

    hooks_file.parent.mkdir(parents=True, exist_ok=True)

    # Load existing hooks or create new
    if hooks_file.exists():
        try:
            hooks_data = _json.loads(hooks_file.read_text("utf-8"))
        except Exception:
            hooks_data = {"hooks": {}}
    else:
        hooks_data = {"hooks": {}}

    if "hooks" not in hooks_data:
        hooks_data["hooks"] = {}

    # Define Verdict hooks for Codex
    codex_verdict_hooks: dict[str, list[dict[str, Any]]] = {
        "PostToolUse": [
            {
                "matcher": "Write|Edit|Bash",
                "hooks": [
                    {
                        "type": "command",
                        "command": "verdict hook record 'codex_session' '$(cat /tmp/codex_last_prompt 2>/dev/null || echo \"no prompt captured\")' --namespace sessions --source codex 2>/dev/null || true",
                        "timeout": 10,
                    }
                ],
            }
        ],
        "SessionStart": [
            {
                "matcher": "startup|resume",
                "hooks": [
                    {
                        "type": "command",
                        "command": "verdict hook recall '$(cat /tmp/codex_session_prompt 2>/dev/null || echo \"\")' --json --limit 20 2>/dev/null || true",
                        "timeout": 10,
                    }
                ],
            }
        ],
    }

    # Merge hooks
    for hook_type, hooks in codex_verdict_hooks.items():
        if hook_type not in hooks_data["hooks"]:
            hooks_data["hooks"][hook_type] = []

        existing_commands = set()
        for h in hooks_data["hooks"][hook_type]:
            for hook in h.get("hooks", []):
                if "verdict hook" in hook.get("command", ""):
                    existing_commands.add(_normalize_hook_command(hook["command"]))

        for verdict_hook in hooks:
            for vh in verdict_hook["hooks"]:
                norm_cmd = _normalize_hook_command(vh["command"])
                if norm_cmd not in existing_commands:
                    hooks_data["hooks"][hook_type].append(verdict_hook)
                    existing_commands.add(norm_cmd)

    hooks_file.write_text(_json.dumps(hooks_data, indent=2), encoding="utf-8")


def install_codex_instructions(codex_dir: Path, bridge_instruction: str) -> None:
    """Create the project Codex dir and append the bridge instruction once."""
    codex_dir.mkdir(parents=True, exist_ok=True)
    agents_md = codex_dir / "AGENTS.md"
    existing = agents_md.read_text("utf-8") if agents_md.exists() else ""
    if "Verdict Unified Memory Bridge" not in existing:
        agents_md.write_text(existing + bridge_instruction, encoding="utf-8")
