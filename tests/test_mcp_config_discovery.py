"""Tests for harness MCP config import into capability bootstrap (BOD-124)."""

from __future__ import annotations

import json
from pathlib import Path

from verdict.capability_bootstrap import BootstrapMode, discover_providers, run_bootstrap
from verdict.mcp_config_discovery import discover_mcp_provider_configs, resolve_provider_id


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _fixture_home_with_mcp(tmp_path: Path) -> tuple[Path, Path]:
    """Claude + Cursor + Codex configs naming Serena / Context7 / Codebase Memory.

    Bodies include fake env secrets that must never appear in discovery output.
    """

    home = tmp_path / "home"
    cwd = tmp_path / "project"
    cwd.mkdir(parents=True)

    secret = "sk-test-SHOULD-NOT-LEAK"

    _write_json(
        home / ".claude.json",
        {
            "mcpServers": {
                "serena": {
                    "command": "serena",
                    "args": ["start-mcp-server"],
                    "env": {"SERENA_TOKEN": secret},
                },
                "context7": {
                    "command": "npx",
                    "args": ["-y", "@upstash/context7-mcp"],
                    "env": {"CONTEXT7_API_KEY": secret},
                },
            }
        },
    )
    _write_json(
        home / ".cursor" / "mcp.json",
        {
            "mcpServers": {
                "codebase-memory-mcp": {
                    "command": "/opt/codebase-memory-mcp",
                    "args": [],
                    "env": {"CBM_TOKEN": secret},
                },
                "serena": {"command": "uvx", "args": ["serena", "start-mcp-server"]},
            }
        },
    )
    codex = home / ".codex"
    codex.mkdir(parents=True)
    (codex / "config.toml").write_text(
        "\n".join(
            [
                "[mcp_servers.serena]",
                'command = "serena"',
                'args = ["start-mcp-server"]',
                "",
                "[mcp_servers.context7]",
                'command = "context7"',
                f'env = {{ API_KEY = "{secret}" }}',
                "",
                "[mcp_servers.codebase-memory-mcp]",
                'command = "/opt/codebase-memory-mcp"',
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return home, cwd


def test_resolve_provider_id_aliases() -> None:
    assert resolve_provider_id("serena") == "adapter.serena_lsp"
    assert resolve_provider_id("codebase-memory-mcp") == "adapter.codebase_memory"
    assert resolve_provider_id("context7") == "adapter.context7"
    assert resolve_provider_id("unknown-mcp") is None


def test_discover_mcp_configs_from_fixture_dirs(tmp_path: Path) -> None:
    home, cwd = _fixture_home_with_mcp(tmp_path)
    hits = discover_mcp_provider_configs(home=home, cwd=cwd)
    by_provider = {hit.provider_id for hit in hits}
    assert by_provider == {"adapter.serena_lsp", "adapter.context7", "adapter.codebase_memory"}
    harnesses = {hit.harness for hit in hits}
    assert {"claude", "cursor", "codex"} <= harnesses

    blob = " ".join(f"{hit.config_path}:{hit.server_name}:{hit.harness}" for hit in hits)
    assert "sk-test-SHOULD-NOT-LEAK" not in blob
    assert "SERENA_TOKEN" not in blob
    assert "CONTEXT7_API_KEY" not in blob
    assert "CBM_TOKEN" not in blob
    for hit in hits:
        assert hit.config_path
        assert "sk-test" not in repr(hit)


def test_discover_providers_marks_mcp_configured_not_healthy(tmp_path: Path) -> None:
    home, cwd = _fixture_home_with_mcp(tmp_path)
    providers = discover_providers(
        path_resolver=lambda _name: None,
        probe_gateway=lambda _pid: {"reachable": False, "health_ok": False},
        home=home,
        cwd=cwd,
    )
    by_id = {item.provider_id: item for item in providers}
    for provider_id in ("adapter.serena_lsp", "adapter.context7", "adapter.codebase_memory"):
        item = by_id[provider_id]
        assert item.lifecycle.value == "configured"
        assert item.health_state != "healthy"
        assert item.qualification_state != "qualified"
        assert item.config_sources
        assert item.path_or_endpoint is None
        joined = " ".join(item.config_sources)
        assert "sk-test-SHOULD-NOT-LEAK" not in joined
        assert "SERENA_TOKEN" not in joined


def test_recommend_does_not_duplicate_install_when_mcp_configured(tmp_path: Path) -> None:
    home, cwd = _fixture_home_with_mcp(tmp_path)
    report = run_bootstrap(
        mode=BootstrapMode.RECOMMENDED,
        non_interactive=True,
        path_resolver=lambda _name: None,
        probe_gateway=lambda _pid: {"reachable": False, "health_ok": False},
        home=home,
        cwd=cwd,
    ).to_dict()

    install_targets = {
        action["provider_id"]
        for action in report["plan"]["actions"]
        if action["kind"] == "install_provider"
    }
    assert "adapter.serena_lsp" not in install_targets
    assert "adapter.context7" not in install_targets
    assert "adapter.codebase_memory" not in install_targets

    by_id = {item["provider_id"]: item for item in report["providers"]}
    assert by_id["adapter.serena_lsp"]["lifecycle"] == "configured"
    assert by_id["adapter.context7"]["lifecycle"] == "configured"
    assert by_id["adapter.codebase_memory"]["lifecycle"] == "configured"


def test_empty_home_does_not_import_host_mcp(tmp_path: Path) -> None:
    """Hermetic empty home must not pick up the developer's real MCP configs."""

    home = tmp_path / "empty-home"
    home.mkdir()
    cwd = tmp_path / "empty-cwd"
    cwd.mkdir()
    hits = discover_mcp_provider_configs(home=home, cwd=cwd)
    assert hits == ()
    providers = discover_providers(path_resolver=lambda _name: None, home=home, cwd=cwd)
    by_id = {item.provider_id: item for item in providers}
    assert by_id["adapter.serena_lsp"].lifecycle.value == "not_installed"
    assert by_id["adapter.context7"].lifecycle.value == "not_installed"
    assert by_id["adapter.codebase_memory"].lifecycle.value == "not_installed"
