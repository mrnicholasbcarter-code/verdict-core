"""Offline contract tests for verified-model actions, CLI, and TUI surfaces.

All stores are under tmp_path. Inventory, connections, clocks, transports,
readers, and cancellation are injected. Live transport construction is forbidden.
"""

from __future__ import annotations

import argparse
import io
import json
import threading
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console
from rich.table import Table

import verdict.actions.verified_models as surfaces
import verdict.home as home
import verdict.orchestration.verified_refresh as refresh
from tests.test_verified_models_projection import NOW, at_rest_entry, conn, health_cache, iso, row
from verdict.actions.registry import run_action
from verdict.actions.verified_models import StorePaths, VerifiedSnapshotAdapter
from verdict.orchestration import cli, eligibility_report
from verdict.orchestration import run as orchestration_run
from verdict.orchestration.contracts import TaskRequirements
from verdict.orchestration.eligibility import EligibilityLadder, HarnessVisibility
from verdict.orchestration.verified_models import VerifiedModelQuery
from verdict.orchestration.verified_models_render import (
    format_verified_progress,
    render_refresh_plan,
    render_verified_plain,
    render_verified_table,
)
from verdict.prove_at_rest import ProbeExchange
from verdict.subagent_selection import HealthResult
from verdict.terminal_ui import TerminalUI
from verdict.tui_verified_controls import WaitRunner

GATEWAY = "http://offline.invalid:20128/v1/"


def forbidden(*_args: Any, **_kwargs: Any) -> Any:
    pytest.fail("offline surface reached forbidden I/O, live construction, or refresh")


@pytest.fixture(autouse=True)
def offline_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove ambient policy/configuration and fail closed at every live boundary."""
    import urllib.request

    import verdict.probes as probes
    import verdict.prove_at_rest as prove

    for name in (
        "VERDICT_HEALTH_CACHE",
        "VERDICT_AUTO_REFRESH",
        "VERDICT_REFRESH_MAX_ROUTES",
        "VERDICT_REFRESH_MAX_REQUESTS",
        "VERDICT_REFRESH_WALL_SECONDS",
        "VERDICT_REFRESH_CONCURRENCY",
        "COLUMNS",
        "LINES",
        "NO_COLOR",
        "TERM",
        "CI",
        "FORCE_COLOR",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path))
    monkeypatch.setenv("VERDICT_PLAIN", "1")
    monkeypatch.setattr(surfaces, "active_controller_route", lambda: None)
    monkeypatch.setattr(home, "_COMMAND_INDEX", None)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(orchestration_run, "fetch_inventory", forbidden)
    monkeypatch.setattr(orchestration_run, "fetch_connections", forbidden)
    monkeypatch.setattr(orchestration_run, "resolve_api_key", forbidden)
    monkeypatch.setattr(eligibility_report, "build_selector", forbidden)
    monkeypatch.setattr(surfaces, "_lazy_live_transport", forbidden)
    monkeypatch.setattr(prove, "live_transport", forbidden)
    monkeypatch.setattr(probes, "openai_probe_transport", forbidden)


def write_json(path: Path, document: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


def files_under(path: Path) -> dict[str, bytes]:
    return {str(p.relative_to(path)): p.read_bytes() for p in path.rglob("*") if p.is_file()}


def adapter_for(
    tmp_path: Path,
    ids: tuple[str, ...] = ("cc/sonnet",),
    *,
    age: int | None = 900,
    inventory: list[dict[str, Any]] | None = None,
    connections: list[dict[str, Any]] | None = None,
) -> VerifiedSnapshotAdapter:
    paths = StorePaths.defaults(tmp_path)
    inventory = inventory if inventory is not None else [row(rid) for rid in ids]
    connections = connections if connections is not None else [conn("cc")]
    if age is not None:
        write_json(
            paths.health_cache,
            health_cache(
                *(at_rest_entry(rid, checked_at=NOW - timedelta(seconds=age)) for rid in ids)
            ),
        )
    return VerifiedSnapshotAdapter(
        GATEWAY, paths, inventory, connections, clock=lambda: NOW, monotonic=lambda: 0.0
    )


class RecordingTransport:
    def __init__(self, on_call: Callable[[str, str], None] | None = None) -> None:
        self.calls: list[tuple[str, str, float]] = []
        self.on_call = on_call

    def __call__(self, rid: str, phase: str, timeout: float) -> ProbeExchange:
        self.calls.append((rid, phase, timeout))
        if self.on_call is not None:
            self.on_call(rid, phase)
        assert 0 < timeout <= 15
        return ProbeExchange(
            http_status=200,
            ok=True,
            chat_exact=phase == "chat",
            tool_called=phase == "tool",
            reported_model=rid,
            latency_ms=2.0,
        )


def consume(adapter: VerifiedSnapshotAdapter, **kwargs: Any) -> Any:
    return surfaces.consume_verified_models(
        gateway=GATEWAY,
        adapter=adapter,
        clock=lambda: NOW,
        monotonic=kwargs.pop("monotonic", lambda: 0.0),
        live=False,
        **kwargs,
    )


def console_ui() -> tuple[TerminalUI, io.StringIO]:
    stream = io.StringIO()
    console = Console(
        file=stream,
        width=140,
        height=40,
        force_terminal=False,
        no_color=True,
        color_system=None,
        legacy_windows=False,
        _environ={},
    )
    return TerminalUI(console), stream


def cli_args(*flags: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    cli.add_parsers(parser.add_subparsers(dest="command"))
    return parser.parse_args(["eligibility", "--gateway", GATEWAY, *flags])


def rows_by_id(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {r["route_id"]: r for r in payload["rows"]}


@pytest.mark.parametrize("through_palette", [False, True])
def test_models_verified_is_read_only_even_when_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, through_palette: bool
) -> None:
    adapter = adapter_for(tmp_path)
    for path in (adapter.paths.ladder, adapter.paths.worker, adapter.paths.receipt):
        write_json(path, {})
    before = files_under(tmp_path)
    monkeypatch.setattr(surfaces, "refresh_for_consumer", forbidden)
    monkeypatch.setattr(surfaces, "consume_verified_models", forbidden)
    monkeypatch.setattr(surfaces, "HealthCache", forbidden)
    if through_palette:
        ok, data = home.run_palette_action("models.verified", {"adapter": adapter})
    else:
        result = run_action("models.verified", {"adapter": adapter})
        ok, data = result.ok, result.data
    assert ok
    assert data["schema"] == "verdict.verified-models/v1"
    assert data["generated_at"] == iso(NOW)
    assert data["rows"][0]["status"] == "STALE"
    assert "refresh" not in data
    assert files_under(tmp_path) == before


def test_local_only_cached_json_never_refreshes_or_fetches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = adapter_for(tmp_path)
    write_json(
        adapter.paths.catalog,
        {"inventory_rows": adapter.inventory_rows, "connections": adapter.connections},
    )
    before = files_under(tmp_path)
    monkeypatch.setattr(surfaces, "consume_verified_models", forbidden)
    monkeypatch.setattr(surfaces, "refresh_for_consumer", forbidden)
    monkeypatch.setattr(WaitRunner, "run", forbidden)
    result = run_action(
        "models.verified",
        {"gateway": GATEWAY, "paths": adapter.paths, "local_only": True, "clock": lambda: NOW},
    )
    assert result.ok
    assert result.data["rows"][0]["status"] == "STALE"
    assert result.data == run_action("models.verified", {"adapter": adapter}).data
    assert files_under(tmp_path) == before


def test_missing_local_catalog_has_no_live_fallback(tmp_path: Path) -> None:
    result = run_action(
        "models.verified",
        {
            "gateway": GATEWAY,
            "paths": StorePaths.defaults(tmp_path),
            "local_only": True,
            "clock": lambda: NOW,
        },
    )
    assert result.ok
    assert result.data["total_count"] == 0
    assert result.data["rows"] == []
    assert files_under(tmp_path) == {}


@pytest.mark.parametrize(
    "query",
    [
        VerifiedModelQuery(page=0),
        VerifiedModelQuery(page_size=0),
        VerifiedModelQuery(status="TYPO"),
    ],
)
def test_action_rejects_bad_query_before_any_snapshot_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, query: VerifiedModelQuery
) -> None:
    adapter = adapter_for(tmp_path)
    monkeypatch.setattr(surfaces, "_read_document", forbidden)
    monkeypatch.setattr(adapter, "_load_metadata", forbidden)
    result = run_action("models.verified", {"adapter": adapter, "query": query})
    assert not result.ok
    assert result.exit_code == 2


def test_fresh_consumer_zero_calls_and_runner_is_not_called(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = adapter_for(tmp_path, age=30)
    before = files_under(tmp_path)
    transport = RecordingTransport()
    monkeypatch.setattr(WaitRunner, "run", forbidden)
    result = consume(adapter, run_refresh=forbidden, transport=transport, write=forbidden)
    assert result.ok
    assert result.data["refresh"]["outcome"] == "reused_fresh"
    assert result.data["refresh"]["requests_made"] == 0
    assert result.data["rows"][0]["status"] == "VERIFIED"
    assert transport.calls == []
    assert files_under(tmp_path) == before


def test_stale_consumer_waits_then_reloads_and_only_then_renders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = adapter_for(tmp_path)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    events: list[str] = []
    output: dict[str, Any] = {}
    original_load = adapter.load

    def load(query: VerifiedModelQuery) -> Any:
        events.append("load")
        return original_load(query)

    monkeypatch.setattr(adapter, "load", load)

    def hold(_rid: str, phase: str) -> None:
        if phase == "chat":
            entered.set()
            assert release.wait(5), "test did not release the bounded transport"

    transport = RecordingTransport(hold)

    def runner(snapshot: Any, **kwargs: Any) -> Any:
        events.append("refresh-start")
        outcome = refresh.refresh_for_consumer(snapshot, **kwargs, sleep=forbidden)
        events.append("refresh-complete")
        return outcome

    def progress(line: str) -> None:
        assert "probed" in line
        assert "STALE" not in line and "cc/sonnet" not in line
        events.append("progress")

    def worker() -> None:
        try:
            output["result"] = consume(
                adapter, transport=transport, run_refresh=runner, write=progress
            )
            events.append("render")
            output["rendered"] = render_verified_plain(output["result"].data)
        except BaseException as exc:
            output["error"] = exc
        finally:
            finished.set()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    try:
        assert entered.wait(5)
        assert "result" not in output
        assert events == ["load", "progress", "refresh-start"]
    finally:
        release.set()
        assert finished.wait(5)
        thread.join(timeout=1)
    assert "error" not in output, output.get("error")
    assert not thread.is_alive()
    result = output["result"]
    assert result.ok
    assert result.data["rows"][0]["status"] == "VERIFIED"
    assert result.data["rows"][0]["checked_at"] == iso(NOW)
    assert result.data["rows"][0]["coding_ok"] is True
    assert result.data["refresh"]["requests_made"] == 2
    assert [phase for _, phase, _ in transport.calls] == ["chat", "tool"]
    assert events.count("load") == 2
    assert events.index("refresh-complete") < len(events) - 1
    final_load = max(i for i, event in enumerate(events) if event == "load")
    assert all(i < final_load for i, event in enumerate(events) if event == "progress")
    assert events.index("refresh-complete") < final_load < events.index("render")
    assert "STALE  cc/sonnet" not in output["rendered"]
    reloaded = run_action("models.verified", {"adapter": adapter}).data
    assert {k: v for k, v in result.data.items() if k != "refresh"} == reloaded


def test_auto_refresh_only_the_current_filtered_page(tmp_path: Path) -> None:
    ids = tuple(f"cc/model-{n:03d}" for n in range(8))
    adapter = adapter_for(tmp_path, ids)
    query = VerifiedModelQuery(status="STALE", provider="cc", search="model", page=2, page_size=2)
    expected_ids = [r.route_id for r in adapter.load(query).rows]
    transport = RecordingTransport()
    result = consume(adapter, query=query, transport=transport)
    assert result.ok
    assert {rid for rid, _, _ in transport.calls} == set(expected_ids)
    assert len(transport.calls) == 4
    # Reload after refresh re-applies the filters. Completed rows leave STALE;
    # the newly shown stale rows must not start a second sweep.
    assert all(r["status"] == "STALE" for r in result.data["rows"])
    all_rows = rows_by_id(run_action("models.verified", {"adapter": adapter}).data)
    assert {rid for rid, r in all_rows.items() if r["status"] == "VERIFIED"} == set(expected_ids)


def test_auto_mixed_capacity_probes_prepaid_only(tmp_path: Path) -> None:
    inventory = [
        row("cc/prepaid"),
        row("ap/metered", pricing={"input": 1.0, "output": 2.0}),
        row("zz/unknown"),
    ]
    connections = [
        conn("cc"),
        conn("ap", auth="apikey", plan="PAYG"),
        conn("zz", auth="apikey", plan=""),
    ]
    adapter = adapter_for(
        tmp_path,
        ("cc/prepaid", "ap/metered", "zz/unknown"),
        inventory=inventory,
        connections=connections,
    )
    transport = RecordingTransport()
    result = consume(adapter, transport=transport)
    assert result.ok
    assert [(rid, phase) for rid, phase, _ in transport.calls] == [
        ("cc/prepaid", "chat"),
        ("cc/prepaid", "tool"),
    ]
    rows = rows_by_id(result.data)
    assert rows["cc/prepaid"]["status"] == "VERIFIED"
    for rid, capacity in (("ap/metered", "metered"), ("zz/unknown", "unknown")):
        assert rows[rid]["capacity_class"] == capacity
        assert rows[rid]["status"] == "STALE"
        assert rows[rid]["refresh_reason"] == "requires_confirmation"
        assert rows[rid]["coding_ok"] is False


def test_metered_unknown_only_zero_calls_and_no_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = adapter_for(
        tmp_path,
        ("ap/metered", "zz/unknown"),
        age=None,
        inventory=[row("ap/metered", pricing={"input": 1.0, "output": 2.0}), row("zz/unknown")],
        connections=[conn("ap", auth="apikey", plan="PAYG"), conn("zz", auth="apikey", plan="")],
    )
    monkeypatch.setattr(WaitRunner, "run", forbidden)
    transport = RecordingTransport()
    result = consume(adapter, run_refresh=forbidden, transport=transport)
    assert result.data["refresh"]["outcome"] == "nothing_eligible"
    assert transport.calls == []
    assert {r["refresh_reason"] for r in result.data["rows"]} == {"requires_confirmation"}
    assert files_under(tmp_path) == {}


@pytest.mark.parametrize("mode", ["environment", "no-refresh"])
def test_disabled_refresh_keeps_last_known_and_does_not_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    adapter = adapter_for(tmp_path)
    before = files_under(tmp_path)
    if mode == "environment":
        monkeypatch.setenv("VERDICT_AUTO_REFRESH", "0")
    monkeypatch.setattr(WaitRunner, "run", forbidden)
    result = consume(
        adapter, run_refresh=forbidden, transport=forbidden, no_refresh=mode == "no-refresh"
    )
    summary = result.data["refresh"]
    assert summary["outcome"] == (
        "auto_refresh_disabled" if mode == "environment" else "no_refresh"
    )
    assert summary["last_known"] is True
    assert summary["requests_made"] == 0
    assert result.data["rows"][0]["status"] == "STALE"
    assert "LAST-KNOWN" in render_verified_plain(result.data)
    assert files_under(tmp_path) == before


@pytest.mark.parametrize("cap", ["route_cap", "request_cap", "wall_cap", "bucket"])
def test_incomplete_refresh_keeps_stale_rows_and_exact_cap_labels(tmp_path: Path, cap: str) -> None:
    adapter = adapter_for(tmp_path, ("cc/a", "cc/b", "cc/c"))
    config = refresh.RefreshConfig(
        max_routes=1 if cap == "route_cap" else 3,
        max_requests=2 if cap == "request_cap" else 6,
        wall_seconds=1 if cap == "wall_cap" else 120,
    )
    ticks = [0.0]

    def advance(_rid: str, phase: str) -> None:
        if cap == "wall_cap" and phase == "chat":
            ticks[0] = 2.0

    if cap == "bucket":
        document = json.loads(adapter.paths.health_cache.read_text())
        document["buckets"] = {"cc": {"capacity": 1, "window_seconds": 60, "timestamps": []}}
        write_json(adapter.paths.health_cache, document)
    transport = RecordingTransport(advance)
    result = consume(adapter, transport=transport, config=config, monotonic=lambda: ticks[0])
    assert result.ok
    assert result.data["refresh"]["outcome"] == "capped"
    assert result.data["refresh"]["cap_reason"] == cap
    assert result.data["refresh"]["complete"] is False
    stale = [r for r in result.data["rows"] if r["status"] == "STALE"]
    assert stale
    assert {r["refresh_reason"] for r in stale} == {cap}
    assert all(r["coding_ok"] is False for r in stale)
    rendered = render_verified_plain(result.data)
    assert f"Refresh cap/skip reason: {cap}" in rendered
    assert f"refresh_reason={cap}" in rendered
    assert len(transport.calls) <= config.max_requests
    if cap == "wall_cap":
        assert len(transport.calls) == 1
        assert len(stale) == 3  # partial chat cannot renew the old full proof
    if cap == "bucket":
        assert transport.calls == []


def test_cancel_after_chat_renders_last_known_without_partial_proof(tmp_path: Path) -> None:
    adapter = adapter_for(tmp_path, ("cc/a", "cc/b"))
    stopped = threading.Event()
    transport = RecordingTransport(lambda _rid, phase: stopped.set() if phase == "chat" else None)
    result = consume(adapter, transport=transport, cancel=stopped.is_set)
    assert result.ok
    assert result.data["refresh"]["outcome"] == "cancelled"
    assert result.data["refresh"]["last_known"] is True
    assert result.data["refresh"]["requests_made"] == 1
    assert [(rid, phase) for rid, phase, _ in transport.calls] == [("cc/a", "chat")]
    assert all(r["status"] == "STALE" and not r["coding_ok"] for r in result.data["rows"])
    assert {r["refresh_reason"] for r in result.data["rows"]} == {"cancelled"}
    assert "CANCELLED / LAST-KNOWN" in render_verified_plain(result.data)
    stored = json.loads(adapter.paths.health_cache.read_text())["routes"]
    assert {entry["checked_at"] for entry in stored.values()} == {iso(NOW - timedelta(seconds=900))}


def test_concurrent_consumers_join_singleflight_without_sleep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = adapter_for(tmp_path, ("cc/a", "cc/b"))
    entered, release, joined = threading.Event(), threading.Event(), threading.Event()
    owner_complete = threading.Event()
    done = {"owner": threading.Event(), "joiner": threading.Event()}
    results: dict[str, Any] = {}
    original_join = refresh.RefreshCoordinator._join_and_wait

    def observe_join(coordinator: Any, *args: Any, **kwargs: Any) -> Any:
        joined.set()
        return original_join(coordinator, *args, **kwargs)

    monkeypatch.setattr(refresh.RefreshCoordinator, "_join_and_wait", observe_join)

    def hold(_rid: str, phase: str) -> None:
        if phase == "chat":
            entered.set()
            assert release.wait(5)

    owner_transport = RecordingTransport(hold)
    join_transport = RecordingTransport()

    def event_wait(_seconds: float) -> None:
        # The coordinator's cross-process wait is replaced with a deterministic
        # wake-up. No test or worker uses time.sleep or polling.
        assert owner_complete.wait(5)

    def owner_runner(snapshot: Any, **kwargs: Any) -> Any:
        try:
            return refresh.refresh_for_consumer(snapshot, **kwargs, sleep=forbidden)
        finally:
            owner_complete.set()

    def joined_runner(snapshot: Any, **kwargs: Any) -> Any:
        return refresh.refresh_for_consumer(snapshot, **kwargs, sleep=event_wait)

    def worker(name: str, query: VerifiedModelQuery, transport: Any, runner: Any) -> None:
        try:
            results[name] = consume(adapter, query=query, transport=transport, run_refresh=runner)
        except BaseException as exc:
            results[name] = exc
        finally:
            done[name].set()

    owner_thread = threading.Thread(
        target=worker,
        args=("owner", VerifiedModelQuery(search="cc/a"), owner_transport, owner_runner),
        daemon=True,
    )
    join_thread = threading.Thread(
        target=worker,
        args=("joiner", VerifiedModelQuery(), join_transport, joined_runner),
        daemon=True,
    )
    owner_thread.start()
    try:
        assert entered.wait(5)
        join_thread.start()
        assert joined.wait(5)
        assert not done["owner"].is_set() and not done["joiner"].is_set()
        assert join_transport.calls == []
    finally:
        release.set()
        assert done["owner"].wait(5)
        if join_thread.ident is not None:
            assert done["joiner"].wait(5)
        owner_thread.join(timeout=1)
        if join_thread.ident is not None:
            join_thread.join(timeout=1)
    assert not owner_thread.is_alive() and not join_thread.is_alive()
    for name in ("owner", "joiner"):
        assert not isinstance(results[name], BaseException), results[name]
        assert results[name].ok
    owner_data, join_data = results["owner"].data, results["joiner"].data
    assert owner_data["refresh"]["outcome"] == "completed"
    assert join_data["refresh"]["outcome"] == "joined"
    assert join_data["refresh"]["job_id"] == owner_data["refresh"]["job_id"]
    assert [(rid, phase) for rid, phase, _ in owner_transport.calls] == [
        ("cc/a", "chat"),
        ("cc/a", "tool"),
    ]
    assert join_transport.calls == []
    rows = rows_by_id(join_data)
    assert rows["cc/a"]["status"] == "VERIFIED"
    assert rows["cc/b"]["status"] == "STALE"
    assert rows["cc/b"]["refresh_reason"] == "joined_job_not_covered"
    assert "joined_job_not_covered" in render_verified_plain(join_data)


def test_cli_json_tui_payload_exact_parity_progress_only_on_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    adapter = adapter_for(tmp_path)
    transport = RecordingTransport()
    original_consume = surfaces.consume_verified_models

    def injected(**kwargs: Any) -> Any:
        kwargs.update(
            adapter=adapter,
            clock=lambda: NOW,
            monotonic=lambda: 0.0,
            transport=transport,
            live=False,
        )
        return original_consume(**kwargs)

    monkeypatch.setattr(surfaces, "consume_verified_models", injected)
    assert cli.dispatch(cli_args("--verified", "--json")) == 0
    captured = capsys.readouterr()
    cli_data = json.loads(captured.out)
    assert "probed" in captured.err
    assert captured.out.count('"schema": "verdict.verified-models/v1"') == 1
    assert cli_data["rows"][0]["status"] == "VERIFIED"
    ok, tui_data = home.run_palette_action(
        "models.verified", {"_consumer": True, "gateway": GATEWAY}
    )
    assert ok
    # Complete stable envelope, including generated_at, counts, filters, and
    # source_errors. Only the separately disclosed job summary differs.
    assert {k: v for k, v in cli_data.items() if k != "refresh"} == {
        k: v for k, v in tui_data.items() if k != "refresh"
    }
    assert cli_data["refresh"]["outcome"] == "completed"
    assert tui_data["refresh"]["outcome"] == "reused_fresh"
    assert len(transport.calls) == 2


@pytest.mark.parametrize(
    "flags",
    [
        ("--probe",),
        ("--scope", "cc/"),
        ("--reasoning",),
        ("--frontier",),
        ("--provider-family", "cc"),
        ("--prefer", "claude"),
    ],
)
def test_cli_verified_rejects_legacy_probe_and_task_filters_before_io(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], flags: tuple[str, ...]
) -> None:
    monkeypatch.setattr(surfaces, "consume_verified_models", forbidden)
    assert cli.dispatch(cli_args("--verified", "--json", *flags)) == 2
    error = json.loads(capsys.readouterr().out)
    assert "--verified cannot be combined" in error["error"]
    assert flags[0] in error["error"]


@pytest.mark.parametrize("flags", [("--status", "TYPO"), ("--page", "0"), ("--page-size", "0")])
def test_cli_verified_validates_before_io(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], flags: tuple[str, ...]
) -> None:
    monkeypatch.setattr(surfaces, "consume_verified_models", forbidden)
    assert cli.dispatch(cli_args("--verified", "--json", *flags)) == 2
    assert "filters or paging" in json.loads(capsys.readouterr().out)["error"]


def test_unscoped_default_preserves_chat_only_small_context_and_all_statuses(
    tmp_path: Path,
) -> None:
    inventory = [
        row("cc/full"),
        row("cc/chat", tools=False, ctx=512),
        row("kr/stale"),
        row("cc/unverified"),
        row("cc/failed"),
        row("ap/offline"),
        row("auto"),
        row("cc/excluded"),
    ]
    adapter = adapter_for(
        tmp_path,
        age=None,
        inventory=inventory,
        connections=[conn("cc"), conn("kr"), conn("ap", active=False)],
    )
    adapter.policy_exclusions = {"cc/excluded": "operator policy"}
    write_json(
        adapter.paths.health_cache,
        health_cache(
            at_rest_entry("cc/full", checked_at=NOW - timedelta(seconds=30)),
            at_rest_entry("cc/chat", checked_at=NOW - timedelta(seconds=30), tool_ok=False),
            at_rest_entry("kr/stale", checked_at=NOW - timedelta(seconds=900)),
            at_rest_entry(
                "cc/failed",
                checked_at=NOW - timedelta(seconds=10),
                healthy=False,
                category="timeout",
                chat_ok=False,
                tool_ok=False,
                until=NOW + timedelta(seconds=60),
            ),
        ),
    )
    payload = run_action("models.verified", {"adapter": adapter}).data
    assert payload["filters"] == {"status": None, "provider": None, "search": None}
    assert payload["total_count"] == payload["filtered_count"] == 8
    assert payload["page_size"] == 50
    assert set(payload["counts_by_status"]) == {
        "VERIFIED",
        "STALE",
        "FAILED",
        "UNAVAILABLE",
        "UNVERIFIED",
        "INVENTORY_ONLY",
        "EXCLUDED",
    }
    assert all(n > 0 for n in payload["counts_by_status"].values())
    rows = rows_by_id(payload)
    assert rows["cc/chat"]["status"] == "VERIFIED"
    assert rows["cc/chat"]["coding_ok"] is False
    assert rows["cc/chat"]["capabilities"]["context_window"] == 512
    assert "kr/stale" in rows  # no hidden cc/ scope


def test_filters_conjunctive_case_insensitive_search_and_page_clamping(tmp_path: Path) -> None:
    ids = (*(f"cc/SONNET-{n:03d}" for n in range(7)), "kr/SONNET-000")
    adapter = adapter_for(tmp_path, ids, connections=[conn("cc"), conn("kr")])
    result = run_action(
        "models.verified",
        {
            "adapter": adapter,
            "status": "STALE",
            "provider": "cc",
            "search": "sonnet",
            "page": 999,
            "page_size": 3,
        },
    )
    assert result.ok
    payload = result.data
    assert payload["total_count"] == 8
    assert payload["filtered_count"] == 7
    assert payload["page"] == payload["page_count"] == 3
    assert len(payload["rows"]) == 1
    assert payload["rows"][0]["route_id"] == "cc/SONNET-006"
    assert sum(payload["filtered_counts_by_status"].values()) == 7
    assert payload["counts_by_status"]["STALE"] == 8


def test_render_uses_full_bounded_page_not_generic_first_50(tmp_path: Path) -> None:
    ids = tuple(f"cc/model-{n:03d}" for n in range(230))
    adapter = adapter_for(tmp_path, ids)
    payload = run_action("models.verified", {"adapter": adapter, "page_size": 7000}).data
    assert payload["page_size"] == 200
    assert len(payload["rows"]) == 200
    assert payload["total_count"] == 230
    rendered = render_verified_table(payload)
    tables = [part for part in rendered.renderables if isinstance(part, Table)]
    assert len(tables) == 1
    assert len(tables[0].rows) == 200
    plain = render_verified_plain(payload)
    assert "cc/model-199" in plain and "cc/model-200" not in plain
    tui, stream = console_ui()
    home._render_action_result(tui, True, payload)
    assert "cc/model-199" in stream.getvalue()
    assert "cc/model-200" not in stream.getvalue()
    assert "showing 200/230" in stream.getvalue()


def test_render_defensive_safety_cap_for_unpaged_input(tmp_path: Path) -> None:
    adapter = adapter_for(tmp_path, age=None)
    payload = run_action("models.verified", {"adapter": adapter}).data
    sample = payload["rows"][0]
    payload["rows"] = [{**sample, "route_id": f"cc/unsafe-{n:03d}"} for n in range(230)]
    tables = [
        part for part in render_verified_table(payload).renderables if isinstance(part, Table)
    ]
    assert len(tables[0].rows) == 200
    plain = render_verified_plain(payload)
    assert "cc/unsafe-199" in plain and "cc/unsafe-200" not in plain
    assert "Display safety cap: 200" in plain


def test_snapshot_and_plan_progress_render_never_emit_secrets(tmp_path: Path) -> None:
    secret = "sk-0123456789abcdefghijklmnop"
    email = "operator-secret@example.invalid"
    raw_url = f"https://{email}:{secret}@gateway.invalid/v1?api_key={secret}"
    adapter = adapter_for(tmp_path)
    adapter.connections = [conn("cc", api_key=secret, account_email=email, base_url=raw_url)]
    adapter.policy_exclusions = {"cc/sonnet": f"Authorization: Bearer {secret} {raw_url}"}
    payload = run_action("models.verified", {"adapter": adapter}).data
    plan = run_action(
        "models.refresh.plan",
        {
            "snapshot_rows": payload["rows"],
            "needed_ids": ["cc/sonnet"],
            "gateway_origin": adapter.gateway,
            "now": NOW,
        },
    )
    progress = format_verified_progress(
        {
            "probed": 0,
            "total": 1,
            "verified": 0,
            "failed": 0,
            "unavailable": 0,
            "requests_made": 0,
            "requests_reserved": 2,
            "elapsed_seconds": 0.0,
            "last_reason": f"Bearer {secret} {email} {raw_url}",
        }
    )
    tui, stream = console_ui()
    home._render_action_result(tui, True, payload)
    combined = json.dumps(payload) + render_verified_plain(payload) + stream.getvalue()
    combined += json.dumps(plan.data) + render_refresh_plan(plan.data) + progress
    for forbidden_value in (secret, email, raw_url, str(tmp_path)):
        assert forbidden_value not in combined


@pytest.mark.parametrize("answer", ["", "n", "no", None, "eof", "interrupt"])
def test_manual_refresh_decline_defaults_no_and_zero_calls_writes(
    tmp_path: Path, answer: str | None
) -> None:
    adapter = adapter_for(tmp_path)
    before = files_under(tmp_path)
    lines: list[str] = []

    def read_line(prompt: str) -> Any:
        assert "[y/N]" in prompt
        if answer == "eof":
            raise EOFError
        if answer == "interrupt":
            raise KeyboardInterrupt
        return answer

    result = consume(
        adapter,
        manual=True,
        read_line=read_line,
        write=lines.append,
        transport=forbidden,
        run_refresh=forbidden,
    )
    assert result.ok
    assert result.data["refresh"]["outcome"] in {"not_confirmed", "cancelled"}
    assert result.data["refresh"]["requests_made"] == 0
    assert result.data["rows"][0]["status"] == "STALE"
    presentation = "\n".join(lines)
    assert "Selected routes (1)" in presentation
    assert "cc/sonnet" in presentation
    assert "estimated_requests=2" in presentation
    assert "quota" in presentation and "currency estimate unavailable" in presentation
    assert "[y/N]" in presentation
    assert files_under(tmp_path) == before


def test_manual_yes_exact_plan_metered_and_unknown_liveness_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = adapter_for(
        tmp_path,
        ("ap/metered", "zz/unknown"),
        age=None,
        inventory=[row("ap/metered", pricing={"input": 1.0, "output": 2.0}), row("zz/unknown")],
        connections=[conn("ap", auth="apikey", plan="PAYG"), conn("zz", auth="apikey", plan="")],
    )
    transport = RecordingTransport()
    lines: list[str] = []
    result = consume(
        adapter, manual=True, read_line=lambda _p: "y", write=lines.append, transport=transport
    )
    assert result.ok
    assert [(rid, phase) for rid, phase, _ in transport.calls] == [
        ("ap/metered", "chat"),
        ("zz/unknown", "chat"),
    ]
    assert result.data["refresh"]["requests_made"] == 2
    assert result.data["refresh"]["outcome"] == "completed"
    assert result.data["refresh"]["verified"] == 0
    assert result.data["refresh"]["alive"] == 2
    assert all(r["status"] == "VERIFIED" and not r["coding_ok"] for r in result.data["rows"])
    assert "Selected routes (2)" in "\n".join(lines)
    assert "estimated_requests=2" in "\n".join(lines)


def test_progress_distinguishes_healthy_chat_from_full_verified() -> None:
    line = format_verified_progress(
        {
            "probed": 1,
            "total": 1,
            "verified": 0,
            "alive": 1,
            "failed": 0,
            "unavailable": 0,
            "requests_made": 1,
            "requests_reserved": 0,
            "elapsed_seconds": 0.25,
        }
    )
    assert "1/1 probed" in line
    assert "healthy=1 failed=0" in line
    assert "elapsed=0.2s" in line


def test_manual_metered_alive_only_reloads_chat_verified_evidence(tmp_path: Path) -> None:
    adapter = adapter_for(
        tmp_path,
        ("ap/metered",),
        age=None,
        inventory=[row("ap/metered", pricing={"input": 1.0, "output": 2.0})],
        connections=[conn("ap", auth="apikey", plan="PAYG")],
    )
    transport = RecordingTransport()
    result = consume(adapter, manual=True, read_line=lambda _p: "y", transport=transport)
    assert result.ok
    assert [(rid, phase) for rid, phase, _ in transport.calls] == [("ap/metered", "chat")]
    summary = result.data["refresh"]
    assert summary["verified"] == 0
    assert summary["alive"] == 1
    assert summary["route_outcomes"]["ap/metered"]["status_after"] == "UNVERIFIED"
    assert summary["route_outcomes"]["ap/metered"]["alive"] is True
    cached = json.loads(adapter.paths.health_cache.read_text())["routes"]["ap/metered"]
    assert cached["chat_ok"] is True
    assert cached["tool_ok"] is False
    final = result.data["rows"][0]
    assert final["status"] == "VERIFIED"
    assert final["coding_ok"] is False
    assert "chat_only_not_coding_verified" in final["restrictions"]
    assert "chat verified; tools unverified" in render_verified_plain(result.data)
    reloaded = run_action("models.verified", {"adapter": adapter}).data
    assert {k: v for k, v in result.data.items() if k != "refresh"} == reloaded


def test_chat_only_without_reported_identity_remains_unverified(tmp_path: Path) -> None:
    adapter = adapter_for(tmp_path, age=0)
    document = json.loads(adapter.paths.health_cache.read_text())
    document["routes"]["cc/sonnet"].update(tool_ok=False, identity="not_reported")
    write_json(adapter.paths.health_cache, document)
    result = consume(adapter, no_refresh=True, run_refresh=forbidden, transport=forbidden)
    assert result.ok
    final = result.data["rows"][0]
    assert final["status"] == "UNVERIFIED"
    assert final["coding_ok"] is False
    assert "identity_not_verified" in final["restrictions"]
    assert "chat verified; tools unverified" not in render_verified_plain(result.data)


@pytest.mark.parametrize("entry", ["inline", "prompted", "palette"])
def test_probe_all_home_entry_paths_parse_same_list_and_require_yes(
    monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    tui, stream = console_ui()
    calls: list[tuple[str, dict[str, Any]]] = []
    expected = ["cc/x", "kr/y", "openrouter/foo:free"]
    raw = "cc/x,kr/y cc/x openrouter/foo:free"

    def action(name: str, params: dict[str, Any] | None = None) -> tuple[bool, Any]:
        calls.append((name, dict(params or {})))
        return True, []

    monkeypatch.setattr(home, "run_palette_action", action)
    if entry == "palette":
        monkeypatch.setattr(
            home, "_COMMAND_INDEX", {"/palette-probe": ("probe", "probe", "action")}
        )
    answers = iter(([raw] if entry != "inline" else []) + ["y"])
    command = (
        f"/probe {raw}"
        if entry == "inline"
        else ("/palette-probe" if entry == "palette" else "/probe")
    )
    home._run_command(
        command, tui=tui, state=home.HomeState(gateway=GATEWAY), line_reader=lambda: next(answers)
    )
    assert len(calls) == 1
    name, params = calls[0]
    assert name == "probe"
    assert params["models"] == expected
    assert isinstance(params["models"], list)
    assert params["allow_live_probe"] is True
    assert params["base_url"] == "http://offline.invalid:20128/v1"
    assert "Probe 3 exact model ids" in stream.getvalue()
    assert "[y/N]" in stream.getvalue()
    assert "quota" in stream.getvalue()
    assert "currency estimate unavailable" in " ".join(stream.getvalue().split())


@pytest.mark.parametrize("answer", ["", "n", None, "eof", "interrupt"])
def test_probe_no_consent_never_dispatches(
    monkeypatch: pytest.MonkeyPatch, answer: str | None
) -> None:
    tui, stream = console_ui()
    monkeypatch.setattr(home, "run_palette_action", forbidden)

    def read_line() -> Any:
        if answer == "eof":
            raise EOFError
        if answer == "interrupt":
            raise KeyboardInterrupt
        return answer

    home._run_command(
        "/probe cc/x,kr/y", tui=tui, state=home.HomeState(gateway=GATEWAY), line_reader=read_line
    )
    assert "[y/N]" in stream.getvalue()
    assert "no probes" in stream.getvalue()


@pytest.mark.parametrize("command", ["/probe --probe", "/probe ,,,", "/probe cc/x --probe"])
def test_probe_options_and_empty_list_rejected_before_consent(
    monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    tui, stream = console_ui()
    monkeypatch.setattr(home, "run_palette_action", forbidden)
    home._run_command(
        command, tui=tui, state=home.HomeState(gateway=GATEWAY), line_reader=forbidden
    )
    assert "ERROR" in stream.getvalue()
    assert "[y/N]" not in stream.getvalue()


@pytest.mark.parametrize(
    "text",
    [
        "",
        "stale",
        "provider=cc",
        "search=sonnet",
        "page=2",
        "stale provider=cc search=sonnet page=2",
    ],
)
def test_home_eligibility_uses_independent_filters_no_scope_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    adapter = adapter_for(tmp_path)
    requests: list[dict[str, Any]] = []
    original_consume = surfaces.consume_verified_models

    def injected(**kwargs: Any) -> Any:
        requests.append(dict(kwargs))
        kwargs.update(
            adapter=adapter,
            clock=lambda: NOW,
            monotonic=lambda: 0.0,
            no_refresh=True,
            live=False,
            transport=forbidden,
        )
        return original_consume(**kwargs)

    monkeypatch.setattr(surfaces, "consume_verified_models", injected)
    tui, stream = console_ui()
    home._run_command(
        f"/eligibility {text}",
        tui=tui,
        state=home.HomeState(gateway=GATEWAY),
        line_reader=forbidden,
    )
    assert len(requests) == 1
    query = requests[0]["query"]
    assert query.status == ("STALE" if "stale" in text else None)
    assert query.provider == ("cc" if "provider=" in text else None)
    assert query.search == ("sonnet" if "search=" in text else None)
    assert query.page == (2 if "page=" in text else 1)
    assert requests[0]["manual"] is False
    assert "scope" not in requests[0]
    assert "verified models" in stream.getvalue()


def test_selection_hook_default_none_and_injected_hook_runs_before_confirmation(
    tmp_path: Path,
) -> None:
    assert EligibilityLadder.__init__.__kwdefaults__["refresh_hook"] is None
    calls: list[Any] = []

    def probe(rid: str) -> HealthResult:
        calls.append(("confirm", rid))
        return HealthResult(healthy=True, category="ok")

    def hook(ids: Any, now: Any) -> None:
        calls.append(("refresh", list(ids), now))

    ladder = EligibilityLadder(
        [row("cc/sonnet")], [conn("cc")], probe, tmp_path / "selection.json", refresh_hook=hook
    )
    chosen, _ = ladder.select(TaskRequirements(), now=NOW)
    assert chosen is not None
    assert calls == [("refresh", ["cc/sonnet"], NOW), ("confirm", "cc/sonnet")]


@pytest.mark.parametrize("age,capacity", [(0, "subscription"), (None, "metered")])
def test_selection_hook_no_eligible_candidates_never_waits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, age: int | None, capacity: str
) -> None:
    ids = ("cc/sonnet",) if capacity == "subscription" else ("ap/metered",)
    adapter = adapter_for(
        tmp_path,
        ids,
        age=age,
        inventory=None
        if capacity == "subscription"
        else [row("ap/metered", pricing={"input": 1.0, "output": 2.0})],
        connections=None
        if capacity == "subscription"
        else [conn("ap", auth="apikey", plan="PAYG")],
    )
    monkeypatch.setattr(WaitRunner, "run", forbidden)
    transport = RecordingTransport()
    before = files_under(tmp_path)
    hook = surfaces.selection_refresh_hook(
        GATEWAY, adapter=adapter, transport=transport, run_refresh=forbidden
    )
    assert hook is not None
    assert hook(ids, NOW) is None
    assert transport.calls == []
    assert files_under(tmp_path) == before


def test_build_selector_fake_hook_passes_through_without_live_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import verdict.admission as admission
    import verdict.subagent_selection as selection

    monkeypatch.setattr(orchestration_run, "fetch_inventory", lambda *_a, **_k: [row("cc/sonnet")])
    monkeypatch.setattr(orchestration_run, "fetch_connections", lambda *_a, **_k: [conn("cc")])
    monkeypatch.setattr(orchestration_run, "resolve_api_key", lambda: None)
    monkeypatch.setattr(admission, "active_controller_route", lambda: None)
    monkeypatch.setattr(
        admission, "default_runtime_evidence", lambda **_k: admission.RuntimeEvidence()
    )
    monkeypatch.setattr(admission.AdmittedSet, "write_receipt", lambda *_a: None)
    monkeypatch.setattr(
        eligibility_report,
        "prime_visibility",
        lambda **_k: HarnessVisibility(None, source="offline-test"),
    )
    monkeypatch.setattr(selection, "openai_health_probe", lambda *_a, **_k: forbidden)

    # Use the saved builder without reloading or mutating a shared module.
    def hook(_ids: Any, _now: Any) -> None:
        return None

    selector = ORIGINAL_BUILD_SELECTOR(
        GATEWAY, scope="", prefer="claude", state_file=tmp_path / "state.json", refresh_hook=hook
    )
    assert selector._refresh_hook is hook
    default_selector = ORIGINAL_BUILD_SELECTOR(
        GATEWAY, scope="", prefer="claude", state_file=tmp_path / "default.json"
    )
    assert default_selector._refresh_hook is None
    assert files_under(tmp_path) == {}


ORIGINAL_BUILD_SELECTOR = eligibility_report.build_selector
