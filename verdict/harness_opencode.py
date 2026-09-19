"""Architect-locked OpenCode harness switching for Verdict.

CLI surface (do not rename):

    verdict harness opencode discover
    verdict harness opencode enable   [--base-url ...] [--token-env ...] [--force]
    verdict harness opencode disable
    verdict harness opencode status
    verdict harness opencode certify  [--force]

``enable`` backs up ``~/.config/opencode/opencode.json`` before upserting a
Verdict OpenAI-compatible custom provider (``options.baseURL`` → Verdict
``:8000``, never OmniRoute ``:20128``) and setting ``model`` to
``verdict/default``. ``disable`` restores the backup byte-for-byte.

Binaries ``opencode`` / ``opencode-go`` may be absent from PATH —
discover/status/certify still work and report ``not-installed``. Auth keys in
``~/.local/share/opencode/auth.json`` are never written; live enable with
secrets is NEEDS_OWNER.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from verdict.harness_codex import DEFAULT_BASE_URL, DEFAULT_TOKEN_ENV, HealthCheck, probe_health

PROVIDER_ID = "verdict"
BACKUP_NAME = "opencode.json.verdict.bak"
DEFAULT_MODEL = "verdict/default"
SCHEMA_VERSION = "opencode-harness/v1"
OPENAI_COMPAT_NPM = "@ai-sdk/openai-compatible"
Parity = Literal["supported", "partial", "unsupported", "not-installed"]

_BINARY_NAMES = ("opencode", "opencode-go")


class HarnessOpenCodeError(Exception):
    """Raised when OpenCode harness enable/disable cannot proceed."""


@dataclass(frozen=True)
class OpenCodeHarnessPaths:
    config_dir: Path
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
    model: str = DEFAULT_MODEL


@dataclass(frozen=True)
class StatusReport:
    enabled: bool
    provider: str | None
    base_url: str | None
    token_env: str
    token_env_set: bool
    config_path: Path
    config_exists: bool
    integration: str | None = None
    model: str | None = None


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
    *, opencode_home: Path | None = None, home: Path | None = None
) -> OpenCodeHarnessPaths:
    if opencode_home is not None:
        root = Path(opencode_home).expanduser()
    else:
        env_cfg = os.getenv("OPENCODE_CONFIG")
        if env_cfg and env_cfg.strip():
            cfg = Path(env_cfg.strip()).expanduser()
            return OpenCodeHarnessPaths(
                config_dir=cfg.parent, config=cfg, backup=cfg.parent / BACKUP_NAME
            )
        xdg = os.getenv("XDG_CONFIG_HOME")
        if xdg and xdg.strip():
            root = Path(xdg.strip()).expanduser() / "opencode"
        else:
            root = (home or Path.home()) / ".config" / "opencode"
    config = root / "opencode.json"
    return OpenCodeHarnessPaths(config_dir=root, config=config, backup=root / BACKUP_NAME)


def discover(
    *, opencode_home: Path | None = None, which: Callable[[str], str | None] | None = None
) -> DiscoverReport:
    """Observe OpenCode install/config without mutating anything."""

    paths = resolve_paths(opencode_home=opencode_home)
    finder = which or shutil.which
    binary: str | None = None
    for name in _BINARY_NAMES:
        found = finder(name)
        if found:
            binary = found
            break
    exists = paths.config.is_file()
    base_url: str | None = None
    managed = paths.backup.is_file() or paths.absent_marker.exists()
    if exists:
        data = _load_json(paths.config)
        base_url = _provider_base_url(data)
        if _verdict_provider(data) is not None:
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
    )


def enable(
    *,
    base_url: str = DEFAULT_BASE_URL,
    token_env: str = DEFAULT_TOKEN_ENV,
    force: bool = False,
    opencode_home: Path | None = None,
    health_check: HealthCheck | Callable[[str], bool] | None = None,
) -> EnableResult:
    """Backup OpenCode config, then upsert the Verdict OpenAI-compatible provider."""

    resolved_base = base_url.strip() or DEFAULT_BASE_URL
    resolved_token_env = token_env.strip() or DEFAULT_TOKEN_ENV
    if "20128" in resolved_base and not force:
        raise HarnessOpenCodeError(
            f"refusing to enable OpenCode against OmniRoute-looking base_url={resolved_base}; "
            "Verdict-managed mode targets Verdict :8000"
        )
    if not force:
        healthy = (
            health_check(resolved_base)
            if health_check is not None
            else probe_health(base_url=resolved_base)
        )
        if not healthy:
            raise HarnessOpenCodeError(
                f"Verdict health check failed for {resolved_base}; "
                "refusing to enable OpenCode harness. Pass --force to override."
            )

    paths = resolve_paths(opencode_home=opencode_home)
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    existed = paths.config.is_file()
    created_backup = False
    if existed and not paths.backup.exists():
        shutil.copy2(paths.config, paths.backup)
        created_backup = True
    elif not existed and not paths.backup.exists() and not paths.absent_marker.exists():
        paths.absent_marker.write_text("absent\n", encoding="utf-8")

    data: MutableMapping[str, Any] = _load_json(paths.config) if existed else {}
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
        model=DEFAULT_MODEL,
    )


def disable(*, opencode_home: Path | None = None) -> None:
    """Restore the pre-enable OpenCode config backup exactly."""

    paths = resolve_paths(opencode_home=opencode_home)
    if paths.absent_marker.exists():
        if paths.config.exists():
            paths.config.unlink()
        paths.absent_marker.unlink()
        if paths.backup.exists():
            paths.backup.unlink()
        return
    if not paths.backup.is_file():
        raise HarnessOpenCodeError(
            f"Verdict OpenCode harness is not enabled: no backup at {paths.backup}"
        )
    shutil.copy2(paths.backup, paths.config)
    paths.backup.unlink()


def status(*, opencode_home: Path | None = None) -> StatusReport:
    """Return active Verdict base URL and whether the token env is set."""

    paths = resolve_paths(opencode_home=opencode_home)
    base_url: str | None = None
    token_env = DEFAULT_TOKEN_ENV
    integration: str | None = None
    model: str | None = None
    exists = paths.config.is_file()
    if exists:
        data = _load_json(paths.config)
        provider = _verdict_provider(data)
        if provider is not None:
            base_url = _entry_base_url(provider)
            marker = provider.get("verdictHarnessTokenEnv") or (
                (provider.get("env") or [None])[0]
                if isinstance(provider.get("env"), list)
                else None
            )
            if isinstance(marker, str) and marker.strip():
                token_env = marker.strip()
            if provider.get("verdictHarness") == "opencode" or _is_openai_compat(provider):
                integration = "openai-compatible"
        raw_model = data.get("model")
        if isinstance(raw_model, str) and raw_model.strip():
            model = raw_model.strip()
    enabled = bool(
        exists
        and base_url
        and "8000" in base_url
        and "20128" not in base_url
        and (
            paths.backup.exists()
            or paths.absent_marker.exists()
            or integration == "openai-compatible"
        )
    )
    return StatusReport(
        enabled=enabled,
        provider=PROVIDER_ID if enabled else None,
        base_url=base_url,
        token_env=token_env,
        token_env_set=bool(os.getenv(token_env) or os.getenv("OPENAI_API_KEY")),
        config_path=paths.config,
        config_exists=exists,
        integration=integration,
        model=model,
    )


def certify(
    *,
    opencode_home: Path | None = None,
    force: bool = False,
    health_check: HealthCheck | Callable[[str], bool] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> CertifyReport:
    """Evidence-only certification. Never prints secrets. Live token use is NEEDS_OWNER."""

    discovered = discover(opencode_home=opencode_home, which=which)
    report = status(opencode_home=opencode_home)
    base = report.base_url or DEFAULT_BASE_URL
    if force:
        healthy = True
    else:
        healthy = health_check(base) if health_check is not None else probe_health(base_url=base)

    facets: dict[str, Parity] = {
        "hooks": "unsupported",
        "model_selection": "partial",
        "config_import": "supported" if report.config_exists else "partial",
        "mcp": "partial",
        "subagents": "unsupported",
        "resume": "partial",
        "tool_interception": "unsupported",
        "structured_output": "partial",
    }
    notes: list[str] = [
        "OpenAI-compatible provider via ~/.config/opencode/opencode.json → Verdict :8000",
        "auth.json /connect credentials are never written by this adapter",
        "opencode-go binary accepted as alternate install name",
    ]
    needs_owner = (
        "live enable with secrets (opencode auth login / export token_env; NEEDS_OWNER)",
    )
    if not discovered.installed:
        overall: Parity = "not-installed"
        notes.append("opencode / opencode-go binary not found on PATH")
    elif report.enabled and healthy:
        overall = "partial"
    elif report.enabled:
        overall = "partial"
        notes.append("Verdict health probe failed; pass --force to ignore for local proof")
    else:
        overall = "unsupported"
        notes.append("harness not enabled for Verdict-managed OpenAI path")

    return CertifyReport(
        harness="opencode",
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
    return (
        f"OpenCode harness: {state}\n"
        f"  provider: {report.provider or '(none)'}\n"
        f"  base_url: {report.base_url or '(none)'}\n"
        f"  model: {report.model or '(none)'}\n"
        f"  integration: {report.integration or '(none)'}\n"
        f"  token_env: {report.token_env} (set: {token_state})\n"
    )


def format_discover(report: DiscoverReport) -> str:
    return (
        f"OpenCode discover:\n"
        f"  installed: {'yes' if report.installed else 'no'}\n"
        f"  binary: {report.binary_path or '(none)'}\n"
        f"  config: {report.config_path} (exists: {'yes' if report.config_exists else 'no'})\n"
        f"  managed_by_verdict: {'yes' if report.managed_by_verdict else 'no'}\n"
        f"  base_url: {report.base_url or '(none)'}\n"
        f"  pointing_at_verdict: {'yes' if report.pointing_at_verdict else 'no'}\n"
        f"  pointing_at_omniroute: {'yes' if report.pointing_at_omniroute else 'no'}\n"
    )


def format_certify(report: CertifyReport) -> str:
    facet_lines = "\n".join(f"  {name}: {level}" for name, level in sorted(report.facets.items()))
    notes = "\n".join(f"  - {note}" for note in report.notes)
    owner = "\n".join(f"  - {item}" for item in report.needs_owner)
    return (
        f"OpenCode certify: {report.overall}\n"
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
    """Upsert Verdict OpenAI-compatible provider in opencode.json.

    Writes under ``provider`` (canonical docs key). If a legacy ``providers``
    map exists, keep it and also mirror the Verdict entry there. Never writes
    token values — records ``env`` / ``verdictHarnessTokenEnv`` names only.
    """

    result = dict(data)
    provider_root = _provider_root(result)
    existing = provider_root.get(PROVIDER_ID)
    entry: dict[str, Any] = dict(existing) if isinstance(existing, Mapping) else {}
    entry["npm"] = entry.get("npm") or OPENAI_COMPAT_NPM
    entry["name"] = entry.get("name") or "Verdict"
    options = dict(entry.get("options") or {}) if isinstance(entry.get("options"), Mapping) else {}
    options["baseURL"] = base_url
    entry["options"] = options
    # Prefer settings.baseURL shape used by newer OpenCode docs when present.
    settings = (
        dict(entry.get("settings") or {}) if isinstance(entry.get("settings"), Mapping) else {}
    )
    if settings or "settings" in entry:
        settings["baseURL"] = base_url
        entry["settings"] = settings
    entry["env"] = [token_env]
    entry["verdictHarness"] = "opencode"
    entry["verdictHarnessTokenEnv"] = token_env
    entry["schemaVersion"] = SCHEMA_VERSION
    models = entry.get("models")
    if not isinstance(models, Mapping) or not models:
        entry["models"] = {"default": {"name": "Verdict default"}}
    for secret_key in ("apiKey", "api_key", "token", "secret", "password"):
        entry.pop(secret_key, None)
    provider_root[PROVIDER_ID] = entry
    result["provider"] = provider_root
    # Drop duplicate empty providers key collision if we normalized into provider.
    if "providers" in result and result.get("providers") is not provider_root:
        legacy = dict(result["providers"]) if isinstance(result["providers"], Mapping) else {}
        legacy[PROVIDER_ID] = entry
        result["providers"] = legacy
    result["model"] = DEFAULT_MODEL
    return result


def _provider_root(data: MutableMapping[str, Any]) -> dict[str, Any]:
    for key in ("provider", "providers"):
        raw = data.get(key)
        if isinstance(raw, Mapping):
            return dict(raw)
    return {}


def _verdict_provider(data: Mapping[str, Any]) -> Mapping[str, Any] | None:
    for key in ("provider", "providers"):
        root = data.get(key)
        if isinstance(root, Mapping):
            raw = root.get(PROVIDER_ID)
            if isinstance(raw, Mapping):
                return raw
    return None


def _entry_base_url(provider: Mapping[str, Any]) -> str | None:
    options = provider.get("options")
    if isinstance(options, Mapping):
        raw = options.get("baseURL") or options.get("baseUrl")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    settings = provider.get("settings")
    if isinstance(settings, Mapping):
        raw = settings.get("baseURL") or settings.get("baseUrl")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    raw = provider.get("baseURL") or provider.get("baseUrl")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def _provider_base_url(data: Mapping[str, Any]) -> str | None:
    provider = _verdict_provider(data)
    if provider is None:
        return None
    return _entry_base_url(provider)


def _is_openai_compat(provider: Mapping[str, Any]) -> bool:
    npm = provider.get("npm") or provider.get("package")
    if isinstance(npm, str) and "openai-compatible" in npm:
        return True
    return provider.get("verdictHarness") == "opencode"


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    loaded = json.loads(text)
    return dict(loaded) if isinstance(loaded, dict) else {}


def _atomic_write_json(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(dict(data), indent=2, sort_keys=False) + "\n", encoding="utf-8")
    tmp.replace(path)


__all__ = [
    "BACKUP_NAME",
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "DEFAULT_TOKEN_ENV",
    "OPENAI_COMPAT_NPM",
    "PROVIDER_ID",
    "SCHEMA_VERSION",
    "CertifyReport",
    "DiscoverReport",
    "EnableResult",
    "HarnessOpenCodeError",
    "OpenCodeHarnessPaths",
    "StatusReport",
    "apply_verdict_provider",
    "certify",
    "disable",
    "discover",
    "enable",
    "format_certify",
    "format_discover",
    "format_status",
    "resolve_paths",
    "status",
]
