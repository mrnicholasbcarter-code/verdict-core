"""Tests for harness MCP config import into capability bootstrap."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from verdict.capability_bootstrap import BootstrapMode, discover_providers, run_bootstrap
from verdict.mcp_config_discovery import (
    discover_mcp_provider_configs,
    mcp_config_candidate_paths,
    resolve_provider_id,
)


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


def _fixture_prime_hermes_home(tmp_path: Path) -> tuple[Path, Path]:
    """Prime + Hermes configs naming the same providers as each other and Claude."""

    home = tmp_path / "home"
    cwd = tmp_path / "project"
    cwd.mkdir(parents=True)
    secret = "sk-test-SHOULD-NOT-LEAK"

    _write_json(
        home / ".claude.json",
        {"mcpServers": {"serena": {"command": "serena", "env": {"SERENA_TOKEN": secret}}}},
    )
    prime = home / ".prime" / "agent"
    _write_json(
        prime / "settings.json",
        {
            "defaultModel": "x",
            "mcpServers": {
                "serena": {
                    "type": "stdio",
                    "command": "/opt/bin/serena",
                    "args": ["start-mcp-server", f"--token={secret}"],
                    "env": {"SERENA_TOKEN": secret},
                },
                # Second alias of the same provider in the same file counts once.
                "serena-mcp": {"type": "stdio", "command": "/opt/bin/serena"},
                "context7": {"type": "http", "url": f"https://mcp.context7.com/mcp?key={secret}"},
                "playwright": {"type": "stdio", "command": "npx"},
            },
        },
    )
    _write_json(
        prime / "mcp-connections.json",
        {
            "version": 1,
            "connections": {
                "docs": {
                    "serviceId": "context7",
                    "endpoint": f"https://mcp.context7.com/mcp?key={secret}",
                    "status": "connected",
                },
                "linear": {"serviceId": "linear", "endpoint": "https://mcp.linear.app/mcp"},
            },
        },
    )
    hermes = home / ".hermes"
    hermes.mkdir(parents=True)
    (hermes / "config.yaml").write_text(
        "\n".join(
            [
                "model: hermes-default",
                "mcp_servers:",
                "  serena:",
                "    command: /opt/bin/serena",
                "    args:",
                "      - start-mcp-server",
                "# comment inside the block",
                "  omniroute:",
                "    url: http://localhost:20128/api/mcp/stream",
                "    headers:",
                f"      Authorization: Bearer {secret}",
                "providers:",
                f"  openai: {{api_key: {secret}}}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    _write_json(
        hermes / "mcp.json",
        # A shell-line command (may carry inline secrets) is not reported.
        {"mcpServers": {"codebase-memory-mcp": {"command": f"codebase-memory-mcp --key {secret}"}}},
    )
    _write_json(
        hermes / "config.json",
        # Unknown transport values are dropped, never copied.
        {"mcpServers": {"context7-mcp": {"transport": f"Bearer {secret}", "url": "https://x"}}},
    )
    return home, cwd


def test_discover_prime_and_hermes_configs(tmp_path: Path) -> None:
    home, cwd = _fixture_prime_hermes_home(tmp_path)
    hits = discover_mcp_provider_configs(home=home, cwd=cwd)
    prime = home / ".prime" / "agent"
    hermes = home / ".hermes"

    rows = {
        (hit.provider_id, hit.harness, hit.config_path, hit.transport, hit.command) for hit in hits
    }
    assert rows == {
        ("adapter.serena_lsp", "claude", str(home / ".claude.json"), "stdio", "serena"),
        ("adapter.serena_lsp", "prime", str(prime / "settings.json"), "stdio", "/opt/bin/serena"),
        ("adapter.context7", "prime", str(prime / "settings.json"), "http", None),
        ("adapter.context7", "prime", str(prime / "mcp-connections.json"), "http", None),
        ("adapter.serena_lsp", "hermes", str(hermes / "config.yaml"), "stdio", "/opt/bin/serena"),
        ("adapter.codebase_memory", "hermes", str(hermes / "mcp.json"), "stdio", None),
        ("adapter.context7", "hermes", str(hermes / "config.json"), None, None),
    }
    assert len(hits) == len(rows)
    blob = repr(hits)
    assert "sk-test-SHOULD-NOT-LEAK" not in blob
    assert "context7.com" not in blob
    assert "SERENA_TOKEN" not in blob


def test_prime_hermes_bootstrap_has_no_duplicate_providers_or_actions(tmp_path: Path) -> None:
    home, cwd = _fixture_prime_hermes_home(tmp_path)

    def run() -> dict[str, Any]:
        return run_bootstrap(
            mode=BootstrapMode.RECOMMENDED,
            non_interactive=True,
            path_resolver=lambda _name: None,
            probe_gateway=lambda _pid: {"reachable": False, "health_ok": False},
            home=home,
            cwd=cwd,
        ).to_dict()

    first, second = run(), run()
    assert first["plan"] == second["plan"]
    assert first["providers"] == second["providers"]

    provider_ids = [item["provider_id"] for item in first["providers"]]
    assert len(provider_ids) == len(set(provider_ids))
    actions = first["plan"]["actions"]
    action_keys = [(action["provider_id"], action["kind"]) for action in actions]
    assert len(action_keys) == len(set(action_keys))
    configured = {"adapter.serena_lsp", "adapter.context7", "adapter.codebase_memory"}
    assert (
        not {action["provider_id"] for action in actions if action["kind"] == "install_provider"}
        & configured
    )

    by_id = {item["provider_id"]: item for item in first["providers"]}
    serena_sources = by_id["adapter.serena_lsp"]["config_sources"]
    assert serena_sources == [
        str(home / ".claude.json"),
        str(home / ".hermes" / "config.yaml"),
        str(home / ".prime" / "agent" / "settings.json"),
    ]
    for provider_id in configured:
        assert by_id[provider_id]["lifecycle"] == "configured"


def test_live_discovery_honors_prime_and_hermes_dir_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prime_dir = tmp_path / "custom-prime"
    hermes_dir = tmp_path / "custom-hermes"
    monkeypatch.setenv("PRIME_AGENT_CODING_AGENT_DIR", str(prime_dir))
    monkeypatch.setenv("HERMES_HOME", str(hermes_dir))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    _write_json(prime_dir / "settings.json", {"mcpServers": {"serena": {"command": "serena"}}})
    _write_json(hermes_dir / "mcp.json", {"mcpServers": {"context7": {"url": "https://x"}}})

    paths = {path for _harness, path in mcp_config_candidate_paths(cwd=tmp_path)}
    assert prime_dir / "settings.json" in paths
    assert hermes_dir / "mcp.json" in paths
    assert {hit.harness for hit in discover_mcp_provider_configs(cwd=tmp_path)} == {
        "prime",
        "hermes",
    }

    # An explicit home stays hermetic: env overrides are ignored.
    hermetic = {path for _harness, path in mcp_config_candidate_paths(home=tmp_path, cwd=tmp_path)}
    assert prime_dir / "settings.json" not in hermetic
    assert tmp_path / ".prime" / "agent" / "settings.json" in hermetic


def test_command_with_spaces_reported_only_as_existing_absolute_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    exe = tmp_path / "Program Files" / "serena"
    exe.parent.mkdir(parents=True)
    exe.write_text("", encoding="utf-8")
    _write_json(
        home / ".prime" / "agent" / "settings.json",
        {"mcpServers": {"serena": {"command": str(exe)}, "context7": {"command": "ctx --key k"}}},
    )
    hits = {
        hit.provider_id: hit.command for hit in discover_mcp_provider_configs(home=home, cwd=home)
    }
    assert hits == {"adapter.serena_lsp": str(exe), "adapter.context7": None}
