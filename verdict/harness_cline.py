"""Architect-locked Cline harness switching for Verdict.

CLI surface (do not rename):

    verdict harness cline discover
    verdict harness cline enable   [--base-url ...] [--token-env ...] [--force]
    verdict harness cline disable
    verdict harness cline status
    verdict harness cline certify  [--force]

Integration preference (enable):

1. Existing Verdict-managed ``~/.cline/verdict-provider.json``
2. When ``cline`` CLI is present (or ``~/.cline/`` exists): upsert OpenAI-compatible
   base URL into ``~/.cline/data/settings/providers.json`` (no secrets written)
3. Otherwise: reversible env sidecar + optional VS Code / Code-OSS ``settings.json``
   keys; document IDE UI steps for OpenAI Compatible provider

Cline's VS Code extension often stores API keys in an opaque store. This adapter
therefore certifies as **partial**: managed files are reversible and CI-testable;
live IDE toggle / API-key paste remains NEEDS_OWNER.

``disable`` restores the pre-enable backup of any file we mutated.
Binary may be missing — discover/status/certify report graceful not-installed.
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
SCHEMA_VERSION = "cline-harness/v1"
Parity = Literal["supported", "partial", "unsupported"]

# Known VS Code / settings.json keys across Cline extension versions.
_SETTINGS_BASE_URL_KEYS = (
    "cline.openAiBaseUrl",
    "cline.openAiCompatible.baseUrl",
    "cline.openai.baseURL",
    "cline.openai.baseUrl",
)
_SETTINGS_PROVIDER_KEYS = ("cline.apiProvider", "cline.openAiCompatible.apiProvider")


class HarnessClineError(Exception):
    """Raised when Cline harness enable/disable cannot proceed."""


@dataclass(frozen=True)
class ClineHarnessPaths:
    cline_home: Path
    provider: Path
    env_sidecar: Path
    providers_json: Path
    provider_backup: Path
    env_backup: Path
    providers_backup: Path
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
    settings_path: Path | None = None
    providers_json_path: Path | None = None
    ui_steps: tuple[str, ...] = ()


@dataclass(frozen=True)
class StatusReport:
    enabled: bool
    provider: str | None
    base_url: str | None
    token_env: str
    token_env_set: bool
    config_path: Path
    config_exists: bool
    installed: bool = False
    binary_path: str | None = None
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
    settings_path: Path | None
    providers_json_path: Path | None
    cli_home_present: bool


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
    *, cline_home: Path | None = None, home: Path | None = None, settings_path: Path | None = None
) -> ClineHarnessPaths:
    root_home = home or Path.home()
    explicit_cline_home = cline_home is not None
    if cline_home is not None:
        root = Path(cline_home).expanduser()
    else:
        env_home = os.getenv("CLINE_HOME")
        if env_home and env_home.strip():
            root = Path(env_home.strip()).expanduser()
        else:
            data_dir = os.getenv("CLINE_DATA_DIR")
            if data_dir and data_dir.strip():
                # CLINE_DATA_DIR replaces ~/.cline/data — parent is the logical home.
                root = Path(data_dir.strip()).expanduser().parent
            else:
                root = root_home / ".cline"
    provider = root / PROVIDER_FILE
    env_sidecar = root / ENV_SIDECAR
    providers_json = root / "data" / "settings" / "providers.json"
    settings = settings_path
    # Only auto-detect IDE settings for the real home layout — never reach into
    # the operator's ~/.config when callers inject an isolated cline_home.
    if settings is None and not explicit_cline_home:
        for candidate in (
            root_home / ".config" / "Code" / "User" / "settings.json",
            root_home / ".config" / "Code - OSS" / "User" / "settings.json",
            root_home / ".config" / "VSCodium" / "User" / "settings.json",
        ):
            if candidate.parent.is_dir() or candidate.is_file():
                settings = candidate
                break
    settings_backup = Path(str(settings) + BACKUP_SUFFIX) if settings is not None else None
    return ClineHarnessPaths(
        cline_home=root,
        provider=provider,
        env_sidecar=env_sidecar,
        providers_json=providers_json,
        provider_backup=Path(str(provider) + BACKUP_SUFFIX),
        env_backup=Path(str(env_sidecar) + BACKUP_SUFFIX),
        providers_backup=Path(str(providers_json) + BACKUP_SUFFIX),
        settings=settings,
        settings_backup=settings_backup,
    )


def discover(
    *,
    cline_home: Path | None = None,
    which: Callable[[str], str | None] | None = None,
    settings_path: Path | None = None,
) -> DiscoverReport:
    paths = resolve_paths(cline_home=cline_home, settings_path=settings_path)
    finder = which or shutil.which
    binary = finder("cline")
    base_url: str | None = None
    managed = paths.provider.is_file() or paths.absent_marker.exists()
    if paths.provider.is_file():
        data = _load_json(paths.provider)
        raw = data.get("base_url")
        if isinstance(raw, str) and raw.strip():
            base_url = raw.strip()
        managed = True
    elif paths.providers_json.is_file():
        base_url = _providers_base_url(_load_json(paths.providers_json))
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
        providers_json_path=paths.providers_json if paths.providers_json.is_file() else None,
        cli_home_present=paths.cline_home.is_dir(),
    )


def enable(
    *,
    base_url: str = DEFAULT_BASE_URL,
    token_env: str = DEFAULT_TOKEN_ENV,
    force: bool = False,
    cline_home: Path | None = None,
    settings_path: Path | None = None,
    which: Callable[[str], str | None] | None = None,
    health_check: HealthCheck | Callable[[str], bool] | None = None,
) -> EnableResult:
    resolved_base = base_url.strip() or DEFAULT_BASE_URL
    resolved_token_env = token_env.strip() or DEFAULT_TOKEN_ENV
    if "20128" in resolved_base and not force:
        raise HarnessClineError(
            f"refusing to enable Cline against OmniRoute-looking base_url={resolved_base}; "
            "Verdict-managed mode targets Verdict :8000"
        )
    if not force:
        healthy = (
            health_check(resolved_base)
            if health_check is not None
            else probe_health(base_url=resolved_base)
        )
        if not healthy:
            raise HarnessClineError(
                f"Verdict health check failed for {resolved_base}; "
                "refusing to enable Cline harness. Pass --force to override."
            )

    paths = resolve_paths(cline_home=cline_home, settings_path=settings_path)
    # Capture existence before mkdir so we do not treat a fresh Verdict-created
    # ~/.cline as evidence that the Cline CLI is installed.
    home_existed = paths.cline_home.is_dir()
    paths.cline_home.mkdir(parents=True, exist_ok=True)

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

    # 2) Env sidecar (always; reversible)
    _write_env_sidecar(paths, base_url=resolved_base, token_env=resolved_token_env)

    finder = which or shutil.which
    cli_present = finder("cline") is not None or home_existed
    providers_written: Path | None = None
    settings_written: Path | None = None
    ui_steps: tuple[str, ...] = ()

    if cli_present:
        # Prefer CLI providers.json when the cline binary or ~/.cline tree exists.
        providers_written = _upsert_providers_json(
            paths, base_url=resolved_base, token_env=resolved_token_env
        )
        if providers_written is not None:
            integration = "cline-cli"
            provider_doc["integration"] = integration
            _atomic_write_json(paths.provider, provider_doc)
    else:
        # IDE-only: settings.json when present + document UI confirmation steps.
        if paths.settings is not None:
            settings_written = _upsert_settings(
                paths, base_url=resolved_base, token_env=resolved_token_env
            )
            if settings_written is not None:
                integration = "ide-settings"
                provider_doc["integration"] = integration
                _atomic_write_json(paths.provider, provider_doc)
        ui_steps = _ide_ui_steps(resolved_base)
        provider_doc["ui_steps"] = list(ui_steps)
        _atomic_write_json(paths.provider, provider_doc)

    return EnableResult(
        config_path=paths.provider,
        backup_path=paths.provider_backup,
        base_url=resolved_base,
        token_env=resolved_token_env,
        created_backup=created_backup,
        forced=force,
        integration=integration,
        settings_path=settings_written,
        providers_json_path=providers_written,
        ui_steps=ui_steps,
    )


def disable(*, cline_home: Path | None = None, settings_path: Path | None = None) -> None:
    paths = resolve_paths(cline_home=cline_home, settings_path=settings_path)
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

    if paths.providers_backup.is_file():
        shutil.copy2(paths.providers_backup, paths.providers_json)
        paths.providers_backup.unlink()
        restored_any = True

    if (
        paths.settings is not None
        and paths.settings_backup is not None
        and paths.settings_backup.is_file()
    ):
        shutil.copy2(paths.settings_backup, paths.settings)
        paths.settings_backup.unlink()
        restored_any = True

    if not restored_any and not paths.provider.is_file():
        raise HarnessClineError(
            f"Verdict Cline harness is not enabled: no backup at {paths.provider_backup}"
        )
    if not restored_any:
        raise HarnessClineError(
            f"Verdict Cline harness is not enabled: no backup at {paths.provider_backup}"
        )


def status(
    *,
    cline_home: Path | None = None,
    settings_path: Path | None = None,
    which: Callable[[str], str | None] | None = None,
) -> StatusReport:
    paths = resolve_paths(cline_home=cline_home, settings_path=settings_path)
    finder = which or shutil.which
    binary = finder("cline")
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
        installed=binary is not None,
        binary_path=binary,
        integration=integration,
    )


def certify(
    *,
    cline_home: Path | None = None,
    settings_path: Path | None = None,
    force: bool = False,
    health_check: HealthCheck | Callable[[str], bool] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> CertifyReport:
    discovered = discover(cline_home=cline_home, settings_path=settings_path, which=which)
    report = status(cline_home=cline_home, settings_path=settings_path, which=which)
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
        "Verdict-managed ~/.cline/verdict-provider.json + verdict-openai.env",
        "Prefer cline CLI providers.json when installed; else IDE settings sidecar + UI steps",
        "OmniRoute is optional upstream behind Verdict serve — not a harness target",
        "Cline extension API-key paste / opaque store may still need UI confirmation",
    ]
    needs_owner = ("live enable with secrets (API key paste in Cline UI / token_env export)",)
    if not discovered.installed and not discovered.cli_home_present and not report.config_exists:
        overall: Parity = "unsupported"
        notes.append("cline binary not found on PATH (graceful not-installed)")
    elif not discovered.installed:
        notes.append("cline binary not found on PATH; managed files/settings may still apply")
        overall = "partial" if report.enabled else "unsupported"
        if not report.enabled:
            notes.append("harness not enabled for Verdict-managed OpenAI path")
    elif report.enabled:
        overall = "partial"
        if not healthy:
            notes.append("Verdict health probe failed; pass --force to ignore for local proof")
    else:
        overall = "unsupported"
        notes.append("harness not enabled for Verdict-managed OpenAI path")

    return CertifyReport(
        harness="cline",
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
    elif not report.installed:
        state = "not installed"
    else:
        state = "not configured"
    token_state = "yes" if report.token_env_set else "no"
    return (
        f"Cline harness: {state}\n"
        f"  installed: {'yes' if report.installed else 'no'}\n"
        f"  binary: {report.binary_path or '(none)'}\n"
        f"  provider: {report.provider or '(none)'}\n"
        f"  base_url: {report.base_url or '(none)'}\n"
        f"  integration: {report.integration or '(none)'}\n"
        f"  token_env: {report.token_env} (set: {token_state})\n"
    )


def format_discover(report: DiscoverReport) -> str:
    return (
        f"Cline discover:\n"
        f"  installed: {'yes' if report.installed else 'no'}\n"
        f"  binary: {report.binary_path or '(none)'}\n"
        f"  cli_home: {'yes' if report.cli_home_present else 'no'}\n"
        f"  config: {report.config_path} (exists: {'yes' if report.config_exists else 'no'})\n"
        f"  managed_by_verdict: {'yes' if report.managed_by_verdict else 'no'}\n"
        f"  base_url: {report.base_url or '(none)'}\n"
        f"  pointing_at_verdict: {'yes' if report.pointing_at_verdict else 'no'}\n"
        f"  pointing_at_omniroute: {'yes' if report.pointing_at_omniroute else 'no'}\n"
        f"  settings: {report.settings_path or '(none)'}\n"
        f"  providers_json: {report.providers_json_path or '(none)'}\n"
    )


def format_certify(report: CertifyReport) -> str:
    facet_lines = "\n".join(f"  {name}: {level}" for name, level in sorted(report.facets.items()))
    notes = "\n".join(f"  - {note}" for note in report.notes)
    owner = "\n".join(f"  - {item}" for item in report.needs_owner)
    return (
        f"Cline certify: {report.overall}\n"
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
    result.pop("apiKey", None)
    result.pop("token", None)
    result.pop("openAiApiKey", None)
    return result


def _ide_ui_steps(base_url: str) -> tuple[str, ...]:
    return (
        "Open Cline in VS Code → Settings (gear) → API Configuration",
        "Set API Provider to OpenAI Compatible",
        f"Set Base URL to {base_url} (Verdict :8000 — not OmniRoute :20128)",
        "Paste API key from your token env (Verdict never writes secrets)",
        "Save / Done, then send a short test message",
    )


def _write_env_sidecar(paths: ClineHarnessPaths, *, base_url: str, token_env: str) -> None:
    if paths.env_sidecar.is_file() and not paths.env_backup.exists():
        shutil.copy2(paths.env_sidecar, paths.env_backup)
    text = (
        f"# Verdict Cline harness — source this file; never commit secrets.\n"
        f"export OPENAI_BASE_URL={_shell_quote(base_url)}\n"
        f"export OPENAI_API_BASE={_shell_quote(base_url)}\n"
        f'# export OPENAI_API_KEY="${{{token_env}}}"  # set by operator; not written by Verdict\n'
    )
    _atomic_write_text(paths.env_sidecar, text)


def _upsert_providers_json(
    paths: ClineHarnessPaths, *, base_url: str, token_env: str
) -> Path | None:
    """Upsert OpenAI-compatible base URL into Cline CLI providers.json without secrets."""

    paths.providers_json.parent.mkdir(parents=True, exist_ok=True)
    existed = paths.providers_json.is_file()
    if existed and not paths.providers_backup.exists():
        shutil.copy2(paths.providers_json, paths.providers_backup)
    data: dict[str, Any] = _load_json(paths.providers_json) if existed else {}

    # Prefer nested openai-compatible / openai shapes when present; else write a
    # Verdict-owned entry that CLI/docs can round-trip without wiping other providers.
    updated = False
    for key in ("openai-compatible", "openaiCompatible", "openai"):
        section = data.get(key)
        if isinstance(section, dict):
            section = dict(section)
            section["baseUrl"] = base_url
            section["baseURL"] = base_url
            section.pop("apiKey", None)
            section.pop("api_key", None)
            section["apiKeyEnv"] = token_env
            data[key] = section
            updated = True
    if not updated:
        data["openai-compatible"] = {
            "baseUrl": base_url,
            "baseURL": base_url,
            "apiKeyEnv": token_env,
            "provider": "openai-compatible",
            "managed_by": "verdict",
        }
        # Drop any accidental secret keys at top level.
        for secret_key in ("apiKey", "api_key", "openAiApiKey", "token"):
            data.pop(secret_key, None)

    data["verdict"] = {
        "enabled": True,
        "baseUrl": base_url,
        "tokenEnv": token_env,
        "target": "verdict",
    }
    _atomic_write_json(paths.providers_json, data)
    return paths.providers_json


def _upsert_settings(paths: ClineHarnessPaths, *, base_url: str, token_env: str) -> Path | None:
    if paths.settings is None or paths.settings_backup is None:
        return None
    paths.settings.parent.mkdir(parents=True, exist_ok=True)
    existed = paths.settings.is_file()
    if existed and not paths.settings_backup.exists():
        shutil.copy2(paths.settings, paths.settings_backup)
    data: dict[str, Any] = _load_json(paths.settings) if existed else {}
    written_key = None
    for key in _SETTINGS_BASE_URL_KEYS:
        if key in data:
            data[key] = base_url
            written_key = key
            break
    if written_key is None:
        data["cline.openAiBaseUrl"] = base_url
        written_key = "cline.openAiBaseUrl"
    # Prefer openai-compatible provider when we own the key.
    provider_written = False
    for key in _SETTINGS_PROVIDER_KEYS:
        if key in data:
            data[key] = "openai-compatible"
            provider_written = True
            break
    if not provider_written:
        data["cline.apiProvider"] = "openai-compatible"
    data["verdict.harness.tokenEnv"] = token_env
    data["verdict.harness.enabled"] = True
    # Never write API key values into settings.
    for secret_key in (
        "cline.openAiApiKey",
        "cline.openAiCompatible.apiKey",
        "cline.openai.apiKey",
    ):
        data.pop(secret_key, None)
    _atomic_write_json(paths.settings, data)
    return paths.settings


def _providers_base_url(data: Mapping[str, Any]) -> str | None:
    verdict = data.get("verdict")
    if isinstance(verdict, Mapping):
        raw = verdict.get("baseUrl") or verdict.get("base_url")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    for key in ("openai-compatible", "openaiCompatible", "openai"):
        section = data.get(key)
        if isinstance(section, Mapping):
            raw = section.get("baseUrl") or section.get("baseURL") or section.get("base_url")
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
    return None


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
    "ClineHarnessPaths",
    "DiscoverReport",
    "EnableResult",
    "HarnessClineError",
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
