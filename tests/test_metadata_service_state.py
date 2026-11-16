"""Metadata timer lifecycle regressions (all state is temporary/local)."""

from __future__ import annotations

import argparse
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from verdict.commands.parsers_autodev import register
from verdict.metadata_service import SERVICE, TIMER, manage_service, timer_state


def test_metadata_install_service_cli_accepts_dry_run() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    register(commands)

    args = parser.parse_args(["metadata", "install-service", "--dry-run"])

    assert args.command == "metadata"
    assert args.metadata_command == "install-service"
    assert args.dry_run is True


def test_metadata_timer_dry_run_install_and_repeat_are_side_effect_bounded(tmp_path: Path) -> None:
    unit_dir = tmp_path / "systemd" / "user"
    calls: list[list[str]] = []

    preview = manage_service(unit_dir=unit_dir, dry_run=True, run=calls.append)
    assert preview["changed"] is True
    assert calls == []
    assert not unit_dir.exists()
    assert "OnBootSec=5m" in preview["files"][TIMER]
    assert "OnUnitInactiveSec=6h" in preview["files"][TIMER]
    assert "metadata refresh --include-p1" in preview["files"][SERVICE]

    installed = manage_service(unit_dir=unit_dir, run=calls.append)
    assert installed["changed"] is True
    assert (unit_dir / TIMER).read_text() == preview["files"][TIMER]
    assert (unit_dir / SERVICE).read_text() == preview["files"][SERVICE]
    assert calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", TIMER],
        ["systemctl", "--user", "try-restart", SERVICE],
    ]

    calls.clear()
    prior_stat = (unit_dir / TIMER).stat()
    repeat = manage_service(unit_dir=unit_dir, run=calls.append)
    assert repeat["changed"] is False
    assert calls == []
    assert (unit_dir / TIMER).stat().st_mtime_ns == prior_stat.st_mtime_ns


def test_metadata_timer_state_exposes_failure_and_trigger() -> None:
    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        assert "ExecMainExitTimestamp" in args[-2]
        unit = args[-1]
        result = "exit-code" if unit == SERVICE else "success"
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=(
                "LoadState=loaded\nActiveState=active\nSubState=waiting\n"
                "LastTriggerUSec=Sun 2026-10-18 12:00:00 UTC\n"
                "ExecMainExitTimestamp=Sun 2026-10-18 12:00:05 UTC\n"
                f"Result={result}\nExecMainStatus=1\n"
            ),
            stderr="",
        )

    state = timer_state(run=run)
    assert state["installed"] is True
    assert state["active"] is False
    assert state["last_trigger"] == "Sun 2026-10-18 12:00:00 UTC"
    assert state["last_completion"] == "Sun 2026-10-18 12:00:05 UTC"
    assert state["result"] == "exit-code"
    assert state["failed"] is True
    assert state["state"] == "failed"


def test_metadata_timer_install_cadence_source_failure_and_doctor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cadence render, idempotent install, source-failure doctor, and recovery.

    Covers acceptance A24 #1-#2: dry-run and declined setup make no calls;
    one idempotent install uses the injected runner and a temporary unit dir;
    the rendered unit pins the 6h cadence and boot trigger; a
    never-completed/failed timer is observable by doctor; a failed source is
    reported (never fabricated) and later success recovers.
    """
    from verdict import doctor_diagnostics as dd
    from verdict.metadata import METADATA_SCHEMA_VERSION, SourceStatus, save_store
    from verdict.metadata.store import MetadataSnapshot
    from verdict.metadata_service import SERVICE, TIMER, manage_service

    unit_dir = tmp_path / "systemd" / "user"
    calls: list[list[str]] = []

    # Dry-run: no systemd/state/provider calls, nothing written.
    preview = manage_service(unit_dir=unit_dir, dry_run=True, run=calls.append)
    assert calls == []
    assert not unit_dir.exists()
    assert preview["changed"] is True
    assert "OnBootSec=5m" in preview["files"][TIMER]
    assert "OnUnitInactiveSec=6h" in preview["files"][TIMER]
    assert "metadata refresh --include-p1" in preview["files"][SERVICE]

    # One idempotent install via the injected runner into the temp unit dir.
    installed = manage_service(unit_dir=unit_dir, run=calls.append)
    assert installed["changed"] is True
    assert (unit_dir / TIMER).read_text() == preview["files"][TIMER]
    assert calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", TIMER],
        ["systemctl", "--user", "try-restart", SERVICE],
    ]
    calls.clear()
    assert manage_service(unit_dir=unit_dir, run=calls.append)["changed"] is False
    assert calls == []

    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / ".verdict"))
    fetched_at = "2026-10-16T11:55:00Z"
    now = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)

    def save_snapshot(sources: dict[str, SourceStatus]) -> None:
        save_store(
            MetadataSnapshot(
                schema_version=METADATA_SCHEMA_VERSION,
                refreshed_at=fetched_at,
                sources=sources,
                records=(),
            )
        )

    ok_sources = {
        name: SourceStatus(source=name, status="ok", fetched_at=fetched_at, record_count=1)
        for name in ("models.dev", "models.dev.models", "litellm")
    }
    save_snapshot(ok_sources)

    states: dict[str, dict[str, object]] = {
        "metadata": {
            "installed": True,
            "failed": True,
            "active": False,
            "state": "failed",
            "last_trigger": None,
            "result": "exit-code",
        }
    }

    def timer_state(unit: str) -> dict[str, object]:
        return states["metadata"]

    # Never-completed / failed timer is observable by doctor.
    failed = dd.installed_service_rows(now=now, timer_state=timer_state)
    failed_row = next(row for row in failed if row["label"] == "Metadata refresh timer")
    assert failed_row["status"] == "failed"
    assert "never completed" in failed_row["detail"]

    # Source failure: one public source errors; doctor reports it (no fabricated
    # rating) and the timer still needs a refresh repair.
    states["metadata"] = {
        "installed": True,
        "failed": False,
        "active": True,
        "state": "active",
        "last_trigger": "Fri 2026-10-16 11:55:00 UTC",
        "last_completion": "Fri 2026-10-16 11:56:00 UTC",
        "result": "success",
    }
    failing_sources = dict(ok_sources)
    failing_sources["models.dev"] = SourceStatus(
        source="models.dev", status="error", fetched_at=fetched_at, record_count=0, reason="boom"
    )
    save_snapshot(failing_sources)
    source_failed = dd.installed_service_rows(now=now, timer_state=timer_state)
    source_row = next(row for row in source_failed if row["label"] == "Metadata refresh timer")
    assert source_row["status"] == "failed"
    assert "source models.dev failed" in source_row["detail"]

    # Later success recovers: fresh sources and a completed refresh are ok.
    save_snapshot(ok_sources)
    recovered = dd.installed_service_rows(now=now, timer_state=timer_state)
    recovered_row = next(row for row in recovered if row["label"] == "Metadata refresh timer")
    assert recovered_row["status"] == "ok"
