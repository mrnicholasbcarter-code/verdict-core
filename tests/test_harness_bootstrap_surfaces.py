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
ORIGINAL_DEPENDENCIES = PrimeReadAdapter.dependencies


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


def test_consume_prime_preview_with_unsafe_registry_row_binds_exact_id(
    fixture: dict[str, Any],
) -> None:
    """Defect 1 (d): end-to-end consume_prime preview against a registry that
    contains an unsafe-spaced row (real-registry shape) still previews the
    exact VERIFIED ids successfully; no live calls, injected rows only.
    """
    registry = json.loads(fixture["paths"].models.read_text())
    registry["providers"]["omniroute"]["models"].append(
        {"id": "aihorde/A-Zovya RPG Inpainting", "name": "aihorde/A-Zovya RPG Inpainting"}
    )
    fixture["paths"].models.write_text(json.dumps(registry))
    result = consume(fixture, preview_only=True)
    assert result.ok, result.data
    assert result.data["rows"][0]["selectable"]
    assert "A-Zovya" not in json.dumps(result.data)


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


@pytest.mark.parametrize("capacity,consent", [("metered", "y"), ("unknown", "y"), ("metered", "n")])
def test_separate_spend_consent_executes_exact_ids_then_reloads(
    fixture: dict[str, Any], capacity: str, consent: str
) -> None:
    from verdict.prove_at_rest import ProbeExchange

    adapter = fixture["adapter"]
    adapter.inventory_rows = [row(rid, capacity_class=capacity) for rid in IDS]
    adapter.connections = [conn("cc", auth="api_key", plan="")]
    adapter.paths.health_cache.unlink()
    calls = []

    def transport(rid: str, phase: str, timeout: float) -> ProbeExchange:
        calls.append((rid, phase))
        return ProbeExchange(
            http_status=200,
            ok=True,
            chat_exact=True,
            tool_called=False,
            reported_model=rid,
            latency_ms=1,
        )

    prompts = []

    def answer(prompt: str) -> str:
        prompts.append(prompt)
        return consent

    result = consume(fixture, read_line=answer, transport=transport, preview_only=True)
    if consent == "y":
        assert sorted(calls) == sorted((rid, "chat") for rid in IDS)
        assert result.ok, result.data
        assert all(item["status"] == "VERIFIED" for item in result.data["rows"])
        assert all(not item["coding_ok"] for item in result.data["rows"])
    else:
        assert not calls
        assert not result.ok
    assert len(prompts) == 1 and "manual refresh plan" in prompts[0]
    assert fixture["paths"].settings.read_bytes() == fixture["old"]


def test_read_only_prime_discovery_supported_release_no_helper_execution(
    fixture: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Undo only the fixture reader override; all real files remain temporary.
    from verdict.actions.harness_bootstrap import _credentials

    assert _credentials({"apiKey": "!secret-helper"}) is None
    assert _credentials({"headers": {"Authorization": SECRET}}) is None
    binary = tmp_path / "releases" / "0.9.8-linux-fixture" / "prime-agent"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"fixture-binary")
    monkeypatch.setattr("verdict.harness_prime.shutil.which", lambda _s: str(binary))
    import verdict.actions.harness_bootstrap as module

    # Original function captured before monkeypatch is restored via fixture attribute below.
    with patch.object(PrimeReadAdapter, "dependencies", ORIGINAL_DEPENDENCIES):
        dependencies = module.PrimeReadAdapter(fixture["paths"], GATEWAY).dependencies()
    assert dependencies.discovery.installed
    assert dependencies.discovery.release == "0.9.8"
    assert dependencies.discovery.credentials_present is True
    assert dependencies.discovery.evidence_source == "health_cache"


def test_claude_omitted_scope_changed_digest_and_apply_input(
    fixture: dict[str, Any], tmp_path: Path
) -> None:
    fixture["paths"].settings.write_text(
        json.dumps({"enabledModels": ["cc/sonnet", "unknown/exact"]})
    )
    from verdict.orchestration.verified_models import VerifiedModelQuery

    rows = fixture["adapter"].load(VerifiedModelQuery()).to_dict()["rows"]
    result = run_action(
        "harness.claude.compat",
        {
            "prime_paths": fixture["paths"],
            "projection_rows": rows,
            "start_digest": "a" * 64,
            "end_digest": "b" * 64,
            "now": NOW,
        },
    )
    assert result.ok and result.data["selected_ids"] == ["cc/sonnet"]
    assert "Prime scope" in result.data["selected_ids_source"]
    assert result.data["config_changed"] is True
    refused = run_action(
        "harness.claude.compat", {"selected_ids": ["apply"], "projection_rows": rows, "now": NOW}
    )
    assert not refused.ok


def test_sync_separate_confirmation_does_not_invoke_picker(
    fixture: dict[str, Any], capsys: Any
) -> None:
    from verdict.actions.base import ActionResult
    from verdict.cli import cmd_harness_bootstrap

    with (
        patch(
            "verdict.actions.registry.run_action",
            return_value=ActionResult(data={"written": False}),
        ) as action,
        patch("sys.stdin.isatty", return_value=False),
    ):
        cmd_harness_bootstrap("prime", "sync-models", output_json=True)
        action.assert_not_called()
        assert json.loads(capsys.readouterr().out)["written"] is False
        cmd_harness_bootstrap("prime", "sync-models", dry_run=True, output_json=True)
        assert action.call_args.args == ("harness.prime.sync-models", {"dry_run": True})


def test_guided_exact_picker_uses_same_preview(fixture: dict[str, Any]) -> None:
    result = consume(fixture, "prime", read_line=lambda _p: ",".join(IDS), preview_only=True)
    assert result.ok
    assert result.data["selected_ids"] == list(IDS)
    assert fixture["paths"].settings.read_bytes() == fixture["old"]


def test_cancelled_refresh_never_promotes_cached_proof(fixture: dict[str, Any]) -> None:
    def cancelled(snapshot: Any, **kwargs: Any) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(outcome="cancelled")

    fixture["run_refresh"] = cancelled
    result = consume(fixture, read_line=lambda _p: "y")
    assert not result.ok
    assert fixture["paths"].settings.read_bytes() == fixture["old"]
    assert not list(fixture["paths"].agent_dir.glob("*.bak"))


def test_stale_manual_plan_changed_during_consent_has_zero_calls(fixture: dict[str, Any]) -> None:
    from verdict.prove_at_rest import ProbeExchange

    adapter = fixture["adapter"]
    adapter.inventory_rows = [row(rid, capacity_class="metered") for rid in IDS]
    adapter.connections = [conn("cc", auth="api_key", plan="")]
    adapter.paths.health_cache.unlink()
    calls = []

    def transport(rid: str, phase: str, timeout: float) -> ProbeExchange:
        calls.append(rid)
        raise AssertionError("stale plan dispatch")

    def answer(_prompt: str) -> str:
        adapter.paths.health_cache.write_text(json.dumps(health_cache()))
        return "y"

    result = consume(fixture, read_line=answer, transport=transport, preview_only=True)
    assert not calls and not result.ok
    assert fixture["paths"].settings.read_bytes() == fixture["old"]


def test_prepaid_automatic_refresh_reloads_before_preview(fixture: dict[str, Any]) -> None:
    from verdict.prove_at_rest import ProbeExchange

    fixture["config"] = RefreshConfig(auto_refresh=True)
    fixture["adapter"].paths.health_cache.unlink()
    calls = []

    def transport(rid: str, phase: str, timeout: float) -> ProbeExchange:
        calls.append((rid, phase))
        return ProbeExchange(
            http_status=200,
            ok=True,
            chat_exact=phase == "chat",
            tool_called=phase == "tool",
            reported_model=rid,
            latency_ms=1,
        )

    result = consume(
        fixture,
        transport=transport,
        preview_only=True,
        read_line=lambda _p: pytest.fail("automatic prepaid refresh must not spend-prompt"),
    )
    assert result.ok
    assert sorted(calls) == sorted((rid, phase) for rid in IDS for phase in ("chat", "tool"))
    assert all(row["status"] == "VERIFIED" for row in result.data["rows"])
    assert fixture["paths"].settings.read_bytes() == fixture["old"]


def test_expired_original_preview_refuses_after_confirmation(fixture: dict[str, Any]) -> None:
    clock = [NOW]
    fixture["clock"] = lambda: clock[0]

    def answer(_prompt: str) -> str:
        clock[0] = NOW + timedelta(minutes=10)
        return "y"

    result = consume(fixture, read_line=answer)
    assert not result.ok
    assert fixture["paths"].settings.read_bytes() == fixture["old"]
    assert "proof_deadline_elapsed" in str(result.data)


def test_verified_cli_publishes_after_consumer_and_legacy_json_unchanged(
    fixture: dict[str, Any], tmp_path: Path, capsys: Any
) -> None:
    import argparse

    from verdict.cli import cmd_verified_completion_view
    from verdict.orchestration import cli as orchestration_cli

    parser = argparse.ArgumentParser()
    orchestration_cli.add_parsers(parser.add_subparsers(dest="command"))
    args = parser.parse_args(
        ["eligibility", "--verified", "--no-refresh", "--json", "--gateway", GATEWAY]
    )
    adapter = fixture["adapter"]
    timeline = []

    def consumer(**kwargs: Any) -> Any:
        from verdict.actions.base import ActionResult
        from verdict.orchestration.verified_models import VerifiedModelQuery

        timeline.append("consumer-done")
        final = adapter.load(VerifiedModelQuery()).to_dict()
        kwargs["on_projection"](final, adapter)
        return ActionResult(data=final)

    def publisher(view: Any) -> None:
        timeline.append("publish")
        assert len(view["rows"]) == 2

    with (
        patch("verdict.actions.verified_models.VerifiedSnapshotAdapter", return_value=adapter),
        patch("verdict.actions.verified_models.consume_verified_models", consumer),
        patch("verdict.tui_completion_snapshot.publish_snapshot", publisher),
    ):
        assert cmd_verified_completion_view(args) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["schema"] == "verdict.verified-models/v1"
    assert timeline == ["consumer-done", "publish"]
    legacy = parser.parse_args(["eligibility", "--json"])
    from verdict.actions.base import ActionResult

    payload = {"schema": "legacy-fixture", "working_models": [], "counts": {"untested": 3}}
    with patch(
        "verdict.actions.registry.run_action", return_value=ActionResult(data=payload)
    ) as action:
        assert orchestration_cli._eligibility(legacy) == 0
    assert json.loads(capsys.readouterr().out) == payload
    assert action.call_args.args[0] == "eligibility"


def test_unsafe_agent_dir_refusal_is_actionable(fixture: dict[str, Any]) -> None:
    """A group-writable agent dir refuses with the code, a ~-relative path and a
    `chmod go-w` hint, not the old generic "unsafe, changed or missing" text.
    """
    agent_dir = fixture["paths"].agent_dir
    original_mode = agent_dir.stat().st_mode
    agent_dir.chmod(0o775)
    try:
        result = consume(fixture, preview_only=True)
    finally:
        agent_dir.chmod(original_mode)
    assert not result.ok
    message = result.data["error"]
    assert "unsafe_directory" in message
    assert "chmod go-w" in message
    assert "~/prime" in message
    assert SECRET not in message
    assert str(agent_dir) not in message


def _refusal_message(path: Path, code: str = "unsafe_directory") -> str:
    from verdict.harness_prime_selection import PrimeSelectionError
    from verdict.tui_bootstrap_controls import _actionable_refusal

    result = _actionable_refusal(PrimeSelectionError(code, path=path))
    assert not result.ok
    message = result.data["error"]
    assert isinstance(message, str)
    return message


def test_actionable_refusal_sanitizes_newline_in_path_name(tmp_path: Path) -> None:
    """A directory name containing a newline must never forge extra lines."""
    message = _refusal_message(tmp_path / "evil\nname")
    assert "\n" not in message
    assert "inspect this directory's permissions" in message
    assert "chmod" not in message


def test_actionable_refusal_sanitizes_escape_sequence_in_path_name(tmp_path: Path) -> None:
    """A directory name containing ESC must never inject an ANSI escape."""
    message = _refusal_message(tmp_path / "evil\x1b[31mname")
    assert "\x1b" not in message
    assert "inspect this directory's permissions" in message
    assert "chmod" not in message


def test_actionable_refusal_sanitizes_outside_home_control_chars(tmp_path: Path) -> None:
    """An outside-HOME path is basename-only, but control chars in the
    basename itself must still sanitize."""
    message = _refusal_message(Path("/elsewhere/evil\x07name"))
    assert "\x07" not in message
    assert "inspect this directory's permissions" in message
    assert "chmod" not in message


def test_actionable_refusal_home_itself_renders_as_tilde(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``$HOME`` itself (the empty relative path) renders as bare ``~``."""
    monkeypatch.setenv("HOME", str(tmp_path))
    message = _refusal_message(tmp_path)
    assert "~ is group/other-writable" in message
    assert "chmod go-w" in message
    assert "'~'" in message
    assert str(tmp_path) not in message


def test_actionable_refusal_normal_path_still_gives_chmod_hint(tmp_path: Path) -> None:
    """A normal, printable path name still gets the actionable ``chmod`` hint."""
    message = _refusal_message(tmp_path / "normal-dir" / "nested")
    assert "chmod go-w" in message
    assert "nested" in message
