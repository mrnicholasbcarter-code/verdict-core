"""Claude Code memory-bridge installer.

Behavior matches the former ``verdict.memory_bridge._install_claude_hooks``.
``shared_db`` is accepted and unused; the hooks do not embed the plane path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from verdict.memory_harness._normalize import _normalize_hook_command


def install_claude_hooks(settings_file: Path, shared_db: Path) -> None:
    """Install Verdict hooks in Claude Code settings.json."""
    import json as _json

    settings_file.parent.mkdir(parents=True, exist_ok=True)

    # Load existing settings or create new
    if settings_file.exists():
        try:
            settings = _json.loads(settings_file.read_text("utf-8"))
        except Exception:
            settings = {}
    else:
        settings = {}

    # Ensure hooks structure exists
    if "hooks" not in settings:
        settings["hooks"] = {}

    # Define the Verdict hooks we need (canonical form with single quotes)
    verdict_hooks: dict[str, list[dict[str, Any]]] = {
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [
                    {
                        "type": "command",
                        "command": "verdict hook record 'claude_pre_bash' '$(cat /tmp/claude_last_prompt 2>/dev/null || echo \"bash command\")' --namespace sessions --source claude 2>/dev/null || true",
                    }
                ],
            }
        ],
        "PostToolUse": [
            {
                "matcher": "",
                "hooks": [
                    {
                        "type": "command",
                        "command": "verdict hook record 'claude_session' '$(cat /tmp/claude_last_prompt 2>/dev/null || echo \"no prompt captured\")' --namespace sessions --source claude 2>/dev/null || true",
                    }
                ],
            }
        ],
        "UserPromptSubmit": [
            {
                "matcher": "",
                "hooks": [
                    {
                        "type": "command",
                        "command": "verdict hook recall '$(cat /tmp/claude_last_prompt 2>/dev/null || echo \"\")' --json --limit 20 2>/dev/null || true",
                    }
                ],
            }
        ],
        "SessionStart": [
            {
                "matcher": "compact",
                "hooks": [
                    {
                        "type": "command",
                        "command": "verdict hook recall '$(cat /tmp/claude_session_prompt 2>/dev/null || echo \"\")' --json --limit 30 2>/dev/null || true",
                    }
                ],
            },
            {
                "matcher": "startup|resume",
                "hooks": [
                    {
                        "type": "command",
                        "command": "verdict hook recall '$(cat /tmp/claude_session_prompt 2>/dev/null || echo \"\")' --json --limit 30 2>/dev/null || true",
                    }
                ],
            },
        ],
        "SubagentStop": [
            {
                "matcher": "",
                "hooks": [
                    {
                        "type": "command",
                        "command": "verdict hook record 'claude_subagent' '$(cat /tmp/claude_last_prompt 2>/dev/null || echo \"subagent completed\")' --namespace sessions --source claude 2>/dev/null || true",
                    }
                ],
            }
        ],
    }

    # First, CLEAN UP any existing verdict hooks with non-canonical quote styles
    for hook_type in list(settings["hooks"].keys()):
        if hook_type not in verdict_hooks:
            continue
        new_hooks_list = []
        seen_normalized = set()  # Track across ALL matcher objects for this hook_type
        for matcher_obj in settings["hooks"][hook_type]:
            matcher = matcher_obj.get("matcher", "")
            # Only process matchers that we manage
            if not any(vh.get("matcher") == matcher for vh in verdict_hooks.get(hook_type, [])):
                new_hooks_list.append(matcher_obj)
                continue

            new_hooks = []
            for hook in matcher_obj.get("hooks", []):
                cmd = hook.get("command", "")
                if "verdict hook" in cmd:
                    norm = _normalize_hook_command(cmd)
                    if norm not in seen_normalized:
                        seen_normalized.add(norm)
                        # Only keep if it matches our canonical form (single quotes)
                        if cmd == _normalize_hook_command(cmd):
                            new_hooks.append(hook)
                else:
                    new_hooks.append(hook)

            if new_hooks:
                matcher_obj["hooks"] = new_hooks
                new_hooks_list.append(matcher_obj)

        settings["hooks"][hook_type] = new_hooks_list

    # Now ADD canonical hooks if missing
    for hook_type, hooks in verdict_hooks.items():
        if hook_type not in settings["hooks"]:
            settings["hooks"][hook_type] = []

        # Track what we already have (normalized) for deduplication
        existing_commands = set()
        existing_matchers = set()
        for h in settings["hooks"][hook_type]:
            existing_matchers.add(h.get("matcher", ""))
            for hook in h.get("hooks", []):
                if "verdict hook" in hook.get("command", ""):
                    existing_commands.add(_normalize_hook_command(hook["command"]))

        for verdict_hook in hooks:
            matcher = verdict_hook.get("matcher", "")
            # If this matcher doesn't exist at all, add the whole hook object
            if matcher not in existing_matchers:
                settings["hooks"][hook_type].append(verdict_hook)
                existing_matchers.add(matcher)
                # Also add commands to existing_commands for deduplication
                for vh in verdict_hook["hooks"]:
                    existing_commands.add(_normalize_hook_command(vh["command"]))
            else:
                # Matcher exists, add individual commands if missing
                for vh in verdict_hook["hooks"]:
                    norm_cmd = _normalize_hook_command(vh["command"])
                    if norm_cmd not in existing_commands:
                        # Find the matching matcher_obj and append
                        for h in settings["hooks"][hook_type]:
                            if h.get("matcher") == matcher:
                                h["hooks"].append(vh)
                                break
                        existing_commands.add(norm_cmd)

    # Write back
    settings_file.write_text(_json.dumps(settings, indent=2), encoding="utf-8")


def install_claude_instructions(instructions_file: Path, bridge_instruction: str) -> None:
    """Append the bridge instruction to CLAUDE.md when the marker is absent."""
    existing = instructions_file.read_text("utf-8") if instructions_file.exists() else ""
    if "Verdict Unified Memory Bridge" not in existing:
        instructions_file.write_text(existing + bridge_instruction, encoding="utf-8")
