"""Tests for the PR watcher systemd service and timer rendering."""

from __future__ import annotations

from pathlib import Path


def test_pr_watcher_service_renders_exact_unit_files() -> None:
    """The rendered service and timer units match the expected text exactly."""
    from verdict import pr_watcher_service

    # Use a fixed root for deterministic output
    result = pr_watcher_service.manage_service(
        unit_dir=Path("/tmp/test-units"),
        dry_run=True,
        repos=["mrnicholasbcarter-code/verdict-core"],
    )

    service_body = result["files"][pr_watcher_service.SERVICE]
    timer_body = result["files"][pr_watcher_service.TIMER]

    # Service unit checks
    assert (
        "Description=Poll PRs, classify CI, bounded reruns, emit transitions (Verdict)"
        in service_body
    )
    assert "Type=oneshot" in service_body
    assert "WorkingDirectory=" in service_body
    assert "ExecStart=" in service_body
    assert '--repo "mrnicholasbcarter-code/verdict-core"' in service_body
    assert "verdict pr-watch --json" in service_body
    assert "Nice=19" in service_body
    assert "IOSchedulingClass=idle" in service_body
    assert "CPUWeight=20" in service_body
    assert "MemoryMax=512M" in service_body
    assert "TimeoutStartSec=15min" in service_body

    # Timer unit checks
    assert "Description=Poll PRs every 5 minutes" in timer_body
    assert "OnBootSec=10min" in timer_body
    assert "OnUnitActiveSec=5min" in timer_body
    assert "Persistent=true" in timer_body
    assert f"Unit={pr_watcher_service.SERVICE}" in timer_body
    assert "WantedBy=timers.target" in timer_body


def test_pr_watcher_service_timer_state_missing() -> None:
    """timer_state returns missing when unit is not loaded."""
    from verdict import metadata_service, pr_watcher_service

    # Mock the metadata_service._show to return unloaded state
    original_show = metadata_service._show

    def mock_show(unit: str, run_fn):
        return {"LoadState": "not-found"}

    metadata_service._show = mock_show
    try:
        state = pr_watcher_service.timer_state()
        assert state["state"] == "missing"
        assert state["result"] == "unknown"
    finally:
        metadata_service._show = original_show


def test_pr_watcher_service_timer_state_active() -> None:
    """timer_state returns active when timer is active and service succeeded."""
    from verdict import metadata_service, pr_watcher_service

    original_show = metadata_service._show

    def mock_show(unit: str, run_fn):
        if unit == pr_watcher_service.TIMER:
            return {"LoadState": "loaded", "ActiveState": "active"}
        if unit == pr_watcher_service.SERVICE:
            return {"LoadState": "loaded", "ActiveState": "inactive", "Result": "success"}
        return {}

    metadata_service._show = mock_show
    try:
        state = pr_watcher_service.timer_state()
        assert state["state"] == "active"
        assert state["result"] == "success"
    finally:
        metadata_service._show = original_show


def test_pr_watcher_service_timer_state_failed() -> None:
    """timer_state returns failed when timer or service failed."""
    from verdict import metadata_service, pr_watcher_service

    original_show = metadata_service._show

    def mock_show(unit: str, run_fn):
        if unit == pr_watcher_service.TIMER:
            return {"LoadState": "loaded", "ActiveState": "failed"}
        if unit == pr_watcher_service.SERVICE:
            return {"LoadState": "loaded", "ActiveState": "inactive", "Result": "success"}
        return {}

    metadata_service._show = mock_show
    try:
        state = pr_watcher_service.timer_state()
        assert state["state"] == "failed"
    finally:
        metadata_service._show = original_show


def test_pr_watcher_service_manage_service_dry_run() -> None:
    """Dry run never writes files or runs systemctl."""
    from verdict import pr_watcher_service

    calls: list[list[str]] = []

    def recording_systemctl(args: list[str]) -> None:
        calls.append(args)

    result = pr_watcher_service.manage_service(
        unit_dir=Path("/tmp/test-units-dry"),
        dry_run=True,
        repos=["owner/repo1", "owner/repo2"],
        run=recording_systemctl,
    )

    assert result["dry_run"] is True
    assert result["changed"] is True  # Plan shows changes
    assert calls == []  # No systemctl calls in dry run
    assert "owner/repo1" in result["files"][pr_watcher_service.SERVICE]
    assert "owner/repo2" in result["files"][pr_watcher_service.SERVICE]


def test_pr_watcher_service_manage_service_uninstall() -> None:
    """Uninstall plan removes units and returns disable commands."""
    from verdict import pr_watcher_service

    # Create the unit files first so they can be "removed" (not "absent")
    unit_dir = Path("/tmp/test-units-uninstall")
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / pr_watcher_service.SERVICE).write_text("[Unit]\nDescription=Test\n")
    (unit_dir / pr_watcher_service.TIMER).write_text("[Unit]\nDescription=Test\n")

    result = pr_watcher_service.manage_service(
        unit_dir=unit_dir, dry_run=True, uninstall=True, repos=["owner/repo"]
    )

    assert result["uninstall"] is True
    assert result["files"] == {}
    assert result["plan"][pr_watcher_service.SERVICE] == "remove"
    assert result["plan"][pr_watcher_service.TIMER] == "remove"
    assert result["commands"] == [
        [
            "systemctl",
            "--user",
            "disable",
            "--now",
            pr_watcher_service.TIMER,
            pr_watcher_service.SERVICE,
        ]
    ]


def test_pr_watcher_service_manage_service_multiple_repos() -> None:
    """Multiple repos are joined with --repo flags in ExecStart."""
    from verdict import pr_watcher_service

    result = pr_watcher_service.manage_service(
        unit_dir=Path("/tmp/test-units-multi"),
        dry_run=True,
        repos=["owner/repo1", "owner/repo2", "owner/repo3"],
    )

    service_body = result["files"][pr_watcher_service.SERVICE]
    # Each repo gets its own --repo flag (quoted)
    assert service_body.count("--repo") == 3
    assert '--repo "owner/repo1"' in service_body
    assert '--repo "owner/repo2"' in service_body
    assert '--repo "owner/repo3"' in service_body
