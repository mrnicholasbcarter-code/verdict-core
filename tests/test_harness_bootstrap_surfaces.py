"""Offline injected bootstrap integration. No operator-home writes or live calls."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from tests.test_verified_models_projection import NOW, at_rest_entry, conn, health_cache, row
from verdict.actions.harness_bootstrap import PrimeReadAdapter
from verdict.actions.registry import get_action, run_action
from verdict.actions.verified_models import StorePaths, VerifiedSnapshotAdapter
from verdict.harness_prime_compat import PrimeCompatibilityContext
from verdict.harness_prime_selection import PrimeDependencies, SelectionPaths
from verdict.orchestration.verified_refresh import RefreshConfig
from verdict.tui_bootstrap_controls import consume_prime, parse_bootstrap_args

IDS = ("cc/sonnet", "cc/opus")
GATEWAY = "http://127.0.0.1:20128"
SECRET = "sk-private-surface-secret"


@pytest.fixture
def fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / "verdict"))
    monkeypatch.setenv("PRIME_AGENT_CODING_AGENT_DIR", str(tmp_path / "prime"))
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude"))
    monkeypatch.setattr("verdict.actions.verified_models.active_controller_route", lambda: None)
    agent = tmp_path / "prime"
    agent.mkdir(mode=0o700)
    paths = SelectionPaths(agent)
    old = json.dumps(
        {"enabledModels": ["cc/old"], "defaultModel": "cc/old", "secret": SECRET}
    ).encode()
    paths.settings.write_bytes(old)
    paths.settings.chmod(0o600)
    registry = {
        "providers": {
            "omniroute": {
                "api": "openai-completions",
                "baseUrl": GATEWAY + "/v1",
                "apiKey": SECRET,
                "models": [{"id": rid, "name": rid} for rid in (*IDS, "cc/old")],
            }
        }
    }
    paths.models.write_text(json.dumps(registry))
    paths.models.chmod(0o600)
    ctx = PrimeCompatibilityContext(
        agent, True, "0.9.8", "fixture-digest", GATEWAY + "/v1", "health_cache", True
    )

    def dependencies(_self: Any) -> PrimeDependencies:
        return PrimeDependencies(paths.models.read_bytes(), ctx)

    monkeypatch.setattr(PrimeReadAdapter, "dependencies", dependencies)
    store = StorePaths.defaults(tmp_path / "verdict")
    store.health_cache.parent.mkdir()
    store.health_cache.write_text(
        json.dumps(
            health_cache(
                *(at_rest_entry(rid, checked_at=NOW - timedelta(seconds=30)) for rid in IDS)
            )
        )
    )
    adapter = VerifiedSnapshotAdapter(
        GATEWAY, store, [row(rid) for rid in IDS], [conn("cc")], clock=lambda: NOW
    )
    calls: list[str] = []

    def refresh(snapshot: Any, **kwargs: Any) -> Any:
        from verdict.orchestration.verified_refresh import refresh_for_consumer

        calls.append(kwargs["consumer"])
        assert kwargs["needed_ids"] == list(IDS)
        assert {r.route_id for r in snapshot.rows} == set(IDS)
        return refresh_for_consumer(snapshot, **kwargs)

    return {
        "reader": PrimeReadAdapter(paths, GATEWAY),
        "adapter": adapter,
        "old": old,
        "calls": calls,
        "run_refresh": refresh,
        "paths": paths,
        "clock": lambda: NOW,
        "config": RefreshConfig(auto_refresh=False),
    }


def consume(fixture: dict[str, Any], text: str = "prime cc/sonnet cc/opus", **kwargs: Any) -> Any:
    keys = ("reader", "adapter", "run_refresh", "clock", "config")
    return consume_prime(
        parse_bootstrap_args(text), gateway=GATEWAY, **{key: fixture[key] for key in keys}, **kwargs
    )


def test_preview_and_cancel_no_settings_or_backups(fixture: dict[str, Any]) -> None:
    before = {p.name: p.read_bytes() for p in fixture["paths"].agent_dir.iterdir()}
    for kwargs in (
        {"preview_only": True},
        {"read_line": lambda _p: ""},
        {"read_line": lambda _p: None},
    ):
        result = consume(fixture, **kwargs)
        assert result.ok
        assert fixture["paths"].settings.read_bytes() == fixture["old"]
        assert {p.name: p.read_bytes() for p in fixture["paths"].agent_dir.iterdir()} == before
        assert SECRET not in json.dumps(result.data)


def test_multi_apply_restore_and_refresh_order(fixture: dict[str, Any]) -> None:
    result = consume(fixture, read_line=lambda _p: "y")
    assert result.data["status"] == "applied", result.data
    assert fixture["calls"] == ["picker", "selection"]
    assert json.loads(fixture["paths"].settings.read_bytes())["enabledModels"] == list(IDS)
    restored = consume(fixture, "prime restore", read_line=lambda _p: "y")
    assert restored.data["status"] == "restored", restored.data
    assert fixture["paths"].settings.read_bytes() == fixture["old"]


@pytest.mark.parametrize("age", [900, None])
def test_stale_unverified_refused(fixture: dict[str, Any], age: int | None) -> None:
    path = fixture["adapter"].paths.health_cache
    if age is None:
        path.unlink()
    else:
        path.write_text(
            json.dumps(
                health_cache(
                    *(at_rest_entry(rid, checked_at=NOW - timedelta(seconds=age)) for rid in IDS)
                )
            )
        )
    result = consume(fixture, read_line=lambda _p: "y")
    assert not result.ok
    assert result.data["refusals"]
    assert fixture["paths"].settings.read_bytes() == fixture["old"]


def test_concurrent_config_mutation_refused(fixture: dict[str, Any]) -> None:
    def answer(_prompt: str) -> str:
        fixture["paths"].settings.write_bytes(fixture["old"] + b" ")
        return "y"

    result = consume(fixture, read_line=answer)
    assert not result.ok
    assert result.data["reasons"] == ["config_changed"]
    assert not list(fixture["paths"].agent_dir.glob("*.bak"))


@pytest.mark.parametrize(
    "text",
    [
        "prime --probe",
        "prime cc/a,--probe",
        "claude apply",
        "claude cc/a mode=bad",
        "prime mode=native",
    ],
)
def test_invalid_bootstrap_tokens_refused_before_io(text: str) -> None:
    with pytest.raises(ValueError):
        parse_bootstrap_args(text)


def test_claude_read_only_and_no_apply_action(fixture: dict[str, Any], tmp_path: Path) -> None:
    from verdict.harness_claude import DiscoverReport, StatusReport

    claude = tmp_path / "claude"
    claude.mkdir()
    config = claude / "settings.json"
    config.write_text(json.dumps({"secret": SECRET}))
    discovery = DiscoverReport(True, "/fixture/claude", config, True, True, None, True, False, True)
    status = StatusReport(
        True, "verdict", None, "FIXTURE_TOKEN", True, config, True, True, "openai-compatible"
    )
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = run_action(
        "harness.claude.compat",
        {
            "selected_ids": IDS,
            "projection_rows": fixture["adapter"]
            .load(
                __import__(
                    "verdict.orchestration.verified_models", fromlist=["VerifiedModelQuery"]
                ).VerifiedModelQuery()
            )
            .to_dict()["rows"],
            "discovery": discovery,
            "status": status,
            "claude_home": claude,
            "now": NOW,
        },
    )
    assert result.ok
    assert result.data["apply_available"] is False
    assert all(r["native"]["compatibility"] == "unsupported" for r in result.data["rows"])
    assert "BOD-102" in result.data["needs_owner"]
    assert SECRET not in json.dumps(result.data)
    assert {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
    assert get_action("harness.claude.select.apply") is None


def test_cli_json_noninteractive_is_preview_only(fixture: dict[str, Any], capsys: Any) -> None:
    from verdict.cli import cmd_harness_bootstrap

    real = consume_prime

    def injected(args: Any, **kwargs: Any) -> Any:
        kwargs.update(
            {key: fixture[key] for key in ("reader", "adapter", "run_refresh", "clock", "config")}
        )
        kwargs["live"] = False
        return real(args, **kwargs)

    with (
        patch("verdict.tui_bootstrap_controls.consume_prime", injected),
        patch("sys.stdin.isatty", return_value=False),
    ):
        cmd_harness_bootstrap("prime", "select", ids=list(IDS), output_json=True)
    streams = capsys.readouterr()
    assert json.loads(streams.out)["selected_ids"] == list(IDS)
    assert "Bounded prepaid" in streams.err
    assert fixture["paths"].settings.read_bytes() == fixture["old"]


def test_parser_new_commands_and_legacy_eligibility(fixture: dict[str, Any]) -> None:
    import argparse

    from verdict.commands import parsers_harness
    from verdict.orchestration import cli as orchestration_cli

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    parsers_harness.register(subparsers)
    orchestration_cli.add_parsers(subparsers)
    for argv in (
        ["harness", "prime", "select", "cc/a", "--preview", "--json"],
        ["harness", "prime", "restore", "--json"],
        ["harness", "claude", "compat", "cc/a", "--mode", "native", "--json"],
        ["harness", "prime", "sync-models", "--dry-run"],
    ):
        args = parser.parse_args(argv)
        assert args.command == "harness"
    legacy = parser.parse_args(["eligibility", "--json"])
    assert not legacy.verified
