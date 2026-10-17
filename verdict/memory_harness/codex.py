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
import os
import shlex
import sys
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
    The interpreter is the absolute, shell-quoted ``sys.executable`` that ran
    the install, so the hook still finds verdict on pipx and uv installs where
    a bare ``python`` on PATH would not have it.
    """
    return f"{shlex.quote(sys.executable)} {_HOOK_MODULE_ARGS} {event}"


_HOOK_MODULE_ARGS = "-m verdict.memory_harness.codex"


def _verdict_hook(event: str) -> dict[str, Any]:
    return {
        "hooks": [
            {"type": "command", "command": hook_command(event), "timeout": HOOK_TIMEOUT_SECONDS}
        ]
    }


def _is_verdict_command(command: object) -> bool:
    if not isinstance(command, str):
        return False
    # Match any interpreter path (including the legacy bare ``python``), so a
    # reinstall from another environment replaces rather than duplicates hooks.
    return f" {_HOOK_MODULE_ARGS} " in f" {command} " or "verdict hook" in command


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


# ---------------------------------------------------------------------------
# Hook entrypoint: ``python -m verdict.memory_harness.codex <Event>``
# ---------------------------------------------------------------------------

OUTBOX_DIR_ENV = "VERDICT_CODEX_OUTBOX_DIR"
OUTBOX_FILENAME = "codex-lifecycle.jsonl"
NO_BACKEND_GAP = "no-backend: captured to local outbox only"
EXIT_GATE_BLOCKED = 2


class LaunchGateError(Exception):
    """The Verdict launch gate rejected this hook invocation (fail closed)."""


def default_outbox_dir() -> Path:
    configured = os.environ.get(OUTBOX_DIR_ENV)
    if configured:
        return Path(configured)
    return Path.home() / ".verdict" / "outbox" / "codex"


def launch_gate(event: str, payload: Mapping[str, Any]) -> None:
    """Reject invocations Verdict cannot attribute; raises LaunchGateError."""
    if event not in CODEX_HOOK_EVENTS:
        raise LaunchGateError(f"unknown Codex hook event {event!r}")
    declared = payload.get("hook_event_name")
    if declared is not None and declared != event:
        raise LaunchGateError(f"payload hook_event_name {declared!r} does not match {event!r}")
    for key in ("session_id", "cwd"):
        if not isinstance(payload.get(key), str) or not payload.get(key):
            raise LaunchGateError(f"payload missing {key}")


def run_hook(event: str, stdin_text: str, *, outbox_dir: Path | None = None) -> int:
    """Gate, translate and capture one Codex hook payload.

    Graceful path: there is no backend client yet, so the record carries a named
    gap and goes to the local outbox (deduplicated by event_hash), exit 0.
    Gate rejections and capture failures fail closed: stderr reason, nonzero.
    Nothing is ever written to stdout (Codex may parse it).
    """
    try:
        loaded = json.loads(stdin_text) if stdin_text.strip() else None
        if not isinstance(loaded, dict):
            raise LaunchGateError("stdin is not a JSON object")
        launch_gate(event, loaded)
    except (LaunchGateError, ValueError) as exc:
        print(f"verdict codex hook blocked: {exc}", file=sys.stderr)
        return EXIT_GATE_BLOCKED
    record = {**event_provenance(loaded), "event": event, "gap": NO_BACKEND_GAP}
    try:
        target = (outbox_dir or default_outbox_dir()) / OUTBOX_FILENAME
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            for line in target.read_text("utf-8").splitlines():
                if f'"event_hash": "{record["event_hash"]}"' in line:
                    return 0
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError as exc:
        print(f"verdict codex hook capture failed: {exc}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m verdict.memory_harness.codex <Event>", file=sys.stderr)
        return EXIT_GATE_BLOCKED
    return run_hook(args[0], sys.stdin.read())


if __name__ == "__main__":
    raise SystemExit(main())
