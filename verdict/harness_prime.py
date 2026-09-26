"""Architect-locked Prime Agent harness switching for Verdict.

CLI surface (do not rename):

    verdict harness prime discover
    verdict harness prime enable   [--base-url ...] [--token-env ...] [--force]
    verdict harness prime disable
    verdict harness prime status
    verdict harness prime certify  [--force]
    verdict harness prime sync-models [--dry-run] [--gateway ...]

``enable`` backs up ``~/.prime/agent/models.json`` before upserting a Verdict
OpenAI-compatible provider (``baseUrl`` → Verdict ``:8000``, never OmniRoute
``:20128``). ``apiKey`` is stored as the *name* of an env var (never a secret
value). ``disable`` restores the backup byte-for-byte.

Binaries ``prime`` / ``prime-agent`` may be absent from PATH — discover/status/
certify still work and report ``not-installed``. Live enable with secrets is
NEEDS_OWNER.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from verdict.harness_codex import DEFAULT_BASE_URL, DEFAULT_TOKEN_ENV, HealthCheck, probe_health

PROVIDER_ID = "verdict"
BACKUP_NAME = "models.json.verdict.bak"
DEFAULT_MODEL_ID = "default"
SCHEMA_VERSION = "prime-harness/v1"
Parity = Literal["supported", "partial", "unsupported", "not-installed"]

_BINARY_NAMES = ("prime", "prime-agent")


class HarnessPrimeError(Exception):
    """Raised when Prime Agent harness enable/disable cannot proceed."""


@dataclass(frozen=True)
class PrimeHarnessPaths:
    agent_home: Path
    models: Path
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


def resolve_paths(*, prime_home: Path | None = None, home: Path | None = None) -> PrimeHarnessPaths:
    if prime_home is not None:
        root = Path(prime_home).expanduser()
    else:
        env_home = os.getenv("PRIME_AGENT_HOME") or os.getenv("PRIME_HOME")
        if env_home and env_home.strip():
            root = Path(env_home.strip()).expanduser()
            if root.name != "agent":
                root = root / "agent"
        else:
            root = (home or Path.home()) / ".prime" / "agent"
    models = root / "models.json"
    return PrimeHarnessPaths(agent_home=root, models=models, backup=models.parent / BACKUP_NAME)


def discover(
    *, prime_home: Path | None = None, which: Callable[[str], str | None] | None = None
) -> DiscoverReport:
    """Observe Prime Agent install/config without mutating anything."""

    paths = resolve_paths(prime_home=prime_home)
    finder = which or shutil.which
    binary: str | None = None
    for name in _BINARY_NAMES:
        found = finder(name)
        if found:
            binary = found
            break
    exists = paths.models.is_file()
    base_url: str | None = None
    managed = paths.backup.is_file() or paths.absent_marker.exists()
    if exists:
        data = _load_json(paths.models)
        base_url = _provider_base_url(data)
        if _verdict_provider(data) is not None:
            managed = True
    return DiscoverReport(
        installed=binary is not None,
        binary_path=binary,
        config_path=paths.models,
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
    prime_home: Path | None = None,
    health_check: HealthCheck | Callable[[str], bool] | None = None,
) -> EnableResult:
    """Backup Prime models.json, then upsert the Verdict OpenAI-compatible provider."""

    resolved_base = base_url.strip() or DEFAULT_BASE_URL
    resolved_token_env = token_env.strip() or DEFAULT_TOKEN_ENV
    if "20128" in resolved_base and not force:
        raise HarnessPrimeError(
            f"refusing to enable Prime Agent against OmniRoute-looking base_url={resolved_base}; "
            "Verdict-managed mode targets Verdict :8000"
        )
    if not force:
        healthy = (
            health_check(resolved_base)
            if health_check is not None
            else probe_health(base_url=resolved_base)
        )
        if not healthy:
            raise HarnessPrimeError(
                f"Verdict health check failed for {resolved_base}; "
                "refusing to enable Prime Agent harness. Pass --force to override."
            )

    paths = resolve_paths(prime_home=prime_home)
    paths.agent_home.mkdir(parents=True, exist_ok=True)
    existed = paths.models.is_file()
    created_backup = False
    if existed and not paths.backup.exists():
        shutil.copy2(paths.models, paths.backup)
        created_backup = True
    elif not existed and not paths.backup.exists() and not paths.absent_marker.exists():
        paths.absent_marker.write_text("absent\n", encoding="utf-8")

    data: MutableMapping[str, Any] = _load_json(paths.models) if existed else {}
    updated = apply_verdict_provider(data, base_url=resolved_base, token_env=resolved_token_env)
    _atomic_write_json(paths.models, updated)
    return EnableResult(
        config_path=paths.models,
        backup_path=paths.backup,
        base_url=resolved_base,
        token_env=resolved_token_env,
        created_backup=created_backup,
        forced=force,
        integration="openai-compatible",
    )


def disable(*, prime_home: Path | None = None) -> None:
    """Restore the pre-enable Prime models.json backup exactly."""

    paths = resolve_paths(prime_home=prime_home)
    if paths.absent_marker.exists():
        if paths.models.exists():
            paths.models.unlink()
        paths.absent_marker.unlink()
        if paths.backup.exists():
            paths.backup.unlink()
        return
    if not paths.backup.is_file():
        raise HarnessPrimeError(
            f"Verdict Prime Agent harness is not enabled: no backup at {paths.backup}"
        )
    shutil.copy2(paths.backup, paths.models)
    paths.backup.unlink()


def status(*, prime_home: Path | None = None) -> StatusReport:
    """Return active Verdict base URL and whether the token env is set."""

    paths = resolve_paths(prime_home=prime_home)
    base_url: str | None = None
    token_env = DEFAULT_TOKEN_ENV
    integration: str | None = None
    exists = paths.models.is_file()
    if exists:
        data = _load_json(paths.models)
        provider = _verdict_provider(data)
        if provider is not None:
            raw = provider.get("baseUrl") or provider.get("baseURL")
            if isinstance(raw, str) and raw.strip():
                base_url = raw.strip()
            key = provider.get("apiKey")
            if isinstance(key, str) and key.strip() and not key.startswith("sk-"):
                # Env-var name form (not a literal secret).
                token_env = key.strip()
            marker = provider.get("verdictHarnessTokenEnv")
            if isinstance(marker, str) and marker.strip():
                token_env = marker.strip()
            if (
                provider.get("verdictHarness") == "prime"
                or provider.get("api") == "openai-completions"
            ):
                integration = "openai-compatible"
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
        config_path=paths.models,
        config_exists=exists,
        integration=integration,
    )


def certify(
    *,
    prime_home: Path | None = None,
    force: bool = False,
    health_check: HealthCheck | Callable[[str], bool] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> CertifyReport:
    """Evidence-only certification. Never prints secrets. Live token use is NEEDS_OWNER."""

    discovered = discover(prime_home=prime_home, which=which)
    report = status(prime_home=prime_home)
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
        "OpenAI-compatible provider via ~/.prime/agent/models.json → Verdict :8000",
        "apiKey stored as env var name only; never writes secret values",
        "Anthropic Messages / built-in Prime Inference remain outside this adapter",
    ]
    needs_owner = (
        "live enable with secrets (export token_env; Prime /login and auth.json are NEEDS_OWNER)",
    )
    if not discovered.installed:
        overall: Parity = "not-installed"
        notes.append("prime / prime-agent binary not found on PATH")
    elif report.enabled and healthy:
        overall = "partial"
    elif report.enabled:
        overall = "partial"
        notes.append("Verdict health probe failed; pass --force to ignore for local proof")
    else:
        overall = "unsupported"
        notes.append("harness not enabled for Verdict-managed OpenAI path")

    return CertifyReport(
        harness="prime",
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
        f"Prime Agent harness: {state}\n"
        f"  provider: {report.provider or '(none)'}\n"
        f"  base_url: {report.base_url or '(none)'}\n"
        f"  integration: {report.integration or '(none)'}\n"
        f"  token_env: {report.token_env} (set: {token_state})\n"
    )


def format_discover(report: DiscoverReport) -> str:
    return (
        f"Prime Agent discover:\n"
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
        f"Prime Agent certify: {report.overall}\n"
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
    """Upsert Verdict OpenAI-compatible provider in Prime models.json.

    Preference: existing providers.verdict → OpenAI-compatible entry. Never
    writes token values — ``apiKey`` is the env var *name*.
    """

    result = dict(data)
    providers = dict(result.get("providers") or {})
    existing = providers.get(PROVIDER_ID)
    entry: dict[str, Any] = dict(existing) if isinstance(existing, Mapping) else {}
    entry["baseUrl"] = base_url
    entry["api"] = "openai-completions"
    entry["apiKey"] = token_env  # env var name — Prime resolves at request time
    entry["authHeader"] = True
    entry["verdictHarness"] = "prime"
    entry["verdictHarnessTokenEnv"] = token_env
    entry["schemaVersion"] = SCHEMA_VERSION
    models = entry.get("models")
    if not isinstance(models, list) or not models:
        entry["models"] = [
            {
                "id": DEFAULT_MODEL_ID,
                "name": "Verdict default",
                "reasoning": False,
                "input": ["text"],
            }
        ]
    # Never persist literal secrets under common key names.
    for secret_key in ("token", "secret", "password"):
        entry.pop(secret_key, None)
    providers[PROVIDER_ID] = entry
    result["providers"] = providers
    return result


OMNIROUTE_PROVIDER_ID = "omniroute"


@dataclass(frozen=True)
class SyncModelsResult:
    config_path: Path
    backup_path: Path | None
    added: tuple[str, ...]
    removed: tuple[str, ...]
    total: int
    dry_run: bool
    written: bool


def _live_model_entry(row: Mapping[str, Any], previous: Mapping[str, Any] | None) -> dict[str, Any]:
    """Prime model entry for one live gateway row; keeps prior per-model settings."""
    if previous is not None:
        return dict(previous)
    route_id = str(row["id"])
    caps = row.get("capabilities")
    caps = caps if isinstance(caps, Mapping) else {}
    context = int(row.get("max_input_tokens") or row.get("context_length") or 0)
    entry: dict[str, Any] = {
        "id": route_id,
        "name": route_id,
        "reasoning": bool(caps.get("reasoning")),
        "input": ["text"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
    }
    if context:
        entry["contextWindow"] = context
    max_out = int(row.get("max_output_tokens") or 0)
    if max_out:
        entry["maxTokens"] = max_out
    return entry


def sync_models(
    live_rows: Sequence[Mapping[str, Any]],
    *,
    dry_run: bool = False,
    prime_home: Path | None = None,
    now: Callable[[], str] | None = None,
) -> SyncModelsResult:
    """Rewrite only ``providers.omniroute.models`` from the live gateway inventory.

    Every other key in models.json (other providers, omniroute baseUrl/apiKey)
    is preserved. Existing model entries keep their settings (e.g.
    ``thinkingLevelMap``); new ids get a minimal entry. The file is backed up
    to ``models.json.verdict-sync-<stamp>.bak`` before writing. ``dry_run``
    computes the diff and writes nothing.
    """
    paths = resolve_paths(prime_home=prime_home)
    if not paths.models.is_file():
        raise HarnessPrimeError(f"Prime models.json not found at {paths.models}")
    data = _load_json(paths.models)
    providers = data.get("providers")
    provider = providers.get(OMNIROUTE_PROVIDER_ID) if isinstance(providers, Mapping) else None
    if not isinstance(provider, Mapping):
        raise HarnessPrimeError(
            f"no providers.{OMNIROUTE_PROVIDER_ID} entry in {paths.models}; refusing to create one"
        )
    old_models = provider.get("models")
    previous: dict[str, Mapping[str, Any]] = {}
    for item in old_models if isinstance(old_models, list) else []:
        if isinstance(item, Mapping) and item.get("id"):
            previous.setdefault(str(item["id"]), item)
    live: dict[str, Mapping[str, Any]] = {}
    for row in live_rows:
        if isinstance(row, Mapping) and isinstance(row.get("id"), str) and row["id"].strip():
            live.setdefault(row["id"], row)
    if not live:
        raise HarnessPrimeError("live gateway inventory is empty; refusing to clear models")
    added = tuple(sorted(set(live) - set(previous)))
    removed = tuple(sorted(set(previous) - set(live)))
    if dry_run:
        return SyncModelsResult(
            paths.models, None, added, removed, len(live), dry_run=True, written=False
        )
    stamp = now() if now is not None else _utc_stamp()
    backup = paths.models.with_name(f"models.json.verdict-sync-{stamp}.bak")
    shutil.copy2(paths.models, backup)
    new_provider = dict(provider)
    new_provider["models"] = [
        _live_model_entry(row, previous.get(rid)) for rid, row in live.items()
    ]
    new_providers = dict(providers) if isinstance(providers, Mapping) else {}
    new_providers[OMNIROUTE_PROVIDER_ID] = new_provider
    updated = dict(data)
    updated["providers"] = new_providers
    _atomic_write_json(paths.models, updated)
    return SyncModelsResult(
        paths.models, backup, added, removed, len(live), dry_run=False, written=True
    )


def format_sync_models(result: SyncModelsResult) -> str:
    mode = "dry-run (nothing written)" if result.dry_run else "written"
    lines = [
        f"Prime models.json omniroute sync: {mode}",
        f"  config: {result.config_path}",
        f"  live models: {result.total}",
        f"  added: {len(result.added)}",
        f"  removed: {len(result.removed)}",
    ]
    if result.backup_path is not None:
        lines.append(f"  backup: {result.backup_path}")
    for label, ids in (("+", result.added), ("-", result.removed)):
        lines.extend(f"  {label} {rid}" for rid in ids)
    return "\n".join(lines) + "\n"


def _utc_stamp() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _verdict_provider(data: Mapping[str, Any]) -> Mapping[str, Any] | None:
    providers = data.get("providers")
    if not isinstance(providers, Mapping):
        return None
    raw = providers.get(PROVIDER_ID)
    return raw if isinstance(raw, Mapping) else None


def _provider_base_url(data: Mapping[str, Any]) -> str | None:
    provider = _verdict_provider(data)
    if provider is None:
        return None
    raw = provider.get("baseUrl") or provider.get("baseURL")
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


__all__ = [
    "BACKUP_NAME",
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL_ID",
    "DEFAULT_TOKEN_ENV",
    "PROVIDER_ID",
    "SCHEMA_VERSION",
    "CertifyReport",
    "DiscoverReport",
    "EnableResult",
    "HarnessPrimeError",
    "PrimeHarnessPaths",
    "StatusReport",
    "SyncModelsResult",
    "apply_verdict_provider",
    "certify",
    "disable",
    "discover",
    "enable",
    "format_certify",
    "format_discover",
    "format_status",
    "format_sync_models",
    "resolve_paths",
    "status",
    "sync_models",
]
