"""Real prompt_toolkit Document and key I/O, plus equivalent plain-loop help."""

from __future__ import annotations

import asyncio
import io
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from tests.test_verified_models_projection import NOW
from verdict import home
from verdict.terminal_ui import TerminalUI
from verdict.tui_completion import SCHEMA, CompletionSnapshot, VerdictCompleter
from verdict.tui_completion_snapshot import publish_snapshot


def snapshot() -> CompletionSnapshot:
    return CompletionSnapshot(
        SCHEMA,
        NOW.isoformat(),
        [{"run": "run-123", "outcome": "COMPLETE"}],
        [
            {
                "route_id": "cc/sonnet",
                "provider": "cc",
                "status": "VERIFIED",
                "checked_at": (NOW - timedelta(seconds=10)).isoformat(),
                "fresh_until": (NOW + timedelta(minutes=5)).isoformat(),
            }
        ],
        ["cc"],
        ["prime", "claude"],
        ["native", "openai-side-path"],
        [],
    )


def console() -> Console:
    return Console(file=io.StringIO(), width=180, force_terminal=False, no_color=True)


def test_palette_shared_grammar_and_actual_document_without_io() -> None:
    specs = home.command_specs()
    assert {row[1] for row in home.PALETTE} <= {spec.name for spec in specs}
    descriptions = {spec.name: spec.description for spec in specs}
    assert all(descriptions[cmd] == desc for _, cmd, desc, _ in home.PALETTE)
    completer = VerdictCompleter(specs, snapshot(), NOW)
    doc = Document("/bootstrap prime cc/ trailing", cursor_position=len("/bootstrap prime cc/"))
    with patch.object(Path, "read_bytes", side_effect=AssertionError("keystroke disk I/O")):
        results = list(completer.get_completions(doc, CompleteEvent(completion_requested=True)))
    assert results[0].text == "cc/sonnet"
    assert results[0].start_position == -3
    runs = list(completer.get_completions(Document("/trace run-"), CompleteEvent()))
    assert runs[0].text == "run-123"
    assert not list(
        completer.get_completions(Document("/credentials set SECRET "), CompleteEvent())
    )


@pytest.mark.parametrize(
    "line",
    ["/probe --probe", "/probe cc/a,--probe", "/bootstrap prime --probe", "/eligibility --probe"],
)
def test_pasted_options_never_dispatch(
    line: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    target = console()
    state = home.HomeState(gateway="http://127.0.0.1:9", completion_snapshot=snapshot())
    with (
        patch("verdict.home.run_palette_action") as action,
        patch("verdict.tui_bootstrap_controls.consume_prime") as prime,
    ):
        home._run_command(line, tui=TerminalUI(target), state=state, line_reader=lambda: "y")
    action.assert_not_called()
    prime.assert_not_called()
    assert (
        "invalid" in target.file.getvalue().lower() or "expects" in target.file.getvalue().lower()
    )


def test_fallback_help_and_typo_match_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path))
    target = console()
    state = home.HomeState(completion_snapshot=snapshot())
    commands = iter(["/help bootstrap", "/help eligibility", "/elegibility", "/quit"])
    monkeypatch.setattr("builtins.input", lambda _p="": next(commands))
    monkeypatch.setattr(home, "_save_history", lambda *_a: None)
    with patch("verdict.home.run_palette_action") as action:
        assert home._fallback_input_loop(target, TerminalUI(target), state) == 0
    action.assert_not_called()
    out = target.file.getvalue()
    assert "mode=native|openai-side-path" in out
    assert "provider=.." in out and "page=.." in out
    assert "did you mean /eligibility?" in out.lower()
    assert "snapshot" in out


def test_load_only_prompt_entry_and_after_explicit_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path))
    path = tmp_path / "verified-completion-snapshot.json"
    publish_snapshot({"generated_at": NOW.isoformat(), "rows": []}, path)
    state = home.HomeState(runs=[{"run": "run-123", "outcome": "COMPLETE"}])
    reads = []
    original = home._reload_completion

    def reload(current: home.HomeState) -> None:
        reads.append("load")
        original(current)

    monkeypatch.setattr(home, "_reload_completion", reload)
    lines = iter(["/help bootstrap", "/quit"])
    assert home._command_prompt(console(), state, line_reader=lambda: next(lines, None)) == 0
    assert reads == ["load", "load", "load"]  # entry and explicit commands only


@pytest.mark.asyncio
async def test_actual_prompt_keys_insert_then_submit_and_escape_restores() -> None:
    """Synchronize on real buffer events, not timed sleeps or fake key handlers."""
    with create_pipe_input() as pipe:
        session = PromptSession(
            input=pipe,
            output=DummyOutput(),
            completer=VerdictCompleter(home.command_specs(), snapshot(), NOW),
            key_bindings=home.completion_key_bindings(),
            complete_while_typing=False,
        )
        completions_ready = asyncio.Event()
        session.default_buffer.on_completions_changed += lambda _b: completions_ready.set()
        prompt = asyncio.create_task(session.prompt_async())
        pipe.send_text("/boot\t")
        await asyncio.wait_for(completions_ready.wait(), timeout=2)
        inserted = asyncio.Event()

        def observe(_app: object) -> None:
            if (
                session.default_buffer.text == "/bootstrap"
                and session.default_buffer.complete_state is None
            ):
                inserted.set()

        session.app.after_render += observe
        pipe.send_text("\r")
        await asyncio.wait_for(inserted.wait(), timeout=2)
        assert not prompt.done()  # Enter with selected menu never submits/executes.
        pipe.send_text("\r")
        assert await asyncio.wait_for(prompt, timeout=2) == "/bootstrap"
        session.app.after_render -= observe
        completions_ready.clear()
        prompt = asyncio.create_task(session.prompt_async())
        pipe.send_text("/boot\t")
        await asyncio.wait_for(completions_ready.wait(), timeout=2)
        restored = asyncio.Event()

        def escaped(_app: object) -> None:
            if (
                session.default_buffer.text == "/boot"
                and session.default_buffer.complete_state is None
            ):
                restored.set()

        session.app.after_render += escaped
        pipe.send_text("\x1b")
        await asyncio.wait_for(restored.wait(), timeout=2)
        assert not prompt.done()
        pipe.send_text("\r")
        assert await asyncio.wait_for(prompt, timeout=2) == "/boot"


def test_home_render_and_snapshot_reload_after_prepaid_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_verified_models_projection import at_rest_entry, conn, health_cache, row
    from verdict.actions import verified_models as surfaces
    from verdict.actions.verified_models import StorePaths, VerifiedSnapshotAdapter
    from verdict.orchestration.verified_refresh import RefreshConfig, refresh_for_consumer
    from verdict.prove_at_rest import ProbeExchange
    from verdict.tui_completion_snapshot import load_snapshot

    monkeypatch.setenv("VERDICT_HOME", str(tmp_path))
    monkeypatch.setattr(surfaces, "active_controller_route", lambda: None)
    paths = StorePaths.defaults(tmp_path)
    paths.health_cache.write_text(
        __import__("json").dumps(
            health_cache(at_rest_entry("cc/sonnet", checked_at=NOW - timedelta(minutes=15)))
        )
    )
    adapter = VerifiedSnapshotAdapter(
        "http://127.0.0.1:20128", paths, [row("cc/sonnet")], [conn("cc")], clock=lambda: NOW
    )
    assert (
        adapter.load(
            __import__(
                "verdict.orchestration.verified_models", fromlist=["VerifiedModelQuery"]
            ).VerifiedModelQuery()
        )
        .rows[0]
        .status.value
        == "STALE"
    )
    calls = []

    def transport(rid: str, phase: str, timeout: float) -> ProbeExchange:
        calls.append(phase)
        return ProbeExchange(
            http_status=200,
            ok=True,
            chat_exact=phase == "chat",
            tool_called=phase == "tool",
            reported_model=rid,
            latency_ms=1,
        )

    original = surfaces.consume_verified_models

    def consume(**kwargs: object) -> object:
        values = dict(kwargs)
        values.update(
            adapter=adapter,
            config=RefreshConfig(),
            clock=lambda: NOW,
            live=False,
            transport=transport,
            run_refresh=refresh_for_consumer,
        )
        return original(**values)

    # Home captures from its actual consumer adapter, not pre-refresh bytes.
    monkeypatch.setattr(surfaces, "consume_verified_models", consume)
    rendered = []
    monkeypatch.setattr(
        home, "_render_action_result", lambda _tui, ok, data, **_k: rendered.append((ok, data))
    )
    target = console()
    home._run_command(
        "/eligibility", tui=TerminalUI(target), state=home.HomeState(gateway=adapter.gateway)
    )
    assert calls == ["chat", "tool"]
    assert rendered[0][0]
    assert rendered[0][1]["rows"][0]["status"] == "VERIFIED"
    snapshot = load_snapshot(
        paths.health_cache.parent / "verified-completion-snapshot.json",
        now=__import__("verdict.actions.verified_models", fromlist=["utc_now"]).utc_now(),
    )
    assert snapshot.model_rows[0]["status"] == "VERIFIED"
    assert snapshot.model_rows[0]["checked_at"] == NOW.isoformat()
