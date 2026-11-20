"""Tests for the admission snapshot keeper service and its doctor rows.

Fake transport, temp VERDICT_HOME, dry-run systemctl — no network, no inference.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from verdict.admit_snapshot_service import SERVICE, TIMER, manage_service
from verdict.catalog_cache import put_cached_catalog
from verdict.free_tier_admit import LiveAdmitError, load_omniroute_admit_snapshot

BASE = "http://127.0.0.1:20128"
ROUTE = "openrouter/nvidia/nemotron-3-nano-30b-a3b:free"

#: Minimal surface payloads that satisfy load_omniroute_admit_snapshot.
PAYLOADS: dict[str, object] = {
    "/v1/models": {"data": [{"id": ROUTE, "object": "model", "owned_by": "openrouter"}]},
    "/api/free-tier/summary": {
        "perModel": [{"modelId": "nvidia/nemotron-3-nano-30b-a3b:free", "provider": "openrouter"}]
    },
    "/api/v1/me/status": {"accountQuotas": []},
    "/api/providers": {
        "connections": [{"provider": "openrouter", "isActive": True, "testStatus": "active"}],
        "total": 1,
    },
    # Health surfaces (fetched by load_omniroute_health inside the loader).
    "/api/usage/model-latency-stats": {"entries": []},
    "/api/resilience/connections": {"connections": []},
}

#: Paths that must NEVER be called — inference endpoints.
_INFERENCE_PATHS = {
    "/v1/chat/completions",
    "/chat/completions",
    "/v1/completions",
    "/completions",
    "/v1/responses",
    "/responses",
    "/v1/messages",
    "/messages",
}


def _transport(
    failing: Callable[[str, int], bool] | None = None, seen: list[str] | None = None
) -> httpx.MockTransport:
    """Build a MockTransport that serves PAYLOADS and optionally fails some paths."""
    counts: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        # Prove: no inference endpoint is ever called.
        assert path not in _INFERENCE_PATHS, f"inference endpoint called: {path}"
        assert "auto" not in path and "combo" not in path, f"opaque route path called: {path}"
        counts[path] = counts.get(path, 0) + 1
        if seen is not None:
            seen.append(path)
        if failing is not None and failing(path, counts[path]):
            raise httpx.ReadTimeout("slow", request=request)
        if path not in PAYLOADS:
            return httpx.Response(404)
        return httpx.Response(200, json=PAYLOADS[path])

    return httpx.MockTransport(handler)


@pytest.fixture()
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated VERDICT_HOME; returns the last-good path."""
    monkeypatch.setenv("VERDICT_CATALOG_CACHE", str(tmp_path / "catalog-cache.json"))
    return tmp_path / "admit-last-good.json"


def _load(last_good: Path, transport: httpx.MockTransport, **kwargs: Any) -> Any:
    """Call load_omniroute_admit_snapshot with the fake transport."""
    return load_omniroute_admit_snapshot(
        BASE,
        "test-token",
        transport=transport,
        last_good_path=last_good,
        sleep=lambda _: None,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 1. Snapshot is written atomically
# ---------------------------------------------------------------------------


def test_snapshot_written_atomically(isolated: Path) -> None:
    """A successful load creates the last-good file; no partial write exists."""
    assert not isolated.exists()
    snapshot = _load(isolated, _transport())
    assert isolated.exists()
    # File must be valid JSON.
    data = json.loads(isolated.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    # Must be 0600 (owner read/write only).
    mode = isolated.stat().st_mode & 0o777
    assert mode == 0o600, f"expected 0600 got {oct(mode)}"
    # Snapshot must be non-empty and have no stale surfaces.
    assert snapshot.stale_served == ()
    assert snapshot.catalog
    assert snapshot.connections


# ---------------------------------------------------------------------------
# 2. Failed fetch keeps the old last-good file intact
# ---------------------------------------------------------------------------


def test_failed_fetch_keeps_existing_last_good(isolated: Path, tmp_path: Path) -> None:
    """When all surfaces fail and no last-good exists, the file is not created."""
    # Pre-populate the catalog cache so the catalog fallback works.
    put_cached_catalog(
        BASE, "test-token", PAYLOADS["/v1/models"], cache_path=tmp_path / "catalog-cache.json"
    )
    # First write a valid last-good file.
    _load(isolated, _transport())
    original_content = isolated.read_text(encoding="utf-8")
    original_mtime = isolated.stat().st_mtime

    # Now fail all non-catalog surfaces so the load falls back to last-good.
    def always_fail(path: str, _n: int) -> bool:
        return path in {"/api/providers", "/api/free-tier/summary", "/api/v1/me/status"}

    # Give it a fresh last-good path so it reads back what we wrote.
    snapshot = _load(isolated, _transport(always_fail))
    # Stale surfaces were used — not a full success.
    assert snapshot.stale_served != ()
    # File content must be unchanged (the stale fallback doesn't rewrite).
    assert isolated.read_text(encoding="utf-8") == original_content
    # mtime unchanged (no write occurred on the stale path).
    assert isolated.stat().st_mtime == original_mtime


def test_total_failure_raises_and_no_file_created(isolated: Path, tmp_path: Path) -> None:
    """When no last-good exists and fetch fails, LiveAdmitError is raised; no file created."""
    # Fail every surface including catalog (no catalog cache pre-seeded).
    with pytest.raises(LiveAdmitError):
        _load(isolated, _transport(lambda _p, _n: True))
    assert not isolated.exists()


# ---------------------------------------------------------------------------
# 3. No inference endpoint is ever called
# ---------------------------------------------------------------------------


def test_no_inference_endpoint_called(isolated: Path) -> None:
    """The transport handler asserts no inference path is hit (proven in-band)."""
    seen: list[str] = []
    _load(isolated, _transport(seen=seen))
    # Confirm we actually hit some paths.
    assert seen, "expected at least one HTTP call"
    # None of them are inference paths.
    for path in seen:
        assert path not in _INFERENCE_PATHS, f"inference path hit: {path}"
    # Confirm the exact set of admission-surface paths.
    admission_paths = {
        p for p in seen if not p.startswith("/api/usage") and not p.startswith("/api/resilience")
    }
    expected_admission = {
        "/v1/models",
        "/api/free-tier/summary",
        "/api/v1/me/status",
        "/api/providers",
    }
    assert admission_paths == expected_admission, (
        f"unexpected paths: {admission_paths - expected_admission}"
    )


# ---------------------------------------------------------------------------
# 4. Doctor freshness row states
# ---------------------------------------------------------------------------


def test_freshness_row_ok_when_file_young(tmp_path: Path) -> None:
    from verdict.doctor_diagnostics import _admit_snapshot_freshness_row

    store = tmp_path / "admit-last-good.json"
    now = datetime.now(timezone.utc)
    # Write a last-good file with a fetched_at that is 2 minutes ago.
    fetched_at = (now - timedelta(minutes=2)).isoformat()
    store.write_text(
        json.dumps(
            {"http://127.0.0.1:20128": {"free_tier": {"fetched_at": fetched_at, "payload": {}}}}
        ),
        encoding="utf-8",
    )
    row = _admit_snapshot_freshness_row(now=now, path=store)
    assert row["status"] == "ok"
    assert "2m" in row["detail"]


def test_freshness_row_warning_when_file_stale(tmp_path: Path) -> None:
    from verdict.doctor_diagnostics import _admit_snapshot_freshness_row

    store = tmp_path / "admit-last-good.json"
    now = datetime.now(timezone.utc)
    # Write a last-good file with a fetched_at that is 20 minutes ago (> 15 min threshold).
    fetched_at = (now - timedelta(minutes=20)).isoformat()
    store.write_text(
        json.dumps(
            {"http://127.0.0.1:20128": {"free_tier": {"fetched_at": fetched_at, "payload": {}}}}
        ),
        encoding="utf-8",
    )
    row = _admit_snapshot_freshness_row(now=now, path=store)
    assert row["status"] == "warning"
    assert "20m" in row["detail"]
    assert "repair" in row["detail"]


def test_freshness_row_warning_when_file_missing(tmp_path: Path) -> None:
    from verdict.doctor_diagnostics import _admit_snapshot_freshness_row

    store = tmp_path / "admit-last-good.json"
    row = _admit_snapshot_freshness_row(path=store)
    assert row["status"] == "warning"
    assert "missing" in row["detail"]


# ---------------------------------------------------------------------------
# 5. Timer row states
# ---------------------------------------------------------------------------


def test_timer_row_ok_when_active(tmp_path: Path) -> None:
    from verdict.doctor_diagnostics import _admit_snapshot_timer_row

    def fake_timer_state(unit: str) -> dict[str, Any]:
        return {"state": "active", "result": "success"}

    row = _admit_snapshot_timer_row(timer_state=fake_timer_state)
    assert row["status"] == "ok"
    assert TIMER in row["detail"]


def test_timer_row_failed_when_missing(tmp_path: Path) -> None:
    from verdict.doctor_diagnostics import _admit_snapshot_timer_row

    def fake_timer_state(unit: str) -> dict[str, Any]:
        return {"state": "missing", "result": "unknown"}

    row = _admit_snapshot_timer_row(timer_state=fake_timer_state)
    assert row["status"] == "failed"
    assert "repair" in row["detail"]
    assert "setup" in row["detail"]


def test_timer_row_failed_when_inactive(tmp_path: Path) -> None:
    from verdict.doctor_diagnostics import _admit_snapshot_timer_row

    def fake_timer_state(unit: str) -> dict[str, Any]:
        return {"state": "inactive", "result": "unknown"}

    row = _admit_snapshot_timer_row(timer_state=fake_timer_state)
    assert row["status"] == "failed"
    assert "enable" in row["detail"]


# ---------------------------------------------------------------------------
# 6. Setup action rendering in dry-run (no systemctl)
# ---------------------------------------------------------------------------


def test_manage_service_dry_run_renders_units(tmp_path: Path) -> None:
    """manage_service dry-run returns a plan dict without writing files."""
    unit_dir = tmp_path / "systemd-user"
    result = manage_service(unit_dir=unit_dir, dry_run=True)
    assert result["dry_run"] is True
    plan = result["plan"]
    assert SERVICE in plan
    assert TIMER in plan
    # dry_run must not write any unit files.
    assert not unit_dir.exists() or not (unit_dir / TIMER).exists()


def test_manage_service_dry_run_uninstall(tmp_path: Path) -> None:
    """Uninstall dry-run returns a plan dict without writing files or calling systemctl."""
    unit_dir = tmp_path / "systemd-user"
    result = manage_service(unit_dir=unit_dir, dry_run=True, uninstall=True)
    assert result["dry_run"] is True
    # Plan must contain entries for both units (absent or noop — both are ok pre-install).
    plan = result["plan"]
    assert SERVICE in plan
    assert TIMER in plan
    # dry_run must not create the directory.
    assert not unit_dir.exists()


def test_manage_service_install_writes_units(tmp_path: Path) -> None:
    """manage_service install (not dry-run) writes service and timer unit files."""
    unit_dir = tmp_path / "systemd-user"
    systemctl_calls: list[list[str]] = []
    manage_service(
        unit_dir=unit_dir, dry_run=False, run=lambda args: systemctl_calls.append(list(args))
    )
    assert (unit_dir / SERVICE).exists()
    assert (unit_dir / TIMER).exists()
    # Service file must reference our command and not any inference path.
    svc_body = (unit_dir / SERVICE).read_text(encoding="utf-8")
    assert "verdict admission refresh" in svc_body
    assert "chat" not in svc_body
    assert "completions" not in svc_body
    # Timer must use OnUnitInactiveSec and OnStartupSec, NOT OnCalendar.
    timer_body = (unit_dir / TIMER).read_text(encoding="utf-8")
    assert "OnUnitInactiveSec=300" in timer_body
    assert "OnStartupSec=60" in timer_body
    assert "OnCalendar" not in timer_body
    assert "Persistent" not in timer_body
    # systemctl enable was called.
    assert any("enable" in " ".join(c) for c in systemctl_calls)


def test_manage_service_uninstall_removes_units(tmp_path: Path) -> None:
    """manage_service uninstall removes the unit files."""
    unit_dir = tmp_path / "systemd-user"
    systemctl_calls: list[list[str]] = []
    # Install first.
    manage_service(
        unit_dir=unit_dir, dry_run=False, run=lambda args: systemctl_calls.append(list(args))
    )
    assert (unit_dir / SERVICE).exists()
    # Uninstall.
    manage_service(
        unit_dir=unit_dir,
        dry_run=False,
        uninstall=True,
        run=lambda args: systemctl_calls.append(list(args)),
    )
    assert not (unit_dir / SERVICE).exists()
    assert not (unit_dir / TIMER).exists()


# ---------------------------------------------------------------------------
# 7. Bug fixes: mkdir before save_rollback; timer_state catches exceptions
# ---------------------------------------------------------------------------


def test_manage_service_install_on_nonexistent_directory(tmp_path: Path) -> None:
    """manage_service must succeed when the unit_dir does not yet exist (fresh host)."""
    unit_dir = tmp_path / "deep" / "nested" / "systemd" / "user"
    assert not unit_dir.exists(), "precondition: directory must not exist"
    manage_service(unit_dir=unit_dir, dry_run=False, run=lambda args: None)
    assert (unit_dir / SERVICE).exists(), "service unit must be written"
    assert (unit_dir / TIMER).exists(), "timer unit must be written"


def test_timer_state_returns_missing_when_systemctl_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """timer_state catches exceptions from metadata_service._show and returns 'missing'."""
    import verdict.admit_snapshot_service as asm

    def _raise(*args: Any, **kwargs: Any) -> None:
        raise OSError("systemctl not available")

    import verdict.metadata_service as metadata_service

    monkeypatch.setattr(metadata_service, "_run_show", _raise)
    result = asm.timer_state()
    assert result["state"] == "missing"
    assert result["result"] == "unknown"


# ---------------------------------------------------------------------------
# 8. installed_service_rows includes both new rows
# ---------------------------------------------------------------------------


def test_installed_service_rows_includes_admit_snapshot_rows(tmp_path: Path) -> None:
    """installed_service_rows returns the timer row and freshness row."""
    from verdict.doctor_diagnostics import installed_service_rows

    def fake_timer_state(unit: str) -> dict[str, Any]:
        if unit == TIMER:
            return {"state": "active", "result": "success"}
        return {"state": "active", "result": "success"}

    rows = installed_service_rows(now=datetime.now(timezone.utc), timer_state=fake_timer_state)
    labels = [r["label"] for r in rows]
    assert "Admit-snapshot keeper timer" in labels
    assert "Admit-last-good freshness" in labels


# ---------------------------------------------------------------------------
# 9. systemd must reload unit files after they change
# ---------------------------------------------------------------------------


def test_install_reloads_systemd_before_enabling_the_timer(tmp_path: Path) -> None:
    """A changed unit must be reloaded before enable --now, as every other service installer does."""
    unit_dir = tmp_path / "systemd-user"
    calls: list[list[str]] = []
    manage_service(unit_dir=unit_dir, dry_run=False, run=lambda args: calls.append(list(args)))
    reload_idx = calls.index(["systemctl", "--user", "daemon-reload"])
    enable_idx = next(i for i, c in enumerate(calls) if "enable" in c)
    assert reload_idx < enable_idx, calls


def test_unchanged_install_does_not_reload(tmp_path: Path) -> None:
    """A second install with identical units changes nothing and needs no reload."""
    unit_dir = tmp_path / "systemd-user"
    manage_service(unit_dir=unit_dir, dry_run=False, run=lambda args: None)
    calls: list[list[str]] = []
    manage_service(unit_dir=unit_dir, dry_run=False, run=lambda args: calls.append(list(args)))
    assert ["systemctl", "--user", "daemon-reload"] not in calls, calls


def test_uninstall_reloads_systemd_after_removing_units(tmp_path: Path) -> None:
    """Removing units must be followed by a reload so systemd forgets them."""
    unit_dir = tmp_path / "systemd-user"
    manage_service(unit_dir=unit_dir, dry_run=False, run=lambda args: None)
    calls: list[list[str]] = []
    manage_service(
        unit_dir=unit_dir, dry_run=False, uninstall=True, run=lambda args: calls.append(list(args))
    )
    assert calls[-1] == ["systemctl", "--user", "daemon-reload"], calls
    assert not (unit_dir / TIMER).exists()
