"""Discover existing MCP server entries from supported harness config files.

Supported registries: Claude Code, Cursor, Codex, Prime (``settings.json`` ``mcpServers``
and ``mcp-connections.json``), Hermes (``config.yaml`` ``mcp_servers``, ``config.json``,
``mcp.json``) and project/global ``.mcp.json``.

Capability-bootstrap import/reuse: read config paths, server names, transport and the
stdio ``command`` only. Never read or copy ``env`` / ``args`` / ``headers`` / URLs /
tokens between tools. Config presence is not health.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from verdict.harness_hermes import resolve_paths as resolve_hermes_paths
from verdict.orchestration.prime_settings import default_prime_agent_dir

# Map common MCP server registry names → capability-bootstrap catalog ids.
_SERVER_NAME_TO_PROVIDER: Mapping[str, str] = {
    "serena": "adapter.serena_lsp",
    "serena-mcp": "adapter.serena_lsp",
    "serena_lsp": "adapter.serena_lsp",
    "adapter.serena_lsp": "adapter.serena_lsp",
    "codebase-memory-mcp": "adapter.codebase_memory",
    "codebase-memory": "adapter.codebase_memory",
    "codebase_memory": "adapter.codebase_memory",
    "adapter.codebase_memory": "adapter.codebase_memory",
    "context7": "adapter.context7",
    "context7-mcp": "adapter.context7",
    "adapter.context7": "adapter.context7",
}

_TRANSPORTS: Mapping[str, str] = {
    "stdio": "stdio",
    "http": "http",
    "streamable-http": "http",
    "streamable_http": "http",
    "streamablehttp": "http",
    "sse": "sse",
}

_CODEX_MCP_SECTION = re.compile(r"(?m)^\[mcp_servers\.(?P<name>[^\]]+)\]\s*$")


@dataclass(frozen=True)
class McpConfigHit:
    """One MCP server name found in a harness config file (no secrets)."""

    provider_id: str
    server_name: str
    harness: str
    config_path: str
    transport: str | None = None
    command: str | None = None


def resolve_provider_id(server_name: str) -> str | None:
    """Map a harness MCP server key to a bootstrap catalog provider id."""

    key = server_name.strip().lower().replace("_", "-")
    if key in _SERVER_NAME_TO_PROVIDER:
        return _SERVER_NAME_TO_PROVIDER[key]
    # Accept dotted catalog ids and underscore variants.
    dotted = server_name.strip().lower()
    return _SERVER_NAME_TO_PROVIDER.get(dotted)


def _read_json_object(path: Path) -> Mapping[str, Any] | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _mcp_servers(data: Mapping[str, Any]) -> Mapping[str, Any]:
    """``mcpServers`` (Claude/Cursor/Prime/Hermes JSON) or ``mcp_servers`` (Hermes YAML)."""

    for key in ("mcpServers", "mcp_servers"):
        servers = data.get(key)
        if isinstance(servers, dict):
            return servers
    return {}


def _server_transport(body: object) -> tuple[str | None, str | None]:
    """Return (transport, stdio executable) from a server body.

    ``env`` / ``args`` / ``headers`` and the URL value are never read; a ``url`` key only
    marks the transport as remote. Unknown transport values are dropped, not copied.
    """

    if not isinstance(body, Mapping):
        return None, None
    declared = body.get("type") or body.get("transport")
    if isinstance(declared, str):
        transport = _TRANSPORTS.get(declared.strip().lower())
    else:
        transport = "stdio" if "command" in body else "http" if "url" in body else None
    if transport != "stdio":
        return transport, None
    raw_command = body.get("command")
    command = raw_command.strip() if isinstance(raw_command, str) else ""
    if not command:
        return transport, None
    # The executable only. A command with whitespace is reported only when it is an
    # existing absolute file (a ``Program Files`` path); otherwise it is a shell line
    # that may carry inline secrets.
    if any(char.isspace() for char in command):
        path = Path(command)
        return transport, command if path.is_absolute() and path.is_file() else None
    return transport, command


def _hits_from_mcp_servers(
    *, servers: Mapping[str, Any], harness: str, config_path: Path
) -> list[McpConfigHit]:
    hits: list[McpConfigHit] = []
    for raw_name, body in servers.items():
        if not isinstance(raw_name, str):
            continue
        provider_id = resolve_provider_id(raw_name)
        if provider_id is None:
            continue
        transport, command = _server_transport(body)
        hits.append(
            McpConfigHit(
                provider_id=provider_id,
                server_name=raw_name,
                harness=harness,
                config_path=str(config_path),
                transport=transport,
                command=command,
            )
        )
    return hits


def _hits_from_prime_connections(path: Path) -> list[McpConfigHit]:
    """Prime ``mcp-connections.json``: remote connections keyed by name with a ``serviceId``."""

    data = _read_json_object(path)
    connections = data.get("connections") if data is not None else None
    if not isinstance(connections, dict):
        return []
    hits: list[McpConfigHit] = []
    for raw_name, body in connections.items():
        if not isinstance(raw_name, str):
            continue
        service = body.get("serviceId") if isinstance(body, Mapping) else None
        provider_id = resolve_provider_id(raw_name) or (
            resolve_provider_id(service) if isinstance(service, str) else None
        )
        if provider_id is None:
            continue
        hits.append(
            McpConfigHit(
                provider_id=provider_id,
                server_name=raw_name,
                harness="prime",
                config_path=str(path),
                transport="http",
            )
        )
    return hits


def _hits_from_codex_toml(path: Path) -> list[McpConfigHit]:
    """Parse Codex ``[mcp_servers.<name>]`` section headers only (no secret values)."""

    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return []
    hits: list[McpConfigHit] = []
    seen: set[str] = set()
    for match in _CODEX_MCP_SECTION.finditer(text):
        raw_name = match.group("name").strip()
        if not raw_name or raw_name in seen:
            continue
        seen.add(raw_name)
        provider_id = resolve_provider_id(raw_name)
        if provider_id is None:
            continue
        hits.append(
            McpConfigHit(
                provider_id=provider_id,
                server_name=raw_name,
                harness="codex",
                config_path=str(path),
            )
        )
    return hits


def _read_hermes_yaml(path: Path) -> Mapping[str, Any] | None:
    """Parse only the top-level ``mcp_servers:`` block of a Hermes ``config.yaml``.

    The rest of the file (provider keys, gateway tokens) is never parsed, and the
    slice keeps discovery fast on large configs.
    """

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    block: list[str] = []
    for line in text.splitlines():
        if block:
            if line and not line[0].isspace() and not line.startswith("#"):
                break
            block.append(line)
        elif line.startswith("mcp_servers:"):
            block.append(line)
    if not block:
        return None
    try:
        data = yaml.safe_load("\n".join(block))
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def _hits_from_config(*, harness: str, path: Path) -> list[McpConfigHit]:
    if path.suffix == ".toml":
        return _hits_from_codex_toml(path)
    if path.name == "mcp-connections.json":
        return _hits_from_prime_connections(path)
    data = _read_hermes_yaml(path) if path.suffix == ".yaml" else _read_json_object(path)
    if data is None:
        return []
    return _hits_from_mcp_servers(servers=_mcp_servers(data), harness=harness, config_path=path)


def mcp_config_candidate_paths(
    *, home: Path | None = None, cwd: Path | None = None
) -> tuple[tuple[str, Path], ...]:
    """Return (harness, path) pairs probed for MCP server registries.

    Each file appears once (first harness label wins), so a shared file such as
    ``~/.mcp.json`` when ``cwd`` is home is not counted twice.
    """

    root = Path(home) if home is not None else Path.home()
    work = Path(cwd) if cwd is not None else Path.cwd()
    # Env overrides (CLAUDE_CONFIG_DIR, PRIME_AGENT_CODING_AGENT_DIR, HERMES_HOME) apply
    # only to live discovery; an explicit home stays hermetic.
    if home is None:
        prime_dir = default_prime_agent_dir()
        hermes_dir = resolve_hermes_paths().config.parent
    else:
        prime_dir = root / ".prime" / "agent"
        hermes_dir = root / ".hermes"
    # Claude Code global MCP registry lives in ~/.claude.json (not under CLAUDE_HOME).
    paths: list[tuple[str, Path]] = [
        ("claude", root / ".claude.json"),
        ("cursor", root / ".cursor" / "mcp.json"),
        ("codex", root / ".codex" / "config.toml"),
        ("prime", prime_dir / "settings.json"),
        ("prime", prime_dir / "mcp-connections.json"),
        ("hermes", hermes_dir / "config.yaml"),
        ("hermes", hermes_dir / "config.json"),
        ("hermes", hermes_dir / "mcp.json"),
        ("project", work / ".mcp.json"),
        ("cursor-project", work / ".cursor" / "mcp.json"),
        ("global", root / ".mcp.json"),
    ]
    if home is None:
        claude_home = os.getenv("CLAUDE_CONFIG_DIR") or os.getenv("CLAUDE_HOME")
        if claude_home and claude_home.strip():
            claude_root = Path(claude_home.strip()).expanduser()
            paths.append(("claude", claude_root / "mcp.json"))
            paths.append(("claude", claude_root / ".mcp.json"))
    unique: dict[Path, tuple[str, Path]] = {}
    for harness, path in paths:
        unique.setdefault(path.resolve(), (harness, path))
    return tuple(unique.values())


def discover_mcp_provider_configs(
    *, home: Path | None = None, cwd: Path | None = None
) -> tuple[McpConfigHit, ...]:
    """Scan supported harness config locations for known intelligence/docs MCPs."""

    found: list[McpConfigHit] = []
    seen: set[tuple[str, str]] = set()
    for harness, path in mcp_config_candidate_paths(home=home, cwd=cwd):
        if not path.is_file():
            continue
        for hit in _hits_from_config(harness=harness, path=path):
            # Two aliases of one provider in one file (serena + serena-mcp) count once.
            key = (hit.provider_id, hit.config_path)
            if key in seen:
                continue
            seen.add(key)
            found.append(hit)
    return tuple(sorted(found, key=lambda item: (item.provider_id, item.harness, item.config_path)))


def group_mcp_hits_by_provider(hits: Sequence[McpConfigHit]) -> dict[str, tuple[McpConfigHit, ...]]:
    """Group hits by catalog provider_id (stable config_path order)."""

    grouped: dict[str, list[McpConfigHit]] = {}
    for hit in hits:
        grouped.setdefault(hit.provider_id, []).append(hit)
    return {
        provider_id: tuple(sorted(items, key=lambda item: (item.harness, item.config_path)))
        for provider_id, items in grouped.items()
    }


__all__ = [
    "McpConfigHit",
    "discover_mcp_provider_configs",
    "group_mcp_hits_by_provider",
    "mcp_config_candidate_paths",
    "resolve_provider_id",
]
