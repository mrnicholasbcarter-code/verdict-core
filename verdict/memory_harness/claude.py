"""Claude Code memory-bridge installer.

Installs one stdin-driven hook per Claude lifecycle event. Each hook command is
:func:`verdict.harness_claude.hook_command` (the absolute, shell-quoted interpreter
that ran the install, ``-m verdict.memory_harness.claude <Event>``); Claude Code pipes
the hook JSON (session_id, cwd, prompt, ...) to the command on stdin, so no
global temp prompt files are read. Legacy ``/tmp/claude_*`` Verdict hooks are
migrated away on install. ``shared_db`` is accepted and unused.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from verdict.harness_claude import CLAUDE_HOOK_EVENT_KINDS, HOOK_MODULE, hook_command, main
from verdict.memory_harness._hook_ownership import is_legacy_tmp_prompt_hook, is_module_hook

# Provisional entry point ``<python> -m verdict.harness_claude hook <Event>`` shipped
# before the controller's 23:10Z decision; migrated away like the legacy /tmp hooks.
_PROVISIONAL_MODULE = "verdict.harness_claude"
_LEGACY_PROMPT_FILES = ("/tmp/claude_last_prompt", "/tmp/claude_session_prompt")  # nosec B108: detection-only comparison values for migrating away legacy hooks, never written


def _is_verdict_command(command: object) -> bool:
    """Exact parsed match only (see ``_hook_ownership``); everything else is a user hook."""
    return (
        is_module_hook(command, HOOK_MODULE, CLAUDE_HOOK_EVENT_KINDS)
        or is_module_hook(
            command, _PROVISIONAL_MODULE, CLAUDE_HOOK_EVENT_KINDS, subcommand=("hook",)
        )
        or is_legacy_tmp_prompt_hook(command, _LEGACY_PROMPT_FILES)
    )


def _load(settings_file: Path) -> dict[str, Any]:
    try:
        data = json.loads(settings_file.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _strip_verdict_hooks(settings: dict[str, Any]) -> bool:
    """Remove Verdict-managed hook commands in place. Return True if changed."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return False
    changed = False
    for event in list(hooks):
        matchers = hooks[event]
        if not isinstance(matchers, list):
            continue
        kept_matchers: list[Any] = []
        removed_here = False
        for matcher in matchers:
            inner = matcher.get("hooks") if isinstance(matcher, dict) else None
            if not isinstance(inner, list):
                kept_matchers.append(matcher)
                continue
            kept = [
                h
                for h in inner
                if not (isinstance(h, dict) and _is_verdict_command(h.get("command")))
            ]
            if len(kept) == len(inner):
                kept_matchers.append(matcher)
                continue
            removed_here = True
            if kept:
                kept_matchers.append({**matcher, "hooks": kept})
        if removed_here:
            changed = True
            if kept_matchers:
                hooks[event] = kept_matchers
            else:
                del hooks[event]
    if changed and not hooks:
        del settings["hooks"]
    return changed


def install_claude_hooks(settings_file: Path, shared_db: Path) -> None:
    """Install Verdict stdin hooks in Claude Code settings.json (idempotent)."""
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings = _load(settings_file) if settings_file.exists() else {}
    _strip_verdict_hooks(settings)
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = settings["hooks"] = {}
    for event in CLAUDE_HOOK_EVENT_KINDS:
        entry = {"matcher": "", "hooks": [{"type": "command", "command": hook_command(event)}]}
        existing = hooks.get(event)
        if isinstance(existing, list):
            existing.append(entry)
        else:
            hooks[event] = [entry]
    settings_file.write_text(json.dumps(settings, indent=2), encoding="utf-8")


def uninstall_claude_hooks(settings_file: Path) -> None:
    """Remove only Verdict-managed hooks; leave the file untouched if none."""
    if not settings_file.is_file():
        return
    settings = _load(settings_file)
    if _strip_verdict_hooks(settings):
        settings_file.write_text(json.dumps(settings, indent=2), encoding="utf-8")


def install_claude_instructions(instructions_file: Path, bridge_instruction: str) -> None:
    """Append the bridge instruction to CLAUDE.md when the marker is absent."""
    existing = instructions_file.read_text("utf-8") if instructions_file.exists() else ""
    if "Verdict Unified Memory Bridge" not in existing:
        instructions_file.write_text(existing + bridge_instruction, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
