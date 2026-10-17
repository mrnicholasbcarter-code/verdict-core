"""Architect-locked Claude Code harness switching for Verdict.

CLI surface (do not rename):

    verdict harness claude discover
    verdict harness claude enable   [--base-url ...] [--token-env ...] [--force]
    verdict harness claude disable
    verdict harness claude status
    verdict harness claude certify  [--force]

``enable`` backs up ``~/.claude/settings.json`` before upserting Verdict-managed
``env.OPENAI_BASE_URL`` (OpenAI-compatible path at Verdict ``:8000``, never
OmniRoute ``:20128``) and ensuring a SessionStart ``verdict hook claude-gate``
entry. ``disable`` restores the backup byte-for-byte.

Claude Code's default wire format is Anthropic Messages (``/v1/messages``).
Verdict's proxy today is OpenAI-compatible only, so full Anthropic routing via
``ANTHROPIC_BASE_URL`` remains a known gap. This adapter therefore
certifies as **partial**: gate + OpenAI side-path are managed; Messages proxy
is unsupported until Core grows that surface.

Live enable that writes secrets is NEEDS_OWNER — this module never stores token
values; status/certify only report whether the named env var is set.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import sys
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from verdict.harness_codex import DEFAULT_BASE_URL, DEFAULT_TOKEN_ENV, HealthCheck, probe_health

BACKUP_NAME = "settings.json.verdict.bak"
CLAUDE_GATE_ARGS = ["hook", "claude-gate"]
Parity = Literal["supported", "partial", "unsupported"]


class HarnessClaudeError(Exception):
    """Raised when Claude Code harness enable/disable cannot proceed."""


@dataclass(frozen=True)
class ClaudeHarnessPaths:
    config: Path
    backup: Path

    @property
    def absent_marker(self) -> Path:
        return Path(str(self.backup) + ".absent")


@dataclass(frozen=True)
class EnableResult:
    config_path: Path
    backup_path: Path
    base_url: str
    token_env: str
    created_backup: bool
    forced: bool
    integration: str = "openai-compatible"


@dataclass(frozen=True)
class StatusReport:
    enabled: bool
    provider: str | None
    base_url: str | None
    token_env: str
    token_env_set: bool
    config_path: Path
    config_exists: bool
    gate_hook_present: bool = False
    integration: str | None = None


@dataclass(frozen=True)
class DiscoverReport:
    installed: bool
    binary_path: str | None
    config_path: Path
    config_exists: bool
    managed_by_verdict: bool
    base_url: str | None
    pointing_at_verdict: bool
    pointing_at_omniroute: bool
    gate_hook_present: bool


@dataclass(frozen=True)
class CertifyReport:
    harness: str
    overall: Parity
    facets: Mapping[str, Parity]
    healthy: bool
    token_env_set: bool
    base_url: str | None
    notes: tuple[str, ...]
    needs_owner: tuple[str, ...]


def resolve_paths(
    *, claude_home: Path | None = None, home: Path | None = None
) -> ClaudeHarnessPaths:
    if claude_home is not None:
        root = Path(claude_home).expanduser()
    else:
        env_home = os.getenv("CLAUDE_CONFIG_DIR") or os.getenv("CLAUDE_HOME")
        if env_home and env_home.strip():
            root = Path(env_home.strip()).expanduser()
        else:
            root = (home or Path.home()) / ".claude"
    config = root / "settings.json"
    return ClaudeHarnessPaths(config=config, backup=config.parent / BACKUP_NAME)


def discover(
    *, claude_home: Path | None = None, which: Callable[[str], str | None] | None = None
) -> DiscoverReport:
    """Observe Claude Code install/config without mutating anything."""

    paths = resolve_paths(claude_home=claude_home)
    finder = which or shutil.which
    binary = finder("claude")
    exists = paths.config.is_file()
    base_url: str | None = None
    gate = False
    managed = paths.backup.is_file() or paths.absent_marker.exists()
    if exists:
        data = _load_settings(paths.config)
        env = data.get("env") if isinstance(data.get("env"), Mapping) else {}
        raw = env.get("OPENAI_BASE_URL") if isinstance(env, Mapping) else None
        if isinstance(raw, str) and raw.strip():
            base_url = raw.strip()
        gate = _gate_hook_present(data)
        if base_url and "8000" in base_url and _gate_hook_present(data):
            managed = True
    return DiscoverReport(
        installed=binary is not None,
        binary_path=binary,
        config_path=paths.config,
        config_exists=exists,
        managed_by_verdict=managed,
        base_url=base_url,
        pointing_at_verdict=bool(base_url and "8000" in base_url and "20128" not in base_url),
        pointing_at_omniroute=bool(base_url and "20128" in base_url),
        gate_hook_present=gate,
    )


def enable(
    *,
    base_url: str = DEFAULT_BASE_URL,
    token_env: str = DEFAULT_TOKEN_ENV,
    force: bool = False,
    claude_home: Path | None = None,
    health_check: HealthCheck | Callable[[str], bool] | None = None,
) -> EnableResult:
    """Backup Claude settings, then point OpenAI-compatible traffic at Verdict."""

    resolved_base = base_url.strip() or DEFAULT_BASE_URL
    resolved_token_env = token_env.strip() or DEFAULT_TOKEN_ENV
    if "20128" in resolved_base and not force:
        raise HarnessClaudeError(
            f"refusing to enable Claude Code against OmniRoute-looking base_url={resolved_base}; "
            "Verdict-managed mode targets Verdict :8000"
        )
    if not force:
        healthy = (
            health_check(resolved_base)
            if health_check is not None
            else probe_health(base_url=resolved_base)
        )
        if not healthy:
            raise HarnessClaudeError(
                f"Verdict health check failed for {resolved_base}; "
                "refusing to enable Claude Code harness. Pass --force to override."
            )

    paths = resolve_paths(claude_home=claude_home)
    paths.config.parent.mkdir(parents=True, exist_ok=True)
    existed = paths.config.is_file()
    created_backup = False
    if existed and not paths.backup.exists():
        shutil.copy2(paths.config, paths.backup)
        created_backup = True
    elif not existed and not paths.backup.exists() and not paths.absent_marker.exists():
        paths.absent_marker.write_text("absent\n", encoding="utf-8")

    original_text = paths.config.read_text(encoding="utf-8") if existed else ""
    data: MutableMapping[str, Any]
    if original_text.strip():
        loaded = json.loads(original_text)
        if not isinstance(loaded, dict):
            raise HarnessClaudeError("Claude settings.json must be a JSON object")
        data = dict(loaded)
    else:
        data = {}

    updated = apply_verdict_provider(data, base_url=resolved_base, token_env=resolved_token_env)
    _atomic_write_json(paths.config, updated)
    return EnableResult(
        config_path=paths.config,
        backup_path=paths.backup,
        base_url=resolved_base,
        token_env=resolved_token_env,
        created_backup=created_backup,
        forced=force,
        integration="openai-compatible",
    )


def disable(*, claude_home: Path | None = None) -> None:
    """Restore the pre-enable Claude settings backup exactly."""

    paths = resolve_paths(claude_home=claude_home)
    if paths.absent_marker.exists():
        if paths.config.exists():
            paths.config.unlink()
        paths.absent_marker.unlink()
        if paths.backup.exists():
            paths.backup.unlink()
        return
    if not paths.backup.is_file():
        raise HarnessClaudeError(
            f"Verdict Claude Code harness is not enabled: no backup at {paths.backup}"
        )
    shutil.copy2(paths.backup, paths.config)
    paths.backup.unlink()


def status(*, claude_home: Path | None = None) -> StatusReport:
    """Return active OpenAI base URL and whether the token env is set."""

    paths = resolve_paths(claude_home=claude_home)
    base_url: str | None = None
    token_env = DEFAULT_TOKEN_ENV
    gate = False
    integration: str | None = None
    exists = paths.config.is_file()
    if exists:
        data = _load_settings(paths.config)
        env = data.get("env") if isinstance(data.get("env"), Mapping) else {}
        if isinstance(env, Mapping):
            raw = env.get("OPENAI_BASE_URL")
            if isinstance(raw, str) and raw.strip():
                base_url = raw.strip()
            marker = env.get("VERDICT_HARNESS_TOKEN_ENV")
            if isinstance(marker, str) and marker.strip():
                token_env = marker.strip()
            if env.get("VERDICT_HARNESS") == "claude":
                integration = "openai-compatible"
        gate = _gate_hook_present(data)
    enabled = bool(
        base_url
        and "8000" in base_url
        and "20128" not in base_url
        and gate
        and (
            paths.backup.exists()
            or paths.absent_marker.exists()
            or integration == "openai-compatible"
        )
    )
    return StatusReport(
        enabled=enabled,
        provider="verdict" if enabled else None,
        base_url=base_url,
        token_env=token_env,
        token_env_set=bool(os.getenv(token_env) or os.getenv("OPENAI_API_KEY")),
        config_path=paths.config,
        config_exists=exists,
        gate_hook_present=gate,
        integration=integration,
    )


def certify(
    *,
    claude_home: Path | None = None,
    force: bool = False,
    health_check: HealthCheck | Callable[[str], bool] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> CertifyReport:
    """Evidence-only certification. Never prints secrets. Live token use is NEEDS_OWNER."""

    discovered = discover(claude_home=claude_home, which=which)
    report = status(claude_home=claude_home)
    base = report.base_url or DEFAULT_BASE_URL
    healthy = False
    if force:
        healthy = True
    else:
        healthy = health_check(base) if health_check is not None else probe_health(base_url=base)

    facets: dict[str, Parity] = {
        "hooks": "supported" if report.gate_hook_present else "unsupported",
        "model_selection": "partial",  # OpenAI side-path only
        "config_import": "supported"
        if report.config_exists or discovered.managed_by_verdict
        else "partial",
        "mcp": "partial",
        "subagents": "unsupported",
        "resume": "partial",
        "tool_interception": "partial" if report.gate_hook_present else "unsupported",
        "structured_output": "unsupported",
        # Lifecycle matrix facets (BOD-80 parity)
        "session_start": "partial",  # file:memory_bridge._install_claude_hooks SessionStart — local plane only
        "before_first_turn": "partial",  # file:memory_bridge._install_claude_hooks UserPromptSubmit recall — local plane
        "tool_pre_post": "unsupported",  # no native tool pre/post hooks in Claude adapter
        "edit_event": "partial"
        if report.gate_hook_present
        else "unsupported",  # file:memory_bridge._install_claude_hooks
        "compaction_yield": "partial"
        if report.gate_hook_present
        else "unsupported",  # file:memory_bridge._install_claude_hooks
        "verification_result": "unsupported",  # no verification hook wired in Claude adapter
        "session_end": "unsupported",  # no SessionEnd hook in Claude adapter
        "sync_async_semantics": "partial",  # shell command hooks are sync; outbox mirror async
        "mutation_capability": "partial",  # file:memory_bridge.MemoryGate.write via hook commands
    }
    notes: list[str] = [
        "OpenAI-compatible path via env.OPENAI_BASE_URL → Verdict :8000",
        "Anthropic Messages (/v1/messages) via ANTHROPIC_BASE_URL is unsupported until BOD-102",
        "SessionStart claude-gate provides fail-closed catalog admission",
    ]
    live_events = [e for e, lvl in CLAUDE_HOOK_EVENT_CAPABILITY.items() if lvl == "supported"]
    notes.append("hook events observed live (Claude Code 2.1.300): " + ", ".join(live_events))
    needs_owner = (
        "live enable with secrets (token value never written; operator must export token_env)",
    )
    if not discovered.installed:
        overall: Parity = "unsupported"
        notes.append("claude binary not found on PATH")
    elif report.enabled and healthy:
        overall = "partial"
    elif report.enabled:
        overall = "partial"
        notes.append("Verdict health probe failed; pass --force to ignore for local proof")
    else:
        overall = "unsupported"
        notes.append("harness not enabled for Verdict-managed OpenAI path")

    return CertifyReport(
        harness="claude",
        overall=overall,
        facets=facets,
        healthy=healthy,
        token_env_set=report.token_env_set,
        base_url=report.base_url,
        notes=tuple(notes),
        needs_owner=needs_owner,
    )


def format_status(report: StatusReport) -> str:
    if report.enabled:
        state = "enabled"
    elif report.config_exists:
        state = "configured"
    else:
        state = "not configured"
    token_state = "yes" if report.token_env_set else "no"
    gate_state = "yes" if report.gate_hook_present else "no"
    return (
        f"Claude Code harness: {state}\n"
        f"  provider: {report.provider or '(none)'}\n"
        f"  base_url: {report.base_url or '(none)'}\n"
        f"  integration: {report.integration or '(none)'}\n"
        f"  gate_hook: {gate_state}\n"
        f"  token_env: {report.token_env} (set: {token_state})\n"
    )


def format_discover(report: DiscoverReport) -> str:
    return (
        f"Claude Code discover:\n"
        f"  installed: {'yes' if report.installed else 'no'}\n"
        f"  binary: {report.binary_path or '(none)'}\n"
        f"  config: {report.config_path} (exists: {'yes' if report.config_exists else 'no'})\n"
        f"  managed_by_verdict: {'yes' if report.managed_by_verdict else 'no'}\n"
        f"  base_url: {report.base_url or '(none)'}\n"
        f"  pointing_at_verdict: {'yes' if report.pointing_at_verdict else 'no'}\n"
        f"  pointing_at_omniroute: {'yes' if report.pointing_at_omniroute else 'no'}\n"
        f"  gate_hook: {'yes' if report.gate_hook_present else 'no'}\n"
    )


def format_certify(report: CertifyReport) -> str:
    facet_lines = "\n".join(f"  {name}: {level}" for name, level in sorted(report.facets.items()))
    notes = "\n".join(f"  - {note}" for note in report.notes)
    owner = "\n".join(f"  - {item}" for item in report.needs_owner)
    return (
        f"Claude Code certify: {report.overall}\n"
        f"  healthy: {'yes' if report.healthy else 'no'}\n"
        f"  base_url: {report.base_url or '(none)'}\n"
        f"  token_env_set: {'yes' if report.token_env_set else 'no'}\n"
        f"facets:\n{facet_lines}\n"
        f"notes:\n{notes}\n"
        f"needs_owner:\n{owner}\n"
    )


def apply_verdict_provider(
    data: MutableMapping[str, Any], *, base_url: str, token_env: str
) -> dict[str, Any]:
    """Upsert Verdict OpenAI-compatible env + SessionStart claude-gate hook.

    Preference: existing Verdict-managed settings.env markers → OpenAI-compatible
    env keys. Never writes token values. Does not set ANTHROPIC_BASE_URL (Messages
    proxy gap).
    """

    result = dict(data)
    env = dict(result.get("env") or {})
    env["OPENAI_BASE_URL"] = base_url
    env["VERDICT_HARNESS"] = "claude"
    env["VERDICT_HARNESS_TOKEN_ENV"] = token_env
    # Point OpenAI API key env name indirectly — Claude expands process env.
    # Do not embed the secret; operators export token_env / OPENAI_API_KEY.
    result["env"] = env
    result["hooks"] = _ensure_claude_gate(result.get("hooks"))
    return result


def _ensure_claude_gate(hooks: Any) -> dict[str, Any]:
    root: dict[str, Any] = dict(hooks) if isinstance(hooks, Mapping) else {}
    session = list(root.get("SessionStart") or [])
    if not _session_has_gate(session):
        session.append(
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "verdict",
                        "args": list(CLAUDE_GATE_ARGS),
                        "timeout": 70,
                    }
                ]
            }
        )
    root["SessionStart"] = session
    return root


def _session_has_gate(session: list[Any]) -> bool:
    for entry in session:
        if not isinstance(entry, Mapping):
            continue
        for hook in entry.get("hooks") or []:
            if not isinstance(hook, Mapping):
                continue
            cmd = hook.get("command")
            args = hook.get("args")
            if cmd == "verdict" and isinstance(args, list) and list(args) == CLAUDE_GATE_ARGS:
                return True
            if isinstance(cmd, str) and "claude-gate" in cmd:
                return True
    return False


def _gate_hook_present(data: Mapping[str, Any]) -> bool:
    hooks = data.get("hooks")
    if not isinstance(hooks, Mapping):
        return False
    session = hooks.get("SessionStart")
    if not isinstance(session, list):
        return False
    return _session_has_gate(session)


def _load_settings(path: Path) -> dict[str, Any]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(loaded, dict):
        return dict(loaded)
    return {}


def _atomic_write_json(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(dict(data), indent=2, sort_keys=False) + "\n", encoding="utf-8")
    tmp.replace(path)


# --- Hook entry point ---------------------------------------------------------

# Single switch point for the installed hook command (controller 2026-10-16 23:10Z):
# the E0 module entry point ``<python> -m verdict.memory_harness.claude <Event>``.
# ``{python}`` is the shell-quoted absolute interpreter that ran the install, so the
# hook still imports verdict on pipx/uv installs where a bare ``python`` cannot.
HOOK_MODULE = "verdict.memory_harness.claude"
HOOK_COMMAND_TEMPLATE = "{python} -m " + HOOK_MODULE + " {event}"


def hook_command(event: str) -> str:
    """Return the installed hook command for ``event`` using ``sys.executable``."""
    # sys.executable can be empty or None (embedded or frozen interpreters), and
    # shlex.quote("") would write an unrunnable ''. The bare-python fallback is
    # the pre-BOD-422 form: no worse than before, and still matched by the
    # ownership predicate, so a later reinstall from a real interpreter replaces it.
    return HOOK_COMMAND_TEMPLATE.format(python=shlex.quote(sys.executable or "python"), event=event)


OUTBOX_DIR_ENV = "VERDICT_CLAUDE_OUTBOX_DIR"
CLAUDE_VERSION_ENV = "VERDICT_CLAUDE_VERSION"
OUTBOX_FILENAME = "claude-lifecycle.jsonl"

# Exit-code contract with Claude Code hooks (docs.claude.com hooks reference):
#   0  success; stdout is empty, so nothing is injected into the context.
#   2  BLOCKING error: on PreToolUse it blocks the tool call, on UserPromptSubmit
#      it erases the prompt, on Stop/SubagentStop it prevents stopping. This
#      capture hook must never block the user's turn, so it never returns 2.
#   1  (any other nonzero) non-blocking error: Claude Code shows stderr and
#      continues. Used for malformed/unknown input, argv/payload mismatch and an
#      unwritable outbox, so failures are visible instead of masked by exit 0.
# The Verdict launch gate is a separate hook (see _ensure_claude_gate) and keeps
# its own fail-closed semantics; this capture hook does not touch it.
HOOK_EXIT_OK = 0
HOOK_EXIT_ERROR = 1


def default_outbox_dir() -> Path:
    configured = os.environ.get(OUTBOX_DIR_ENV)
    if configured:
        return Path(configured)
    return Path.home() / ".verdict" / "outbox" / "claude"


def _hook_error(message: str) -> int:
    print(f"verdict claude hook: {message}", file=sys.stderr)
    return HOOK_EXIT_ERROR


def run_hook(
    event: str,
    stdin_text: str,
    *,
    outbox_dir: Path | None = None,
    claude_version: str | None = None,
) -> int:
    """Translate one hook payload and append it to the local outbox.

    Returns HOOK_EXIT_OK on success or replay, HOOK_EXIT_ERROR (non-blocking,
    reason on stderr) otherwise. Stdout stays empty. Existing outbox records are
    never rewritten: the outbox is append-only, and an unwritable outbox leaves
    it untouched (degraded: the record is reported, not captured).
    """
    version = claude_version or os.environ.get(CLAUDE_VERSION_ENV) or "unknown"
    try:
        record = translate_hook_stdin(stdin_text, claude_version=version)
    except HarnessClaudeError as exc:
        return _hook_error(str(exc))
    payload_event = record["provenance"]["hook_event_name"]
    if payload_event != event:
        return _hook_error(f"hook event mismatch: argv {event!r} vs payload {payload_event!r}")
    target_dir = outbox_dir if outbox_dir is not None else default_outbox_dir()
    outbox = target_dir / OUTBOX_FILENAME
    event_id = record["identity"]["source_event_id"]
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        if outbox.exists():
            for line in outbox.read_text("utf-8").splitlines():
                try:
                    if json.loads(line)["identity"]["source_event_id"] == event_id:
                        return HOOK_EXIT_OK
                except (ValueError, KeyError, TypeError):
                    continue
        with outbox.open("a", encoding="utf-8") as handle:
            handle.write(_canonical_json(record) + "\n")
    except OSError as exc:
        return _hook_error(f"outbox unwritable ({outbox}): {exc.strerror or exc}")
    return HOOK_EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """Entry point used by ``python -m verdict.memory_harness.claude <Event>``."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        return _hook_error("usage: python -m verdict.memory_harness.claude <Event>")
    return run_hook(args[0], sys.stdin.read())


__all__ = [
    "BACKUP_NAME",
    "CLAUDE_HOOK_EVENT_CAPABILITY",
    "CLAUDE_HOOK_EVENT_KINDS",
    "DEFAULT_BASE_URL",
    "DEFAULT_TOKEN_ENV",
    "HOOK_COMMAND_TEMPLATE",
    "HOOK_EXIT_ERROR",
    "HOOK_EXIT_OK",
    "HOOK_MODULE",
    "OUTBOX_DIR_ENV",
    "OUTBOX_FILENAME",
    "CertifyReport",
    "ClaudeHarnessPaths",
    "DiscoverReport",
    "EnableResult",
    "HarnessClaudeError",
    "StatusReport",
    "apply_verdict_provider",
    "certify",
    "disable",
    "discover",
    "enable",
    "format_certify",
    "format_discover",
    "format_status",
    "hook_command",
    "main",
    "resolve_paths",
    "run_hook",
    "status",
    "translate_hook_payload",
    "translate_hook_stdin",
]


# --- Hook stdin -> lifecycle-event/v1 translation (pure, stdlib only) -------

LIFECYCLE_EVENT_SCHEMA = "lifecycle-event/v1"
LIFECYCLE_PROTOCOL_VERSION = "1"
CLAUDE_HOOK_EVENT_KINDS: dict[str, str] = {
    "SessionStart": "session_start",
    "UserPromptSubmit": "before_first_model_turn",
    "PreToolUse": "before_tool",
    "PostToolUse": "after_tool",
    "SubagentStop": "subagent_end",
    "PreCompact": "before_compact",
    "Stop": "before_yield",
    "SessionEnd": "session_end",
}

# Per-hook-event capability. "supported" = observed firing live in Claude Code 2.1.300
# (isolated OmniRoute smoke 2026-10-16 19:23 ET, fixtures tests/fixtures/claude/v2_1_300_omniroute;
# backend model agy/gemini-3.1-flash-lite, so this proves hooks, not the model).
# "partial" = installed and translated but never observed live.
# The 6 live-observed events are marked SUPPORTED per the controller's 2026-10-17 00:32Z approval.
CLAUDE_HOOK_EVENT_CAPABILITY: dict[str, Parity] = {
    "SessionStart": "supported",
    "UserPromptSubmit": "supported",
    "PreToolUse": "supported",
    "PostToolUse": "supported",
    "SubagentStop": "partial",
    "PreCompact": "partial",
    "Stop": "supported",
    "SessionEnd": "supported",
}


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def translate_hook_payload(payload: Mapping[str, Any], *, claude_version: str) -> dict[str, Any]:
    """Translate one Claude Code hook stdin payload into a lifecycle-event/v1 record.

    Pure: reads nothing but ``payload``. Prompt text comes from the payload's
    ``prompt`` field, never from global temp files. Scope is the cwd basename.
    """
    if not isinstance(payload, Mapping):
        raise HarnessClaudeError("hook payload must be a JSON object")
    event_name = payload.get("hook_event_name")
    session_id = payload.get("session_id")
    cwd = payload.get("cwd")
    for name, value in (("hook_event_name", event_name), ("session_id", session_id), ("cwd", cwd)):
        if not isinstance(value, str) or not value:
            raise HarnessClaudeError(f"hook payload missing {name}")
    assert isinstance(event_name, str) and isinstance(session_id, str) and isinstance(cwd, str)
    kind = CLAUDE_HOOK_EVENT_KINDS.get(event_name)
    if kind is None:
        raise HarnessClaudeError(f"unsupported Claude hook event: {event_name}")
    project = Path(cwd).name
    if not project:
        raise HarnessClaudeError("hook payload cwd has no project name")

    prompt = payload.get("prompt")
    if event_name == "UserPromptSubmit":
        if not isinstance(prompt, str) or not prompt:
            raise HarnessClaudeError("UserPromptSubmit payload missing prompt")
        content = prompt
    else:
        detail = payload.get("source") or payload.get("reason") or ""
        content = f"claude {event_name}" + (f" ({detail})" if detail else "")

    try:
        source_event_id = hashlib.sha256(_canonical_json(dict(payload)).encode("utf-8")).hexdigest()
    except (TypeError, ValueError) as exc:
        raise HarnessClaudeError("hook payload is not canonical JSON") from exc

    return {
        "schema_version": LIFECYCLE_EVENT_SCHEMA,
        "protocol_version": LIFECYCLE_PROTOCOL_VERSION,
        "kind": kind,
        "content": content,
        "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "identity": {
            "source_harness": "claude",
            "project": project,
            "session_id": session_id,
            "parent_session_id": None,
            "attempt_id": None,
            "source_event_id": source_event_id,
            "tenant": "default",
            "workspace": "default",
            "source_agent": "claude-code",
        },
        "scope": project,
        "classification": "source",
        "provenance": {
            "harness": "claude",
            "harness_version": claude_version,
            "hook_event_name": event_name,
            "cwd": cwd,
        },
    }


def translate_hook_stdin(text: str, *, claude_version: str) -> dict[str, Any]:
    """Parse hook stdin JSON text and translate it (see translate_hook_payload)."""
    try:
        payload = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise HarnessClaudeError("hook stdin is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise HarnessClaudeError("hook payload must be a JSON object")
    return translate_hook_payload(payload, claude_version=claude_version)
