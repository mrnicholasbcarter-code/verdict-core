"""Discover existing MCP server entries from supported harness config files.

BOD-124 import/reuse: read config *paths* and server names only. Never copy
``env`` / tokens / secrets between tools. Config presence is not health.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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

_CODEX_MCP_SECTION = re.compile(r"(?m)^\[mcp_servers\.(?P<name>[^\]]+)\]\s*$")


@dataclass(frozen=True)
class McpConfigHit:
    """One MCP server name found in a harness config file (no secrets)."""

    provider_id: str
    server_name: str
    harness: str
    config_path: str


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
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _mcp_servers_from_json(data: Mapping[str, Any]) -> Mapping[str, Any]:
    servers = data.get("mcpServers")
    return servers if isinstance(servers, dict) else {}


def _hits_from_mcp_servers(
    *, servers: Mapping[str, Any], harness: str, config_path: Path
) -> list[McpConfigHit]:
    hits: list[McpConfigHit] = []
    for raw_name in servers:
        if not isinstance(raw_name, str):
            continue
        provider_id = resolve_provider_id(raw_name)
        if provider_id is None:
            continue
        # Intentionally ignore server body (command/args/env/url) — paths only.
        hits.append(
            McpConfigHit(
                provider_id=provider_id,
                server_name=raw_name,
                harness=harness,
                config_path=str(config_path),
            )
        )
    return hits


def _hits_from_json_file(*, path: Path, harness: str) -> list[McpConfigHit]:
    if not path.is_file():
        return []
    data = _read_json_object(path)
    if data is None:
        return []
    return _hits_from_mcp_servers(
        servers=_mcp_servers_from_json(data), harness=harness, config_path=path
    )


def _hits_from_codex_toml(path: Path) -> list[McpConfigHit]:
    """Parse Codex ``[mcp_servers.<name>]`` section headers only (no secret values)."""

    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
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


def mcp_config_candidate_paths(
    *, home: Path | None = None, cwd: Path | None = None
) -> tuple[tuple[str, Path], ...]:
    """Return (harness, path) pairs probed for MCP server registries."""

    root = Path(home) if home is not None else Path.home()
    work = Path(cwd) if cwd is not None else Path.cwd()
    # Claude Code global MCP registry lives in ~/.claude.json (not under CLAUDE_HOME).
    # When home is omitted (live discovery), also honor CLAUDE_CONFIG_DIR / CLAUDE_HOME.
    paths: list[tuple[str, Path]] = [
        ("claude", root / ".claude.json"),
        ("cursor", root / ".cursor" / "mcp.json"),
        ("codex", root / ".codex" / "config.toml"),
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
    return tuple(paths)


def discover_mcp_provider_configs(
    *, home: Path | None = None, cwd: Path | None = None
) -> tuple[McpConfigHit, ...]:
    """Scan supported harness config locations for known intelligence/docs MCPs."""

    found: list[McpConfigHit] = []
    seen: set[tuple[str, str, str]] = set()
    for harness, path in mcp_config_candidate_paths(home=home, cwd=cwd):
        if harness == "codex" and path.suffix == ".toml":
            hits = _hits_from_codex_toml(path)
        else:
            hits = _hits_from_json_file(path=path, harness=harness)
        for hit in hits:
            key = (hit.provider_id, hit.harness, hit.config_path)
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
