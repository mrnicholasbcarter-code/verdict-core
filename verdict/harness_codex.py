"""Architect-locked Codex harness switching for Verdict.

CLI surface (do not rename):

    verdict harness codex enable   [--base-url ...] [--token-env ...] [--force]
    verdict harness codex disable
    verdict harness codex status

``enable`` backs up ``~/.codex/config.toml`` before writing a ``verdict`` provider
aimed at Verdict ``:8000`` (never OmniRoute ``:20128``). ``disable`` restores that
backup byte-for-byte. Status reports the active provider and whether the token
env var is set — never the token value.

Comments on untouched lines are left in place. Enable still rewrites the
``model_provider`` assignment and the ``[model_providers.verdict]`` keys; disable
is the path that restores comments exactly.
"""

from __future__ import annotations

import os
import shutil
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

DEFAULT_BASE_URL = "http://127.0.0.1:8000/v1"
DEFAULT_TOKEN_ENV = "LLMGATE_AUTH_TOKEN"
PROVIDER_NAME = "verdict"
WIRE_API = "responses"
BACKUP_NAME = "config.toml.verdict.bak"
HEALTH_TIMEOUT_SECONDS = 2.0

HealthCheck = Callable[[str], bool]


class HarnessCodexError(Exception):
    """Raised when Codex harness enable/disable cannot proceed."""


@dataclass(frozen=True)
class CodexHarnessPaths:
    """Resolved Codex config and Verdict backup locations."""

    config: Path
    backup: Path

    @property
    def absent_marker(self) -> Path:
        """Sidecar noting that enable created config.toml from nothing."""

        return Path(str(self.backup) + ".absent")


@dataclass(frozen=True)
class EnableResult:
    """Outcome of pointing Codex at Verdict."""

    config_path: Path
    backup_path: Path
    base_url: str
    token_env: str
    created_backup: bool
    forced: bool


@dataclass(frozen=True)
class StatusReport:
    """Public Codex harness status. Never includes secret values."""

    enabled: bool
    provider: str | None
    base_url: str | None
    token_env: str
    token_env_set: bool
    config_path: Path
    config_exists: bool


def resolve_paths(*, codex_home: Path | None = None, home: Path | None = None) -> CodexHarnessPaths:
    """Return Codex config.toml and the stable Verdict backup path."""

    if codex_home is not None:
        root = Path(codex_home).expanduser()
    else:
        env_home = os.getenv("CODEX_HOME")
        if env_home and env_home.strip():
            root = Path(env_home).expanduser()
        else:
            root = (home or Path.home()) / ".codex"
    config = root / "config.toml"
    return CodexHarnessPaths(config=config, backup=config.parent / BACKUP_NAME)


def health_urls(base_url: str) -> tuple[str, ...]:
    """Derive GET /health and /v1/models URLs from a Verdict OpenAI base URL."""

    parsed = urlsplit(base_url.strip())
    origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")
    path = parsed.path.rstrip("/")
    if path.endswith("/models"):
        models_url = f"{origin}{path}"
    elif path.endswith("/v1") or path:
        models_url = f"{origin}{path}/models"
    else:
        models_url = f"{origin}/v1/models"
    return (f"{origin}/health", models_url)


def probe_health(*, base_url: str, timeout: float = HEALTH_TIMEOUT_SECONDS) -> bool:
    """Return True if Verdict answers GET /health or /v1/models with HTTP 2xx."""

    for url in health_urls(base_url):
        try:
            request = urllib.request.Request(
                url, method="GET", headers={"Accept": "application/json"}
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
                status = int(getattr(response, "status", 0))
                if 200 <= status < 300:
                    return True
        except urllib.error.HTTPError:
            continue
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            continue
    return False


def enable(
    *,
    base_url: str = DEFAULT_BASE_URL,
    token_env: str = DEFAULT_TOKEN_ENV,
    force: bool = False,
    codex_home: Path | None = None,
    health_check: HealthCheck | None = None,
) -> EnableResult:
    """Backup Codex config, then set the active provider to Verdict."""

    resolved_base = base_url.strip() or DEFAULT_BASE_URL
    resolved_token_env = token_env.strip() or DEFAULT_TOKEN_ENV
    if not force:
        healthy = (
            health_check(resolved_base)
            if health_check is not None
            else probe_health(base_url=resolved_base)
        )
        if not healthy:
            raise HarnessCodexError(
                f"Verdict health check failed for {resolved_base}; "
                "refusing to enable Codex harness. Pass --force to override."
            )

    paths = resolve_paths(codex_home=codex_home)
    paths.config.parent.mkdir(parents=True, exist_ok=True)
    existed = paths.config.is_file()
    created_backup = False
    if existed and not paths.backup.exists():
        shutil.copy2(paths.config, paths.backup)
        created_backup = True
    elif not existed and not paths.backup.exists() and not paths.absent_marker.exists():
        paths.absent_marker.write_text("absent\n", encoding="utf-8")

    original = paths.config.read_text(encoding="utf-8") if existed else ""
    updated = apply_verdict_provider(original, base_url=resolved_base, token_env=resolved_token_env)
    _atomic_write(paths.config, updated)
    return EnableResult(
        config_path=paths.config,
        backup_path=paths.backup,
        base_url=resolved_base,
        token_env=resolved_token_env,
        created_backup=created_backup,
        forced=force,
    )


def disable(*, codex_home: Path | None = None) -> None:
    """Restore the pre-enable Codex config backup exactly."""

    paths = resolve_paths(codex_home=codex_home)
    if paths.absent_marker.exists():
        if paths.config.exists():
            paths.config.unlink()
        paths.absent_marker.unlink()
        if paths.backup.exists():
            paths.backup.unlink()
        return
    if not paths.backup.is_file():
        raise HarnessCodexError(
            f"Verdict Codex harness is not enabled: no backup at {paths.backup}"
        )
    shutil.copy2(paths.backup, paths.config)
    paths.backup.unlink()


def status(*, codex_home: Path | None = None) -> StatusReport:
    """Return active provider, base URL, and whether the token env is set."""

    paths = resolve_paths(codex_home=codex_home)
    provider: str | None = None
    base_url: str | None = None
    token_env = DEFAULT_TOKEN_ENV
    exists = paths.config.is_file()
    if exists:
        text = paths.config.read_text(encoding="utf-8")
        provider = _top_level_string(text, "model_provider")
        if provider:
            table = f"model_providers.{provider}"
            table_env = _table_string(text, table, "env_key")
            if table_env:
                token_env = table_env
            base_url = _table_string(text, table, "base_url")
    token_env_set = bool(os.getenv(token_env))
    return StatusReport(
        enabled=provider == PROVIDER_NAME,
        provider=provider,
        base_url=base_url,
        token_env=token_env,
        token_env_set=token_env_set,
        config_path=paths.config,
        config_exists=exists,
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
        f"Codex harness: {state}\n"
        f"  provider: {report.provider or '(none)'}\n"
        f"  base_url: {report.base_url or '(none)'}\n"
        f"  token_env: {report.token_env} (set: {token_state})\n"
    )


def apply_verdict_provider(text: str, *, base_url: str, token_env: str) -> str:
    """Set ``model_provider = verdict`` and upsert the Verdict provider table."""

    updated = _set_top_level(text, "model_provider", _format_toml_value(PROVIDER_NAME))
    return _upsert_table(
        updated,
        f"model_providers.{PROVIDER_NAME}",
        {
            "base_url": base_url,
            "env_key": token_env,
            "wire_api": WIRE_API,
            "requires_openai_auth": False,
        },
    )


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _join(lines: list[str], newline: str) -> str:
    if not lines:
        return ""
    return newline.join(lines) + newline


def _format_toml_value(value: str | bool) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    escaped = (
        value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _header_name(line: str) -> str | None:
    stripped = line.split("#", 1)[0].strip()
    if stripped.startswith("[") and stripped.endswith("]") and not stripped.startswith("[["):
        return stripped[1:-1].strip()
    return None


def _assignment_key(line: str) -> str | None:
    stripped = line.split("#", 1)[0].strip()
    if not stripped or stripped.startswith("[") or "=" not in stripped:
        return None
    return stripped.split("=", 1)[0].strip() or None


def _parse_toml_string(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        inner = value[1:-1]
        return inner.replace("\\\\", "\\").replace('\\"', '"').replace("\\n", "\n")
    return value


def _top_level_string(text: str, key: str) -> str | None:
    in_table = False
    for line in text.splitlines():
        if _header_name(line) is not None:
            in_table = True
            continue
        if in_table:
            continue
        if _assignment_key(line) == key:
            raw = line.split("#", 1)[0].split("=", 1)[1]
            parsed: Any = _parse_toml_string(raw)
            return parsed if isinstance(parsed, str) else str(parsed)
    return None


def _table_string(text: str, table: str, key: str) -> str | None:
    in_table = False
    for line in text.splitlines():
        header = _header_name(line)
        if header is not None:
            in_table = header == table
            continue
        if not in_table:
            continue
        if _assignment_key(line) == key:
            raw = line.split("#", 1)[0].split("=", 1)[1]
            parsed: Any = _parse_toml_string(raw)
            return parsed if isinstance(parsed, str) else str(parsed)
    return None


def _set_top_level(text: str, key: str, formatted_value: str) -> str:
    newline = _newline(text)
    lines = text.splitlines()
    assignment = f"{key} = {formatted_value}"
    in_table = False
    first_table_at: int | None = None
    found = False
    out: list[str] = []
    for line in lines:
        header = _header_name(line)
        if header is not None:
            if first_table_at is None:
                first_table_at = len(out)
            in_table = True
        if not in_table and _assignment_key(line) == key:
            out.append(assignment)
            found = True
            continue
        out.append(line)
    if not found:
        insert_at = first_table_at if first_table_at is not None else len(out)
        block = [assignment]
        if insert_at < len(out) and (
            insert_at == 0 or (insert_at > 0 and out[insert_at - 1] != "")
        ):
            block.append("")
        out[insert_at:insert_at] = block
    return _join(out, newline)


def _upsert_table(text: str, table: str, values: Mapping[str, str | bool]) -> str:
    newline = _newline(text)
    lines = text.splitlines()
    start: int | None = None
    end: int | None = None
    for idx, line in enumerate(lines):
        header = _header_name(line)
        if header == table:
            start = idx
            continue
        if start is not None and header is not None:
            end = idx
            break

    if start is None:
        extra: list[str] = []
        if lines and lines[-1].strip() != "":
            extra.append("")
        extra.append(f"[{table}]")
        extra.extend(f"{key} = {_format_toml_value(value)}" for key, value in values.items())
        return _join(lines + extra, newline)

    remaining = dict(values)
    body: list[str] = []
    section_end = end if end is not None else len(lines)
    for line in lines[start + 1 : section_end]:
        assigned = _assignment_key(line)
        if assigned in remaining:
            body.append(f"{assigned} = {_format_toml_value(remaining.pop(assigned))}")
        else:
            body.append(line)
    inserted = [f"{key} = {_format_toml_value(value)}" for key, value in remaining.items()]
    rebuilt = [*lines[:start], f"[{table}]", *inserted, *body, *lines[section_end:]]
    return _join(rebuilt, newline)
