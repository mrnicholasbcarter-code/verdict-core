"""Architect-locked Hermes harness switching for Verdict-managed mode.

CLI surface (do not rename):

    verdict harness hermes enable   [--base-url ...] [--token-env ...] [--model ...] [--force]
    verdict harness hermes disable
    verdict harness hermes status

``enable`` backs up ``~/.hermes/config.yaml`` before upserting a ``Verdict``
custom provider aimed at Verdict ``:8000`` (never OmniRoute ``:20128``) and
pointing ``model.provider`` at that entry. ``disable`` restores the backup
byte-for-byte. Status never prints token values.

Unrelated custom providers (e.g. OmniRoute) are preserved.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from verdict.harness_codex import DEFAULT_BASE_URL, DEFAULT_TOKEN_ENV, HealthCheck, probe_health

PROVIDER_NAME = "Verdict"
BACKUP_NAME = "config.yaml.verdict.bak"
DEFAULT_MODEL = "verdict/default"


class HarnessHermesError(Exception):
    """Raised when Hermes harness enable/disable cannot proceed."""


@dataclass(frozen=True)
class HermesHarnessPaths:
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
    model: str
    created_backup: bool
    forced: bool


@dataclass(frozen=True)
class StatusReport:
    enabled: bool
    provider: str | None
    base_url: str | None
    token_env: str
    token_env_set: bool
    config_path: Path
    config_exists: bool
    model: str | None = None


def resolve_paths(
    *, hermes_home: Path | None = None, home: Path | None = None
) -> HermesHarnessPaths:
    if hermes_home is not None:
        root = Path(hermes_home).expanduser()
    else:
        env_home = os.getenv("HERMES_HOME")
        if env_home and env_home.strip():
            root = Path(env_home.strip()).expanduser()
        else:
            root = (home or Path.home()) / ".hermes"
    config = root / "config.yaml"
    return HermesHarnessPaths(config=config, backup=config.parent / BACKUP_NAME)


def enable(
    *,
    base_url: str = DEFAULT_BASE_URL,
    token_env: str = DEFAULT_TOKEN_ENV,
    model: str = DEFAULT_MODEL,
    force: bool = False,
    hermes_home: Path | None = None,
    health_check: HealthCheck | Callable[[str], bool] | None = None,
) -> EnableResult:
    """Backup Hermes config, then set the active provider to Verdict."""

    resolved_base = base_url.strip() or DEFAULT_BASE_URL
    resolved_token_env = token_env.strip() or DEFAULT_TOKEN_ENV
    resolved_model = model.strip() or DEFAULT_MODEL
    if "20128" in resolved_base and not force:
        raise HarnessHermesError(
            f"refusing to enable Hermes against OmniRoute-looking base_url={resolved_base}; "
            "Verdict-managed mode targets Verdict :8000"
        )
    if not force:
        healthy = (
            health_check(resolved_base)
            if health_check is not None
            else probe_health(base_url=resolved_base)
        )
        if not healthy:
            raise HarnessHermesError(
                f"Verdict health check failed for {resolved_base}; "
                "refusing to enable Hermes harness. Pass --force to override."
            )

    paths = resolve_paths(hermes_home=hermes_home)
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
        loaded = yaml.safe_load(original_text)
        if not isinstance(loaded, dict):
            raise HarnessHermesError("Hermes config.yaml must be a mapping at the top level")
        data = dict(loaded)
    else:
        data = {}

    updated = apply_verdict_provider(
        data, base_url=resolved_base, token_env=resolved_token_env, model=resolved_model
    )
    _atomic_write_yaml(paths.config, updated)
    return EnableResult(
        config_path=paths.config,
        backup_path=paths.backup,
        base_url=resolved_base,
        token_env=resolved_token_env,
        model=resolved_model,
        created_backup=created_backup,
        forced=force,
    )


def disable(*, hermes_home: Path | None = None) -> None:
    """Restore the pre-enable Hermes config backup exactly."""

    paths = resolve_paths(hermes_home=hermes_home)
    if paths.absent_marker.exists():
        if paths.config.exists():
            paths.config.unlink()
        paths.absent_marker.unlink()
        if paths.backup.exists():
            paths.backup.unlink()
        return
    if not paths.backup.is_file():
        raise HarnessHermesError(
            f"Verdict Hermes harness is not enabled: no backup at {paths.backup}"
        )
    shutil.copy2(paths.backup, paths.config)
    paths.backup.unlink()


def status(*, hermes_home: Path | None = None) -> StatusReport:
    """Return active provider/base URL and whether the token env is set."""

    paths = resolve_paths(hermes_home=hermes_home)
    provider: str | None = None
    base_url: str | None = None
    model: str | None = None
    token_env = DEFAULT_TOKEN_ENV
    exists = paths.config.is_file()
    if exists:
        loaded = yaml.safe_load(paths.config.read_text(encoding="utf-8")) or {}
        if isinstance(loaded, Mapping):
            model_section = loaded.get("model")
            if isinstance(model_section, Mapping):
                provider = (
                    str(model_section["provider"])
                    if model_section.get("provider") is not None
                    else None
                )
                base_url = (
                    str(model_section["base_url"])
                    if model_section.get("base_url") is not None
                    else None
                )
                model = (
                    str(model_section["default"])
                    if model_section.get("default") is not None
                    else None
                )
            for entry in _provider_entries(loaded):
                if entry.get("name") == PROVIDER_NAME:
                    env_name = entry.get("key_env")
                    if isinstance(env_name, str) and env_name.strip():
                        token_env = env_name.strip()
                    if base_url is None and entry.get("base_url"):
                        base_url = str(entry["base_url"])
                    break
    return StatusReport(
        enabled=provider == PROVIDER_NAME,
        provider=provider,
        base_url=base_url,
        token_env=token_env,
        token_env_set=bool(os.getenv(token_env)),
        config_path=paths.config,
        config_exists=exists,
        model=model,
    )


def format_status(report: StatusReport) -> str:
    """Render status without ever including a token value."""

    if report.enabled:
        state = "enabled"
    elif report.config_exists:
        state = "configured"
    else:
        state = "not configured"
    token_state = "yes" if report.token_env_set else "no"
    return (
        f"Hermes harness: {state}\n"
        f"  provider: {report.provider or '(none)'}\n"
        f"  base_url: {report.base_url or '(none)'}\n"
        f"  model: {report.model or '(none)'}\n"
        f"  token_env: {report.token_env} (set: {token_state})\n"
    )


def apply_verdict_provider(
    data: MutableMapping[str, Any], *, base_url: str, token_env: str, model: str
) -> dict[str, Any]:
    """Upsert Verdict custom provider and point model.provider at it."""

    result = dict(data)
    model_section = dict(result.get("model") or {})
    model_section["provider"] = PROVIDER_NAME
    model_section["base_url"] = base_url
    model_section["default"] = model
    if "api_mode" not in model_section:
        model_section["api_mode"] = "chat_completions"
    result["model"] = model_section

    providers = list(_provider_entries(result))
    verdict_entry = {
        "name": PROVIDER_NAME,
        "base_url": base_url,
        "key_env": token_env,
        "model": model,
    }
    replaced = False
    for index, entry in enumerate(providers):
        if entry.get("name") == PROVIDER_NAME:
            merged = dict(entry)
            merged.update(verdict_entry)
            providers[index] = merged
            replaced = True
            break
    if not replaced:
        providers.append(verdict_entry)
    result["custom_providers"] = providers
    return result


def _provider_entries(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = data.get("custom_providers")
    if isinstance(raw, list):
        return [dict(item) for item in raw if isinstance(item, Mapping)]
    return []


def _atomic_write_yaml(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        yaml.safe_dump(dict(data), sort_keys=False, default_flow_style=False), encoding="utf-8"
    )
    tmp.replace(path)


__all__ = [
    "BACKUP_NAME",
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "DEFAULT_TOKEN_ENV",
    "PROVIDER_NAME",
    "EnableResult",
    "HarnessHermesError",
    "HermesHarnessPaths",
    "StatusReport",
    "apply_verdict_provider",
    "disable",
    "enable",
    "format_status",
    "resolve_paths",
    "status",
]
