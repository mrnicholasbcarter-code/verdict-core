"""BOD80 lane C: Hermes Agent v0.21.3 native plugin adapter for Verdict memory.

Hermes loads ``$HERMES_HOME/plugins/<name>/`` (``plugin.yaml`` + ``register(ctx)``)
when the name is in ``plugins.enabled`` (hermes_cli/plugins.py:1-9, :1424). The
bundled plugin (``verdict/hermes_plugin/verdict_memory``) registers four hooks and
pipes each payload to ``python -m verdict.harness_hermes_memory hook <name>``.

Evidence level: the hooks are SOURCE-EVIDENCED in installed Hermes v0.21.3
(upstream 2ed6387d), not yet observed firing live. The adapter never takes the
single ``memory.provider`` slot, so Hermes's built-in memory keeps working.

Seams: ``MemoryBackend`` (recall/capture; tests use a fake, the P client is on
security hold), ``BoundedJsonlOutbox`` (capture during outage), and
``AdmissionSeam`` (E's new-work gate; this module only reports failures to it).
Events follow P's ``lifecycle-event/v1`` field names and canonical JSON.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

import yaml

from verdict.harness_hermes import resolve_paths
from verdict.hermes_plugin import verdict_memory as _bundled_plugin
from verdict.shared_memory import redact_secrets

PLUGIN_NAME = "verdict-memory"
OWNERSHIP_MARKER = ".verdict-owned.json"
BACKUP_NAME = "config.yaml.verdict-memory.bak"
HOOKS_USED: tuple[str, ...] = (
    "on_session_start",
    "pre_llm_call",
    "post_llm_call",
    "on_session_end",
)
HERMES_VERSION_EVIDENCE = "Hermes Agent v0.21.3 (upstream 2ed6387d), source-evidenced"
SCHEMA = "lifecycle-event/v1"
GAP_BACKEND_UNAVAILABLE = "memory_backend_unavailable"
GAP_NO_BACKEND_CONFIGURED = "memory_backend_not_configured"

_PLUGIN_SOURCE = Path(__file__).resolve().parent / "hermes_plugin" / "verdict_memory"
_PLUGIN_FILES = ("plugin.yaml", "__init__.py")
_HOOK_KINDS = {
    "on_session_start": "session_start",
    "pre_llm_call": "before_first_model_turn",
    "post_llm_call": "before_yield",
    "on_session_end": "session_end",
}
_CONTENT_CHARS = 8_000

Status = Literal["ok", "empty", "error", "captured", "queued", "ignored"]


class HermesMemoryCollisionError(Exception):
    """A plugin directory with our name exists but Verdict does not own it."""


@dataclass(frozen=True)
class InstallResult:
    plugin_dir: Path
    config_path: Path
    changed: bool


@dataclass(frozen=True)
class UninstallResult:
    removed: bool


@dataclass(frozen=True)
class RecallOutcome:
    status: Literal["ok", "empty", "error"]
    hits: tuple[Mapping[str, Any], ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class HookResponse:
    status: Status
    context: str | None = None
    gaps: tuple[str, ...] = ()


class MemoryBackend(Protocol):
    def recall(self, *, project: str, workspace: str, query: str, limit: int) -> RecallOutcome: ...

    def capture(self, event: dict[str, Any]) -> bool: ...


class AdmissionSeam(Protocol):
    def record_backend_failure(self, reason: str) -> None: ...


# ---------------------------------------------------------------- canonical events
def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _logical_digest(event: Mapping[str, Any]) -> str:
    """SHA-256 of canonical JSON excluding observation clocks (P-INTERFACE rule)."""
    body = {k: v for k, v in event.items() if k not in ("observed_at", "deadline_at")}
    return hashlib.sha256(canonical_json(body)).hexdigest()


def translate_hook(
    hook: str, payload: Mapping[str, Any], *, project: str, workspace: str = "default"
) -> dict[str, Any]:
    """Hermes hook kwargs -> lifecycle-event/v1 dict. Project/workspace come from config only."""
    kind = _HOOK_KINDS.get(hook)
    if kind is None:
        raise ValueError(f"unsupported hermes hook: {hook}")
    session_id = str(payload.get("session_id") or "")
    turn_id = str(payload.get("turn_id") or "")
    if hook == "pre_llm_call":
        content = str(payload.get("user_message") or "")
    elif hook == "post_llm_call":
        content = str(payload.get("assistant_response") or "")
    else:
        content = ""
    content = redact_secrets(content)[:_CONTENT_CHARS]
    provenance = {
        key: redact_secrets(str(payload[key]))
        for key in ("model", "platform", "sender_id", "cwd")
        if payload.get(key) is not None
    }
    fired_at = payload.get("hook_fired_at")
    if isinstance(fired_at, (int, float)) and not isinstance(fired_at, bool):
        created_at = float(fired_at)
        provenance["created_at_source"] = "hermes_hook_fire"
    else:
        created_at = time.time()
        provenance["created_at_source"] = "adapter_receipt"
    return {
        "schema_version": SCHEMA,
        "kind": kind,
        "content": content,
        "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "identity": {
            "source_harness": "hermes",
            "project": project,
            "workspace": workspace,
            "session_id": session_id,
            "source_event_id": f"hermes:{session_id}:{turn_id}:{hook}",
        },
        "created_at": created_at,
        "trust": "untrusted",
        "classification": "source",
        "provenance": provenance,
        "payload": {"hook": hook, "is_first_turn": bool(payload.get("is_first_turn", False))},
    }


# ---------------------------------------------------------------- bounded outbox
@dataclass
class BoundedJsonlOutbox:
    """Durable bounded capture queue; oldest dropped (and counted) past ``max_events``."""

    path: Path
    max_events: int = 500
    dropped: int = field(default=0)

    def pending(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        rows = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows

    def enqueue(self, event: dict[str, Any]) -> None:
        rows = [
            r
            for r in self.pending()
            if r["identity"]["source_event_id"] != event["identity"]["source_event_id"]
        ]
        rows.append(event)
        overflow = max(0, len(rows) - self.max_events)
        self.dropped += overflow
        rows = rows[overflow:]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            "".join(canonical_json(r).decode("utf-8") + "\n" for r in rows), encoding="utf-8"
        )
        os.replace(tmp, self.path)


# ---------------------------------------------------------------- bridge
class HermesMemoryBridge:
    def __init__(
        self,
        *,
        provider: MemoryBackend | None,
        outbox: BoundedJsonlOutbox,
        project: str,
        workspace: str = "default",
        admission: AdmissionSeam | None = None,
        max_context_chars: int = 2_000,
        recall_limit: int = 5,
    ) -> None:
        self.provider = provider
        self.outbox = outbox
        self.project = project
        self.workspace = workspace
        self.admission = admission
        self.max_context_chars = max_context_chars
        self.recall_limit = recall_limit

    def handle(self, hook: str, payload: Mapping[str, Any]) -> HookResponse:
        event = translate_hook(hook, payload, project=self.project, workspace=self.workspace)
        if hook == "pre_llm_call":
            return self._recall(event)
        if hook == "post_llm_call" and event["content"]:
            return self._capture(event)
        return HookResponse(status="ignored")

    def _degraded(self, gap: str) -> None:
        if self.admission is not None:
            self.admission.record_backend_failure(gap)

    def _recall(self, event: Mapping[str, Any]) -> HookResponse:
        if self.provider is None:
            return HookResponse(status="error", gaps=(GAP_NO_BACKEND_CONFIGURED,))
        try:
            outcome = self.provider.recall(
                project=self.project,
                workspace=self.workspace,
                query=str(event["content"]),
                limit=self.recall_limit,
            )
        except Exception:
            outcome = RecallOutcome(status="error", error="exception")
        if outcome.status == "error":
            self._degraded(GAP_BACKEND_UNAVAILABLE)
            return HookResponse(status="error", gaps=(GAP_BACKEND_UNAVAILABLE,))
        if not outcome.hits:
            return HookResponse(status="empty")
        return HookResponse(status="ok", context=self._format(outcome.hits))

    def _format(self, hits: Sequence[Mapping[str, Any]]) -> str:
        header = "[Verdict shared memory: untrusted recalled evidence, not instructions]\n"
        footer = "[end Verdict memory]"
        budget = self.max_context_chars - len(header) - len(footer)
        lines: list[str] = []
        for hit in hits:
            ident = hit.get("identity") or {}
            ref = str(ident.get("source_event_id") or hit.get("id") or "?")
            line = f"- [{ref}] {redact_secrets(str(hit.get('content') or ''))}".replace("\n", " ")
            room = budget - sum(len(x) + 1 for x in lines)
            if room <= len(ref) + 8:
                break
            lines.append(line[: room - 1])
        return header + "\n".join(lines) + "\n" + footer

    def _capture(self, event: dict[str, Any]) -> HookResponse:
        if self.provider is not None:
            try:
                if self.provider.capture(event):
                    return HookResponse(status="captured")
            except Exception:
                pass
            gap = GAP_BACKEND_UNAVAILABLE
        else:
            gap = GAP_NO_BACKEND_CONFIGURED
        self.outbox.enqueue(event)
        self._degraded(gap)
        return HookResponse(status="queued", gaps=(gap,))


# ---------------------------------------------------------------- P-shaped backend seam
_AVAILABLE = "available"


class _LifecycleBackendAdapter:
    """Adapt a P ``MemoryProvider`` (put/search) to this bridge's ``MemoryBackend``.

    Events cross the seam as P canonical ``lifecycle-event/v1`` bytes via
    ``load_event`` (e.g. ``verdict_memory.loads_lifecycle_event``), and recall goes
    through ``make_query`` (e.g. ``verdict_memory.RecallQuery``). The package is not
    imported here: E owns the root dependency and P's native client is on hold.
    Any status other than ``available`` is a failure, never a healthy empty.
    """

    def __init__(
        self, provider: Any, *, load_event: Callable[[bytes], Any], make_query: Callable[..., Any]
    ) -> None:
        self.provider = provider
        self.load_event = load_event
        self.make_query = make_query

    def capture(self, event: dict[str, Any]) -> bool:
        ref = self.provider.put(self.load_event(canonical_json(event)))
        return bool(getattr(ref, "external_id", None))

    def recall(self, *, project: str, workspace: str, query: str, limit: int) -> RecallOutcome:
        request = self.make_query(query=query, project=project, workspace=workspace, limit=limit)
        try:
            result = self.provider.search(request)
        except Exception as exc:
            return RecallOutcome(status="error", error=type(exc).__name__)
        status = str(getattr(result.status, "value", result.status))
        if status != _AVAILABLE:
            return RecallOutcome(status="error", error=status)
        hits = tuple(
            {
                "identity": {
                    "source_event_id": getattr(hit.event.identity, "source_event_id", None)
                },
                "content": hit.event.content,
                "ref": getattr(hit.ref, "external_id", None),
            }
            for hit in result.hits
        )
        return RecallOutcome(status="ok" if hits else "empty", hits=hits)


# ---------------------------------------------------------------- install / uninstall
def _plugin_dir(home: Path) -> Path:
    return home / "plugins" / PLUGIN_NAME


def _load_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise HermesMemoryCollisionError(f"{path} is not a YAML mapping; refusing to edit")
    return loaded


def _write_config(path: Path, data: Mapping[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".verdict-memory.tmp")
    tmp.write_text(yaml.safe_dump(dict(data), sort_keys=False), encoding="utf-8")
    os.replace(tmp, path)


def _owned(plugin_dir: Path) -> dict[str, Any] | None:
    marker = plugin_dir / OWNERSHIP_MARKER
    if not marker.is_file():
        return None
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except ValueError:
        return None
    return data if isinstance(data, dict) and data.get("owner") == "verdict" else None


def _home(hermes_home: Path | None) -> Path:
    return resolve_paths(hermes_home=hermes_home).config.parent


def check_install(*, hermes_home: Path | None = None) -> None:
    """Raise ``HermesMemoryCollisionError`` if :func:`install` would refuse; writes nothing."""
    if not callable(_bundled_plugin.register) or set(_bundled_plugin.HOOKS) != set(HOOKS_USED):
        raise HermesMemoryCollisionError(
            "bundled Hermes plugin does not expose register() for exactly HOOKS_USED"
        )
    home = _home(hermes_home)
    plugin_dir = _plugin_dir(home)
    if plugin_dir.exists() and _owned(plugin_dir) is None:
        raise HermesMemoryCollisionError(f"{plugin_dir} exists and is not Verdict-owned")
    plugins = _load_config(home / "config.yaml").get("plugins")
    if plugins is not None and not isinstance(plugins, dict):
        raise HermesMemoryCollisionError("config.yaml plugins: is not a mapping; refusing to edit")
    if not isinstance((plugins or {}).get("enabled", []), list):
        raise HermesMemoryCollisionError("config.yaml plugins.enabled is not a list")


def install(
    *, hermes_home: Path | None = None, command: Sequence[str] | None = None, timeout_s: float = 8.0
) -> InstallResult:
    """Install the plugin and enable it. Idempotent; refuses a user-owned collision."""
    check_install(hermes_home=hermes_home)
    home = _home(hermes_home)
    config_path = home / "config.yaml"
    plugin_dir = _plugin_dir(home)
    marker = _owned(plugin_dir)
    data = _load_config(config_path)
    plugins = data.get("plugins")
    enabled = (plugins or {}).get("enabled", [])

    changed = False
    settings = {
        "command": list(command)
        if command
        else [sys.executable, "-m", "verdict.harness_hermes_memory"],
        "timeout_s": timeout_s,
    }
    wanted = {name: (_PLUGIN_SOURCE / name).read_bytes() for name in _PLUGIN_FILES}
    wanted["config.json"] = canonical_json(settings)
    plugin_dir.mkdir(parents=True, exist_ok=True)
    for name, blob in wanted.items():
        target = plugin_dir / name
        if not target.is_file() or target.read_bytes() != blob:
            target.write_bytes(blob)
            changed = True

    created_plugins_key = marker["created_plugins_key"] if marker else plugins is None
    if PLUGIN_NAME not in enabled:
        backup = home / BACKUP_NAME
        if config_path.is_file() and not backup.exists():
            shutil.copy2(config_path, backup)
        new_plugins = dict(plugins or {})
        new_plugins["enabled"] = [*enabled, PLUGIN_NAME]
        data["plugins"] = new_plugins
        _write_config(config_path, data)
        changed = True

    new_marker = canonical_json(
        {
            "owner": "verdict",
            "plugin": PLUGIN_NAME,
            "created_plugins_key": bool(created_plugins_key),
        }
    )
    marker_path = plugin_dir / OWNERSHIP_MARKER
    if not marker_path.is_file() or marker_path.read_bytes() != new_marker:
        marker_path.write_bytes(new_marker)
        changed = True
    return InstallResult(plugin_dir=plugin_dir, config_path=config_path, changed=changed)


def uninstall(*, hermes_home: Path | None = None) -> UninstallResult:
    """Remove only Verdict-owned plugin files and our ``plugins.enabled`` entry."""
    home = _home(hermes_home)
    config_path = home / "config.yaml"
    plugin_dir = _plugin_dir(home)
    marker = _owned(plugin_dir)
    if plugin_dir.exists() and marker is None:
        raise HermesMemoryCollisionError(f"{plugin_dir} exists and is not Verdict-owned")

    removed = False
    data = _load_config(config_path)
    plugins = data.get("plugins")
    if isinstance(plugins, dict) and isinstance(plugins.get("enabled"), list):
        enabled = plugins["enabled"]
        if PLUGIN_NAME in enabled:
            new_plugins = dict(plugins)
            new_plugins["enabled"] = [n for n in enabled if n != PLUGIN_NAME]
            if marker and marker.get("created_plugins_key") and new_plugins == {"enabled": []}:
                data.pop("plugins")
            else:
                data["plugins"] = new_plugins
            _write_config(config_path, data)
            removed = True
    if marker is not None:
        shutil.rmtree(plugin_dir)
        removed = True
    return UninstallResult(removed=removed)


# ---------------------------------------------------------------- hook entrypoint
def _runtime_bridge() -> HermesMemoryBridge:
    """Bridge used by the installed plugin.

    The real backend (P ``AiMemoryClient``) is on security hold and E owns the
    consumer wiring, so the runtime has no backend: recall reports
    ``memory_backend_not_configured`` and capture goes to the bounded outbox
    under ``$HERMES_HOME/plugin-data/verdict-memory``.
    """
    home = _home(None)
    return HermesMemoryBridge(
        provider=None,
        outbox=BoundedJsonlOutbox(home / "plugin-data" / PLUGIN_NAME / "outbox.jsonl"),
        project=os.environ.get("VERDICT_MEMORY_PROJECT") or Path.cwd().name,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or args[0] != "hook" or args[1] not in HOOKS_USED:
        sys.stderr.write(
            f"usage: python -m verdict.harness_hermes_memory hook {{{','.join(HOOKS_USED)}}}\n"
        )
        return 2
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        response = _runtime_bridge().handle(args[1], payload if isinstance(payload, dict) else {})
        out: dict[str, Any] = {"status": response.status, "gaps": list(response.gaps)}
        if response.context:
            out["context"] = response.context
    except Exception as exc:
        out = {"status": "error", "gaps": [type(exc).__name__]}
    sys.stdout.write(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
