"""Codex memory-bridge installer and hook-event translator.

Codex 0.161.0 delivers each hook event as JSON on the hook command's stdin
(``session_id``, ``turn_id``, ``cwd``, ``hook_event_name``, ``transcript_path``).
Verdict hooks read that payload instead of the legacy ``/tmp/codex_*`` prompt
files, which leaked across concurrent sessions and projects.

``shared_db`` is accepted and unused; the hooks do not embed the plane path.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

CODEX_HOOK_EVENTS: tuple[str, ...] = (
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

HOOK_TIMEOUT_SECONDS = 10


def hook_command(event: str) -> str:
    """Return the shell command Codex runs for ``event``.

    Single switch point for the hook entry form; the payload arrives on stdin.
    """
    return f"verdict hook codex-event {event}"


def _verdict_hook(event: str) -> dict[str, Any]:
    return {
        "hooks": [
            {"type": "command", "command": hook_command(event), "timeout": HOOK_TIMEOUT_SECONDS}
        ]
    }


def _is_verdict_command(command: object) -> bool:
    if not isinstance(command, str):
        return False
    return command.startswith(hook_command("").rstrip()) or "verdict hook" in command


def _strip_verdict(groups: list[Any]) -> list[Any]:
    kept: list[Any] = []
    for group in groups:
        if not isinstance(group, dict):
            kept.append(group)
            continue
        hooks = group.get("hooks", [])
        if not isinstance(hooks, list):
            kept.append(group)
            continue
        remaining = [
            h for h in hooks if not (isinstance(h, dict) and _is_verdict_command(h.get("command")))
        ]
        if len(remaining) == len(hooks):
            kept.append(group)
        elif remaining:
            kept.append({**group, "hooks": remaining})
    return kept


def _load(hooks_file: Path) -> dict[str, Any]:
    if not hooks_file.exists():
        return {"hooks": {}}
    try:
        data = json.loads(hooks_file.read_text("utf-8"))
    except (OSError, ValueError):
        return {"hooks": {}}
    if not isinstance(data, dict):
        return {"hooks": {}}
    if not isinstance(data.get("hooks"), dict):
        data["hooks"] = {}
    return data


def _write(hooks_file: Path, data: Mapping[str, Any]) -> None:
    hooks_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = hooks_file.with_name(hooks_file.name + ".verdict-tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(hooks_file)


def install_codex_hooks(hooks_file: Path, shared_db: Path) -> None:
    """Install Verdict stdin hooks for every Codex event; idempotent.

    Legacy ``/tmp/codex_*`` Verdict entries are replaced; user hooks are kept.
    """
    del shared_db
    data = _load(hooks_file)
    hooks: dict[str, Any] = data["hooks"]
    for event in list(hooks):
        groups = hooks[event]
        if isinstance(groups, list):
            hooks[event] = _strip_verdict(groups)
    for event in CODEX_HOOK_EVENTS:
        groups = hooks.get(event)
        if not isinstance(groups, list):
            groups = []
        groups.append(_verdict_hook(event))
        hooks[event] = groups
    _write(hooks_file, data)


def uninstall_codex_hooks(hooks_file: Path) -> None:
    """Remove only Verdict-owned entries; user hooks and other keys stay."""
    if not hooks_file.exists():
        return
    data = _load(hooks_file)
    hooks: dict[str, Any] = data["hooks"]
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        stripped = _strip_verdict(groups)
        if stripped:
            hooks[event] = stripped
        else:
            del hooks[event]
    _write(hooks_file, data)


def _project_of(cwd: str) -> str:
    """Nearest ancestor with a project marker, else ``cwd`` itself."""
    if not cwd:
        return ""
    path = Path(cwd)
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists() or (candidate / ".codex").is_dir():
            return str(candidate)
    return str(path)


def event_provenance(payload: Mapping[str, Any]) -> dict[str, str]:
    """Translate one Codex hook stdin payload into Verdict provenance.

    Pure apart from a read-only project-marker lookup; never touches /tmp.
    """

    def _s(key: str) -> str:
        value = payload.get(key)
        return value if isinstance(value, str) else ""

    material = {k: v for k, v in payload.items() if not k.startswith("_")}
    digest = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
    cwd = _s("cwd")
    return {
        "session_id": _s("session_id"),
        "turn_id": _s("turn_id"),
        "cwd": cwd,
        "project": _project_of(cwd),
        "event": _s("hook_event_name"),
        "transcript_path": _s("transcript_path"),
        "event_hash": digest,
    }


def install_codex_instructions(codex_dir: Path, bridge_instruction: str) -> None:
    """Create the project Codex dir and append the bridge instruction once."""
    codex_dir.mkdir(parents=True, exist_ok=True)
    agents_md = codex_dir / "AGENTS.md"
    existing = agents_md.read_text("utf-8") if agents_md.exists() else ""
    if "Verdict Unified Memory Bridge" not in existing:
        agents_md.write_text(existing + bridge_instruction, encoding="utf-8")
