"""Startup blockers must settle a supervised run before any model dispatch."""

from __future__ import annotations

import argparse
import asyncio
import json
import urllib.error
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from verdict.orchestration import cli, run
from verdict.orchestration.eligibility_report import GatewayDiscoveryError, _gateway_fetch_error
from verdict.orchestration.receipt import EventLog, verify_run_receipt
from verdict.orchestration.supervisor import (
    ControllerSupervisor,
    build_command_factory,
    read_events,
)


class _ExitedController:
    """Completed offline controller, with no OS process to signal."""

    pid = None

    def __init__(self, returncode: int) -> None:
        self.returncode = returncode

    async def wait(self) -> int:
        return self.returncode


def _offline_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[ControllerSupervisor, list[list[str]]]:
    monkeypatch.setenv("VERDICT_OMNIROUTE_API_KEY", "fixture-only-never-send")
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / "state"))
    parser = argparse.ArgumentParser()
    cli.add_parsers(parser.add_subparsers(dest="command"))
    spawned: list[list[str]] = []

    async def spawn(
        argv: list[str], *, log_path: Path, env: Mapping[str, str] | None
    ) -> _ExitedController:
        del env
        spawned.append(argv)
        # Use the real command factory and CLI parser, but no subprocess or
        # provider calls. Exceptions represent controller-process exit failure.
        args = parser.parse_args(argv[3:])
        try:
            # The controller owns its own event loop, as a subprocess would.
            returncode = await asyncio.to_thread(cli._orchestrate, args)
        except GatewayDiscoveryError as exc:
            assert exc.retryable
            log_path.write_text(str(exc))
            returncode = 1
        return _ExitedController(returncode)

    supervisor = ControllerSupervisor(
        build_command_factory(
            "startup",
            ["ship a focused change", "--repo", str(tmp_path), "--plain", "--json"],
            tmp_path / "runs",
        ),
        tmp_path / "runs" / "startup",
        spawner=spawn,
        sleep=lambda _: asyncio.sleep(0),
        max_restarts=2,
        total_deadline_seconds=5,
    )
    return supervisor, spawned


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("endpoint", ["/v1/models", "/api/providers"])
def test_gateway_auth_failure_settles_run_without_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: int,
    endpoint: str,
    no_gateway_network: None,
) -> None:
    del no_gateway_network
    supervisor, spawned = _offline_supervisor(tmp_path, monkeypatch)
    fetched: list[str] = []

    def fetch(path: str) -> list[dict[str, Any]]:
        fetched.append(path)
        if path == endpoint:
            raise urllib.error.HTTPError(
                "https://fixture.invalid/fixture-only-never-send",
                status,
                "fixture-only-never-send",
                {"Authorization": "Bearer fixture-only-never-send"},
                None,
            )
        return []

    monkeypatch.setattr(run, "fetch_inventory", lambda *a, **kw: fetch("/v1/models"))
    monkeypatch.setattr(run, "fetch_connections", lambda *a, **kw: fetch("/api/providers"))

    def forbidden_dispatch(*args: object, **kwargs: object) -> None:
        pytest.fail("startup authentication failure must not plan or construct an executor")

    monkeypatch.setattr(run, "run_golden_path", forbidden_dispatch)
    monkeypatch.setattr(cli, "_executor", forbidden_dispatch)
    # A prior terminal verdict must not mask this generation's discovery cause.
    EventLog(supervisor.run_dir / "events.jsonl").emit(
        "run_finished", outcome="BLOCKED", reason="prior fixture generation"
    )
    outcome = asyncio.run(asyncio.wait_for(supervisor.run(), timeout=5))
    assert outcome.state == "BLOCKED"
    assert outcome.restarts == 0
    assert len(outcome.generations) == len(spawned) == 1
    assert outcome.generations[0]["state"] == "BLOCKED"
    assert endpoint in outcome.reason and f"HTTP {status}" in outcome.reason
    assert fetched == (["/v1/models"] if endpoint == "/v1/models" else ["/v1/models", endpoint])
    events = read_events(supervisor.run_dir / "events.jsonl")
    finished = [e for e in events if e["type"] == "run_finished"]
    assert len(finished) == 2
    assert finished[-1]["data"] == {
        "outcome": "BLOCKED",
        "reason": outcome.reason,
        "phase": "startup",
    }
    assert not any(e["type"] in {"plan_started", "dispatch"} for e in events)
    # The startup cause is a verifiable receipt with no plan (release review S9).
    assert verify_run_receipt(supervisor.run_dir) == []
    receipt = json.loads((supervisor.run_dir / "receipt.json").read_text())
    assert (receipt["outcome"], receipt["reason"]) == ("BLOCKED", outcome.reason)
    assert receipt["nodes"] == []
    assert "fixture-only-never-send" not in json.dumps(receipt)
    assert not (supervisor.run_dir / "graph.json").exists()
    output = capsys.readouterr()
    assert "BLOCKED:" in output.err
    assert "fixture-only-never-send" not in output.out + output.err
    assert "fixture-only-never-send" not in (supervisor.run_dir / "events.jsonl").read_text()


@pytest.mark.parametrize("status", [None, 429, 500, 502, 503, 504])
@pytest.mark.parametrize("endpoint", ["/v1/models", "/api/providers"])
def test_supervisor_retries_transient_discovery_and_reaches_planning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int | None,
    endpoint: str,
    no_gateway_network: None,
) -> None:
    del no_gateway_network
    supervisor, spawned = _offline_supervisor(tmp_path, monkeypatch)
    failed = False
    fetched: list[str] = []
    planned: list[Path] = []

    def fetch(path: str) -> list[dict[str, Any]]:
        nonlocal failed
        fetched.append(path)
        if path == endpoint and not failed:
            failed = True
            if status is None:
                raise urllib.error.URLError("fixture-only-never-send")
            raise urllib.error.HTTPError(
                "https://fixture.invalid/fixture-only-never-send",
                status,
                "fixture-only-never-send",
                {},
                None,
            )
        return []

    monkeypatch.setattr(run, "fetch_inventory", lambda *a, **kw: fetch("/v1/models"))
    monkeypatch.setattr(run, "fetch_connections", lambda *a, **kw: fetch("/api/providers"))
    monkeypatch.setattr(cli, "_executor", lambda _: object())

    async def fixture_golden_path(goal: str, **kwargs: Any) -> SimpleNamespace:
        assert failed and len(spawned) == 2
        assert goal == "ship a focused change"
        assert kwargs["selector"] is not None
        run_dir = Path(kwargs["runs_root"]) / kwargs["run_id"]
        planned.append(run_dir)
        # Discovery has recovered. Stop before any model work; this fixture is
        # not a COMPLETE receipt or live routing proof.
        reason = "offline fixture reached planning after discovery recovered"
        EventLog(run_dir / "events.jsonl").emit("run_finished", outcome="BLOCKED", reason=reason)
        return SimpleNamespace(
            outcome="BLOCKED", reason=reason, run_dir=run_dir, receipt_path=run_dir / "receipt.json"
        )

    monkeypatch.setattr(run, "run_golden_path", fixture_golden_path)
    outcome = asyncio.run(asyncio.wait_for(supervisor.run(), timeout=5))
    assert outcome.state == "BLOCKED"
    assert outcome.reason == "offline fixture reached planning after discovery recovered"
    assert outcome.restarts == 1
    assert len(outcome.generations) == len(spawned) == 2
    first_state = "QUOTA" if status == 429 else "CRASHED"
    assert [generation["state"] for generation in outcome.generations] == [first_state, "BLOCKED"]
    assert planned == [supervisor.run_dir]
    first_fetches = ["/v1/models"] if endpoint == "/v1/models" else ["/v1/models", endpoint]
    assert fetched == [*first_fetches, "/v1/models", "/api/providers"]
    events = read_events(supervisor.run_dir / "events.jsonl")
    assert len([e for e in events if e["type"] == "run_finished"]) == 1
    assert not any(e["data"].get("phase") == "startup" for e in events)
    assert not any(e["data"].get("state") == "FAILED_CLOSED" for e in events)
    assert "fixture-only-never-send" not in (supervisor.run_dir / "events.jsonl").read_text()
    assert "fixture-only-never-send" not in (supervisor.run_dir / "controller-g0.log").read_text()
    assert not (supervisor.run_dir / "receipt.json").exists()


def test_unexpected_startup_bug_still_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VERDICT_OMNIROUTE_API_KEY", "fixture-only-never-send")
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / "state"))
    parser = argparse.ArgumentParser()
    cli.add_parsers(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["orchestrate", "ship it", "--repo", str(tmp_path), "--plain"])

    def broken_selector(*args: object, **kwargs: object) -> None:
        raise RuntimeError("unexpected implementation bug")

    monkeypatch.setattr(cli, "build_selector", broken_selector)
    with pytest.raises(RuntimeError, match="unexpected implementation bug"):
        cli._orchestrate(args)


@pytest.mark.parametrize("status", [None, 429, 500, 502, 503, 504])
def test_transient_discovery_failure_preserves_restart_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int | None
) -> None:
    monkeypatch.setenv("VERDICT_OMNIROUTE_API_KEY", "fixture-only-never-send")
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / "state"))
    parser = argparse.ArgumentParser()
    cli.add_parsers(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["orchestrate", "ship it", "--repo", str(tmp_path), "--plain"])

    def transient_selector(*args: object, **kwargs: object) -> None:
        raise GatewayDiscoveryError("/v1/models", status)

    monkeypatch.setattr(cli, "build_selector", transient_selector)
    with pytest.raises(GatewayDiscoveryError) as captured:
        cli._orchestrate(args)
    assert captured.value.retryable
    assert not list(tmp_path.rglob("events.jsonl"))


def test_discovery_error_conversion_never_copies_transport_secrets() -> None:
    import urllib.error

    error = urllib.error.HTTPError("https://secret.invalid", 401, "secret-token", {}, None)
    safe = _gateway_fetch_error("/v1/models", error)
    assert isinstance(safe, GatewayDiscoveryError)
    assert not safe.retryable
    assert "secret-token" not in str(safe)
    transient = _gateway_fetch_error("/v1/models", urllib.error.URLError("secret-token"))
    assert isinstance(transient, GatewayDiscoveryError) and transient.retryable
    assert "secret-token" not in str(transient)
