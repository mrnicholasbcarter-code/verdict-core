"""Architect-locked Cursor harness switching for Verdict.

CLI surface (do not rename):

    verdict harness cursor discover
    verdict harness cursor enable   [--base-url ...] [--token-env ...] [--force] [--wrapper]
    verdict harness cursor disable
    verdict harness cursor status
    verdict harness cursor certify  [--force]

Integration preference (enable):

1. Existing Verdict-managed ``~/.cursor/verdict-provider.json``
2. OpenAI-compatible custom provider sidecar + optional Cursor User
   ``settings.json`` OpenAI base URL keys (never OmniRoute ``:20128``)
3. Wrapper script last (``~/.cursor/bin/cursor-verdict``) — only with ``--wrapper``
   or when no settings target is writable

Cursor IDE's "Override OpenAI Base URL" UI often lives in an opaque store
(``state.vscdb``). This adapter therefore certifies as **partial**: managed
provider file + env sidecar are reversible and CI-testable; live IDE toggle /
secret paste remains NEEDS_OWNER.

``disable`` restores the pre-enable backup of any file we mutated.
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

PROVIDER_NAME = "verdict"
PROVIDER_FILE = "verdict-provider.json"
ENV_SIDECAR = "verdict-openai.env"
BACKUP_SUFFIX = ".verdict.bak"
WRAPPER_NAME = "cursor-verdict"
SCHEMA_VERSION = "cursor-harness/v1"
Parity = Literal["supported", "partial", "unsupported"]

_SETTINGS_BASE_URL_KEYS = (
    "openai.baseUrl",
    "cursor.openai.baseUrl",
    "cursor.general.openaiBaseUrl",
)


class HarnessCursorError(Exception):
    """Raised when Cursor harness enable/disable cannot proceed."""


@dataclass(frozen=True)
class CursorHarnessPaths:
    cursor_home: Path
    provider: Path
    env_sidecar: Path
    wrapper: Path
    provider_backup: Path
    env_backup: Path
    wrapper_backup: Path
    settings: Path | None
    settings_backup: Path | None

    @property
    def absent_marker(self) -> Path:
        return Path(str(self.provider_backup) + ".absent")


@dataclass(frozen=True)
class EnableResult:
    config_path: Path
    backup_path: Path
    base_url: str
    token_env: str
    created_backup: bool
    forced: bool
    integration: str
    wrapper_path: Path | None = None
    settings_path: Path | None = None


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
    wrapper_present: bool = False


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
    settings_path: Path | None
    wrapper_path: Path | None


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
    *, cursor_home: Path | None = None, home: Path | None = None, settings_path: Path | None = None
) -> CursorHarnessPaths:
    root_home = home or Path.home()
    explicit_cursor_home = cursor_home is not None
    if cursor_home is not None:
        root = Path(cursor_home).expanduser()
    else:
        env_home = os.getenv("CURSOR_HOME")
        if env_home and env_home.strip():
            root = Path(env_home.strip()).expanduser()
        else:
            root = root_home / ".cursor"
    provider = root / PROVIDER_FILE
    env_sidecar = root / ENV_SIDECAR
    wrapper = root / "bin" / WRAPPER_NAME
    settings = settings_path
    # Only auto-detect IDE settings for the real home layout — never reach into
    # the operator's ~/.config when callers inject an isolated cursor_home.
    if settings is None and not explicit_cursor_home:
        candidate = root_home / ".config" / "Cursor" / "User" / "settings.json"
        if candidate.parent.is_dir() or candidate.is_file():
            settings = candidate
    settings_backup = Path(str(settings) + BACKUP_SUFFIX) if settings is not None else None
    return CursorHarnessPaths(
        cursor_home=root,
        provider=provider,
        env_sidecar=env_sidecar,
        wrapper=wrapper,
        provider_backup=Path(str(provider) + BACKUP_SUFFIX),
        env_backup=Path(str(env_sidecar) + BACKUP_SUFFIX),
        wrapper_backup=Path(str(wrapper) + BACKUP_SUFFIX),
        settings=settings,
        settings_backup=settings_backup,
    )


def discover(
    *,
    cursor_home: Path | None = None,
    which: Callable[[str], str | None] | None = None,
    settings_path: Path | None = None,
) -> DiscoverReport:
    paths = resolve_paths(cursor_home=cursor_home, settings_path=settings_path)
    finder = which or shutil.which
    binary = finder("cursor") or finder("cursor-agent")
    base_url: str | None = None
    managed = paths.provider.is_file() or paths.absent_marker.exists()
    if paths.provider.is_file():
        data = _load_json(paths.provider)
        raw = data.get("base_url")
        if isinstance(raw, str) and raw.strip():
            base_url = raw.strip()
        managed = True
    elif paths.settings is not None and paths.settings.is_file():
        base_url = _settings_base_url(_load_json(paths.settings))
    return DiscoverReport(
        installed=binary is not None,
        binary_path=binary,
        config_path=paths.provider,
        config_exists=paths.provider.is_file(),
        managed_by_verdict=managed,
        base_url=base_url,
        pointing_at_verdict=bool(base_url and "8000" in base_url and "20128" not in base_url),
        pointing_at_omniroute=bool(base_url and "20128" in base_url),
        settings_path=paths.settings,
        wrapper_path=paths.wrapper if paths.wrapper.is_file() else None,
    )


def enable(
    *,
    base_url: str = DEFAULT_BASE_URL,
    token_env: str = DEFAULT_TOKEN_ENV,
    force: bool = False,
    wrapper: bool = False,
    cursor_home: Path | None = None,
    settings_path: Path | None = None,
    health_check: HealthCheck | Callable[[str], bool] | None = None,
) -> EnableResult:
    resolved_base = base_url.strip() or DEFAULT_BASE_URL
    resolved_token_env = token_env.strip() or DEFAULT_TOKEN_ENV
    if "20128" in resolved_base and not force:
        raise HarnessCursorError(
            f"refusing to enable Cursor against OmniRoute-looking base_url={resolved_base}; "
            "Verdict-managed mode targets Verdict :8000"
        )
    if not force:
        healthy = (
            health_check(resolved_base)
            if health_check is not None
            else probe_health(base_url=resolved_base)
        )
        if not healthy:
            raise HarnessCursorError(
                f"Verdict health check failed for {resolved_base}; "
                "refusing to enable Cursor harness. Pass --force to override."
            )

    paths = resolve_paths(cursor_home=cursor_home, settings_path=settings_path)
    paths.cursor_home.mkdir(parents=True, exist_ok=True)

    created_backup = False
    if paths.provider.is_file() and not paths.provider_backup.exists():
        shutil.copy2(paths.provider, paths.provider_backup)
        created_backup = True
    elif not paths.provider.is_file() and not paths.absent_marker.exists():
        paths.absent_marker.write_text("absent\n", encoding="utf-8")

    # 1) Verdict-managed provider config (always)
    provider_doc = apply_verdict_provider(
        _load_json(paths.provider) if paths.provider.is_file() else {},
        base_url=resolved_base,
        token_env=resolved_token_env,
    )
    _atomic_write_json(paths.provider, provider_doc)
    integration = "verdict-managed"

    # 2) OpenAI-compatible sidecar + optional IDE settings
    _write_env_sidecar(paths, base_url=resolved_base, token_env=resolved_token_env)
    settings_written: Path | None = None
    if paths.settings is not None:
        settings_written = _upsert_settings(
            paths, base_url=resolved_base, token_env=resolved_token_env
        )
        if settings_written is not None:
            integration = "openai-compatible"

    # 3) Wrapper last — only when explicitly requested.
    wrapper_path: Path | None = None
    if wrapper:
        wrapper_path = _write_wrapper(paths, base_url=resolved_base, token_env=resolved_token_env)
        integration = "wrapper"

    return EnableResult(
        config_path=paths.provider,
        backup_path=paths.provider_backup,
        base_url=resolved_base,
        token_env=resolved_token_env,
        created_backup=created_backup,
        forced=force,
        integration=integration,
        wrapper_path=wrapper_path,
        settings_path=settings_written,
    )


def disable(*, cursor_home: Path | None = None, settings_path: Path | None = None) -> None:
    paths = resolve_paths(cursor_home=cursor_home, settings_path=settings_path)
    restored_any = False

    if paths.absent_marker.exists():
        if paths.provider.exists():
            paths.provider.unlink()
        paths.absent_marker.unlink()
        if paths.provider_backup.exists():
            paths.provider_backup.unlink()
        restored_any = True
    elif paths.provider_backup.is_file():
        shutil.copy2(paths.provider_backup, paths.provider)
        paths.provider_backup.unlink()
        restored_any = True

    if paths.env_backup.is_file():
        shutil.copy2(paths.env_backup, paths.env_sidecar)
        paths.env_backup.unlink()
        restored_any = True
    elif paths.env_sidecar.is_file() and restored_any:
        paths.env_sidecar.unlink()

    if paths.wrapper_backup.is_file():
        shutil.copy2(paths.wrapper_backup, paths.wrapper)
        paths.wrapper_backup.unlink()
        restored_any = True
    elif paths.wrapper.is_file() and restored_any:
        paths.wrapper.unlink()

    if (
        paths.settings is not None
        and paths.settings_backup is not None
        and paths.settings_backup.is_file()
    ):
        shutil.copy2(paths.settings_backup, paths.settings)
        paths.settings_backup.unlink()
        restored_any = True

    if not restored_any and not paths.provider.is_file():
        raise HarnessCursorError(
            f"Verdict Cursor harness is not enabled: no backup at {paths.provider_backup}"
        )
    if not restored_any:
        # Provider present without backup — treat as not enabled through this CLI.
        raise HarnessCursorError(
            f"Verdict Cursor harness is not enabled: no backup at {paths.provider_backup}"
        )


def status(*, cursor_home: Path | None = None, settings_path: Path | None = None) -> StatusReport:
    paths = resolve_paths(cursor_home=cursor_home, settings_path=settings_path)
    base_url: str | None = None
    token_env = DEFAULT_TOKEN_ENV
    integration: str | None = None
    exists = paths.provider.is_file()
    if exists:
        data = _load_json(paths.provider)
        raw = data.get("base_url")
        if isinstance(raw, str) and raw.strip():
            base_url = raw.strip()
        env_name = data.get("token_env")
        if isinstance(env_name, str) and env_name.strip():
            token_env = env_name.strip()
        integ = data.get("integration")
        if isinstance(integ, str) and integ.strip():
            integration = integ.strip()
        if data.get("enabled") is True and data.get("provider") == PROVIDER_NAME:
            integration = integration or "verdict-managed"
    enabled = bool(
        exists
        and base_url
        and "8000" in base_url
        and "20128" not in base_url
        and (paths.provider_backup.exists() or paths.absent_marker.exists() or integration)
    )
    return StatusReport(
        enabled=enabled,
        provider=PROVIDER_NAME if enabled else None,
        base_url=base_url,
        token_env=token_env,
        token_env_set=bool(os.getenv(token_env) or os.getenv("OPENAI_API_KEY")),
        config_path=paths.provider,
        config_exists=exists,
        integration=integration,
        wrapper_present=paths.wrapper.is_file(),
    )


def certify(
    *,
    cursor_home: Path | None = None,
    settings_path: Path | None = None,
    force: bool = False,
    health_check: HealthCheck | Callable[[str], bool] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> CertifyReport:
    discovered = discover(cursor_home=cursor_home, settings_path=settings_path, which=which)
    report = status(cursor_home=cursor_home, settings_path=settings_path)
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
        "subagents": "partial",
        "resume": "partial",
        "tool_interception": "unsupported",
        "structured_output": "partial",
    }
    notes: list[str] = [
        "Verdict-managed ~/.cursor/verdict-provider.json + verdict-openai.env",
        "Cursor IDE Override OpenAI Base URL may still need UI confirmation (opaque store)",
        "Cursor-managed subscription models may bypass custom OpenAI endpoints",
    ]
    needs_owner = ("live enable with secrets (API key paste in Cursor UI / token_env export)",)
    if not discovered.installed:
        overall: Parity = "unsupported"
        notes.append("cursor / cursor-agent binary not found on PATH")
    elif report.enabled:
        overall = "partial"
        if not healthy:
            notes.append("Verdict health probe failed; pass --force to ignore for local proof")
    else:
        overall = "unsupported"
        notes.append("harness not enabled for Verdict-managed OpenAI path")

    return CertifyReport(
        harness="cursor",
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
        f"Cursor harness: {state}\n"
        f"  provider: {report.provider or '(none)'}\n"
        f"  base_url: {report.base_url or '(none)'}\n"
        f"  integration: {report.integration or '(none)'}\n"
        f"  wrapper: {'yes' if report.wrapper_present else 'no'}\n"
        f"  token_env: {report.token_env} (set: {token_state})\n"
    )


def format_discover(report: DiscoverReport) -> str:
    return (
        f"Cursor discover:\n"
        f"  installed: {'yes' if report.installed else 'no'}\n"
        f"  binary: {report.binary_path or '(none)'}\n"
        f"  config: {report.config_path} (exists: {'yes' if report.config_exists else 'no'})\n"
        f"  managed_by_verdict: {'yes' if report.managed_by_verdict else 'no'}\n"
        f"  base_url: {report.base_url or '(none)'}\n"
        f"  pointing_at_verdict: {'yes' if report.pointing_at_verdict else 'no'}\n"
        f"  pointing_at_omniroute: {'yes' if report.pointing_at_omniroute else 'no'}\n"
        f"  settings: {report.settings_path or '(none)'}\n"
        f"  wrapper: {report.wrapper_path or '(none)'}\n"
    )


def format_certify(report: CertifyReport) -> str:
    facet_lines = "\n".join(f"  {name}: {level}" for name, level in sorted(report.facets.items()))
    notes = "\n".join(f"  - {note}" for note in report.notes)
    owner = "\n".join(f"  - {item}" for item in report.needs_owner)
    return (
        f"Cursor certify: {report.overall}\n"
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
    result = dict(data)
    result["schema_version"] = SCHEMA_VERSION
    result["provider"] = PROVIDER_NAME
    result["enabled"] = True
    result["base_url"] = base_url
    result["token_env"] = token_env
    result["integration"] = result.get("integration") or "verdict-managed"
    result["target"] = "verdict"
    # Never persist secret values.
    result.pop("api_key", None)
    result.pop("token", None)
    return result


def _write_env_sidecar(paths: CursorHarnessPaths, *, base_url: str, token_env: str) -> None:
    if paths.env_sidecar.is_file() and not paths.env_backup.exists():
        shutil.copy2(paths.env_sidecar, paths.env_backup)
    text = (
        f"# Verdict Cursor harness — source this file; never commit secrets.\n"
        f"export OPENAI_BASE_URL={_shell_quote(base_url)}\n"
        f"export OPENAI_API_BASE={_shell_quote(base_url)}\n"
        f'# export OPENAI_API_KEY="${{{token_env}}}"  # set by operator; not written by Verdict\n'
    )
    _atomic_write_text(paths.env_sidecar, text)


def _upsert_settings(paths: CursorHarnessPaths, *, base_url: str, token_env: str) -> Path | None:
    if paths.settings is None or paths.settings_backup is None:
        return None
    paths.settings.parent.mkdir(parents=True, exist_ok=True)
    existed = paths.settings.is_file()
    if existed and not paths.settings_backup.exists():
        shutil.copy2(paths.settings, paths.settings_backup)
    data: dict[str, Any] = _load_json(paths.settings) if existed else {}
    # Prefer updating an existing known key; otherwise write openai.baseUrl.
    written_key = None
    for key in _SETTINGS_BASE_URL_KEYS:
        if key in data:
            data[key] = base_url
            written_key = key
            break
    if written_key is None:
        data["openai.baseUrl"] = base_url
        written_key = "openai.baseUrl"
    data["verdict.harness.tokenEnv"] = token_env
    data["verdict.harness.enabled"] = True
    _atomic_write_json(paths.settings, data)
    return paths.settings


def _write_wrapper(paths: CursorHarnessPaths, *, base_url: str, token_env: str) -> Path:
    paths.wrapper.parent.mkdir(parents=True, exist_ok=True)
    if paths.wrapper.is_file() and not paths.wrapper_backup.exists():
        shutil.copy2(paths.wrapper, paths.wrapper_backup)
    script = f"""#!/usr/bin/env bash
# Verdict-managed Cursor wrapper — points OpenAI-compatible traffic at Verdict :8000.
set -euo pipefail
export OPENAI_BASE_URL={_shell_quote(base_url)}
export OPENAI_API_BASE={_shell_quote(base_url)}
if [[ -z "${{OPENAI_API_KEY:-}}" && -n "${{{token_env}:-}}" ]]; then
  export OPENAI_API_KEY="${{{token_env}}}"
fi
if command -v cursor-agent >/dev/null 2>&1; then
  exec cursor-agent "$@"
fi
if command -v cursor >/dev/null 2>&1; then
  exec cursor "$@"
fi
echo "cursor-verdict: neither cursor-agent nor cursor found on PATH" >&2
exit 127
"""
    _atomic_write_text(paths.wrapper, script)
    paths.wrapper.chmod(paths.wrapper.stat().st_mode | 0o111)
    return paths.wrapper


def _settings_base_url(data: Mapping[str, Any]) -> str | None:
    for key in _SETTINGS_BASE_URL_KEYS:
        raw = data.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return None


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


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_TOKEN_ENV",
    "ENV_SIDECAR",
    "PROVIDER_FILE",
    "PROVIDER_NAME",
    "SCHEMA_VERSION",
    "CertifyReport",
    "CursorHarnessPaths",
    "DiscoverReport",
    "EnableResult",
    "HarnessCursorError",
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
