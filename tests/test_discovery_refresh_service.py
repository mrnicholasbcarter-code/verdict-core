"""Tests for the discovery registry refresh timer service and its doctor rows.

Fake transport, temp VERDICT_HOME, dry-run systemctl — no network, no inference.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from verdict.discovery.registry import REGISTRY_SCHEMA, REGISTRY_TTL_S
from verdict.discovery_refresh_service import INTERVAL_S, SERVICE, TIMER, manage_service

# ---------------------------------------------------------------------------
# 1. Timer interval invariant: must keep entries inside REGISTRY_TTL_S
# ---------------------------------------------------------------------------


def test_interval_keeps_entries_inside_registry_ttl() -> None:
    """INTERVAL_S must be strictly less than REGISTRY_TTL_S so entries never reach the TTL."""
    assert INTERVAL_S < REGISTRY_TTL_S, (
        f"INTERVAL_S={INTERVAL_S} must be < REGISTRY_TTL_S={REGISTRY_TTL_S}"
    )
    assert INTERVAL_S <= REGISTRY_TTL_S // 2, (
        "INTERVAL_S should be at most half the TTL for a one-missed-fire safety margin"
    )


# ---------------------------------------------------------------------------
# 2. Service unit uses --free-only --force (no paid or subscription calls)
# ---------------------------------------------------------------------------


def test_service_unit_uses_free_only_and_force(tmp_path: Path) -> None:
    """The service ExecStart must include --free-only and --force flags."""
    unit_dir = tmp_path / "systemd-user"
    manage_service(unit_dir=unit_dir, dry_run=False, run=lambda args: None)
    svc_body = (unit_dir / SERVICE).read_text(encoding="utf-8")
    assert "--free-only" in svc_body, "service must pass --free-only to prevent subscription probes"
    assert "--force" in svc_body, "service must pass --force so every timer fire probes"
    # Confirm no inference endpoints appear in the unit file.
    for inference_path in ("/chat/completions", "/completions", "/responses", "/messages"):
        assert inference_path not in svc_body, f"inference path {inference_path!r} in service unit"


def test_service_unit_uses_discovery_refresh_command(tmp_path: Path) -> None:
    """The service must invoke 'verdict discovery refresh', not admission refresh."""
    unit_dir = tmp_path / "systemd-user"
    manage_service(unit_dir=unit_dir, dry_run=False, run=lambda args: None)
    svc_body = (unit_dir / SERVICE).read_text(encoding="utf-8")
    assert "discovery refresh" in svc_body
    assert "admission refresh" not in svc_body


# ---------------------------------------------------------------------------
# 3. Timer unit uses correct interval and startup delay
# ---------------------------------------------------------------------------


def test_timer_unit_uses_on_unit_inactive_sec(tmp_path: Path) -> None:
    """Timer must use OnUnitInactiveSec (not OnCalendar or Persistent)."""
    unit_dir = tmp_path / "systemd-user"
    manage_service(unit_dir=unit_dir, dry_run=False, run=lambda args: None)
    timer_body = (unit_dir / TIMER).read_text(encoding="utf-8")
    assert f"OnUnitInactiveSec={INTERVAL_S}" in timer_body
    assert "OnStartupSec=" in timer_body
    assert "OnCalendar" not in timer_body
    assert "Persistent" not in timer_body


# ---------------------------------------------------------------------------
# 4. Dry-run renders units without writing files
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
    """Uninstall dry-run returns a plan dict without writing files."""
    unit_dir = tmp_path / "systemd-user"
    result = manage_service(unit_dir=unit_dir, dry_run=True, uninstall=True)
    assert result["dry_run"] is True
    plan = result["plan"]
    assert SERVICE in plan
    assert TIMER in plan
    assert not unit_dir.exists()


# ---------------------------------------------------------------------------
# 5. Install writes units
# ---------------------------------------------------------------------------


def test_manage_service_install_writes_units(tmp_path: Path) -> None:
    """manage_service install (not dry-run) writes service and timer unit files."""
    unit_dir = tmp_path / "systemd-user"
    systemctl_calls: list[list[str]] = []
    manage_service(
        unit_dir=unit_dir, dry_run=False, run=lambda args: systemctl_calls.append(list(args))
    )
    assert (unit_dir / SERVICE).exists()
    assert (unit_dir / TIMER).exists()
    # systemctl enable was called.
    assert any("enable" in " ".join(c) for c in systemctl_calls)


def test_manage_service_uninstall_removes_units(tmp_path: Path) -> None:
    """manage_service uninstall removes the unit files."""
    unit_dir = tmp_path / "systemd-user"
    systemctl_calls: list[list[str]] = []
    manage_service(
        unit_dir=unit_dir, dry_run=False, run=lambda args: systemctl_calls.append(list(args))
    )
    assert (unit_dir / SERVICE).exists()
    manage_service(
        unit_dir=unit_dir,
        dry_run=False,
        uninstall=True,
        run=lambda args: systemctl_calls.append(list(args)),
    )
    assert not (unit_dir / SERVICE).exists()
    assert not (unit_dir / TIMER).exists()


# ---------------------------------------------------------------------------
# 6. mkdir before save_rollback (fresh host)
# ---------------------------------------------------------------------------


def test_manage_service_install_on_nonexistent_directory(tmp_path: Path) -> None:
    """manage_service must succeed when the unit_dir does not yet exist."""
    unit_dir = tmp_path / "deep" / "nested" / "systemd" / "user"
    assert not unit_dir.exists(), "precondition: directory must not exist"
    manage_service(unit_dir=unit_dir, dry_run=False, run=lambda args: None)
    assert (unit_dir / SERVICE).exists(), "service unit must be written"
    assert (unit_dir / TIMER).exists(), "timer unit must be written"


# ---------------------------------------------------------------------------
# 7. daemon-reload before enable; no reload on unchanged second install
# ---------------------------------------------------------------------------


def test_install_reloads_systemd_before_enabling_the_timer(tmp_path: Path) -> None:
    """A changed unit must be reloaded before enable --now."""
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


# ---------------------------------------------------------------------------
# 8. timer_state catches systemctl exceptions
# ---------------------------------------------------------------------------


def test_timer_state_returns_missing_when_systemctl_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """timer_state catches exceptions and returns 'missing'."""
    import verdict.discovery_refresh_service as drs

    def _raise(*args: Any, **kwargs: Any) -> None:
        raise OSError("systemctl not available")

    import verdict.metadata_service as metadata_service

    monkeypatch.setattr(metadata_service, "_run_show", _raise)
    result = drs.timer_state()
    assert result["state"] == "missing"
    assert result["result"] == "unknown"


# ---------------------------------------------------------------------------
# 9. Doctor timer row states
# ---------------------------------------------------------------------------


def test_timer_row_ok_when_active(tmp_path: Path) -> None:
    from verdict.doctor_diagnostics import _discovery_refresh_timer_row

    def fake_timer_state(unit: str) -> dict[str, Any]:
        return {"state": "active", "result": "success"}

    row = _discovery_refresh_timer_row(timer_state=fake_timer_state)
    assert row["status"] == "ok"
    assert TIMER in row["detail"]


def test_timer_row_failed_when_missing(tmp_path: Path) -> None:
    from verdict.doctor_diagnostics import _discovery_refresh_timer_row

    def fake_timer_state(unit: str) -> dict[str, Any]:
        return {"state": "missing", "result": "unknown"}

    row = _discovery_refresh_timer_row(timer_state=fake_timer_state)
    assert row["status"] == "failed"
    assert "repair" in row["detail"]
    assert "setup" in row["detail"]


def test_timer_row_failed_when_inactive(tmp_path: Path) -> None:
    from verdict.doctor_diagnostics import _discovery_refresh_timer_row

    def fake_timer_state(unit: str) -> dict[str, Any]:
        return {"state": "inactive", "result": "unknown"}

    row = _discovery_refresh_timer_row(timer_state=fake_timer_state)
    assert row["status"] == "failed"
    assert "enable" in row["detail"]


# ---------------------------------------------------------------------------
# 10. Doctor freshness row states
# ---------------------------------------------------------------------------


def test_freshness_row_ok_when_registry_young(tmp_path: Path) -> None:
    from verdict.doctor_diagnostics import _discovery_refresh_freshness_row

    now = datetime.datetime.now(datetime.timezone.utc)
    reg = tmp_path / "registry.json"
    reg.write_text(
        json.dumps(
            {
                "schema": REGISTRY_SCHEMA,
                "written_at": (now - datetime.timedelta(hours=1)).isoformat(),
                "ttl_s": REGISTRY_TTL_S,
                "entries": [],
                "counts": {"tool_ok": 3},
            }
        ),
        encoding="utf-8",
    )
    row = _discovery_refresh_freshness_row(now=now, path=reg)
    assert row["status"] == "ok"
    assert "1.0h" in row["detail"]


def test_freshness_row_warning_when_registry_at_ttl(tmp_path: Path) -> None:
    from verdict.discovery_refresh_service import REGISTRY_FRESHNESS_WARN_S
    from verdict.doctor_diagnostics import _discovery_refresh_freshness_row

    now = datetime.datetime.now(datetime.timezone.utc)
    reg = tmp_path / "registry.json"
    # At exactly the warn threshold (12 h = TTL) → warning.
    reg.write_text(
        json.dumps(
            {
                "schema": REGISTRY_SCHEMA,
                "written_at": (
                    now - datetime.timedelta(seconds=REGISTRY_FRESHNESS_WARN_S)
                ).isoformat(),
                "ttl_s": REGISTRY_TTL_S,
                "entries": [],
                "counts": {"tool_ok": 0},
            }
        ),
        encoding="utf-8",
    )
    row = _discovery_refresh_freshness_row(now=now, path=reg)
    assert row["status"] == "warning"
    assert "repair" in row["detail"]


def test_freshness_row_warning_when_registry_missing(tmp_path: Path) -> None:
    from verdict.doctor_diagnostics import _discovery_refresh_freshness_row

    reg = tmp_path / "no-such-file.json"
    row = _discovery_refresh_freshness_row(path=reg)
    assert row["status"] == "warning"
    assert "missing" in row["detail"]


# ---------------------------------------------------------------------------
# 11. installed_service_rows includes both new rows
# ---------------------------------------------------------------------------


def test_installed_service_rows_includes_discovery_refresh_rows(tmp_path: Path) -> None:
    """installed_service_rows returns the timer row and freshness row."""
    from verdict.doctor_diagnostics import installed_service_rows

    def fake_timer_state(unit: str) -> dict[str, Any]:
        return {"state": "active", "result": "success"}

    rows = installed_service_rows(
        now=datetime.datetime.now(datetime.timezone.utc), timer_state=fake_timer_state
    )
    labels = [r["label"] for r in rows]
    assert "Discovery registry refresh timer" in labels
    assert "Discovery registry freshness" in labels


# ---------------------------------------------------------------------------
# 12. Behavioural proof: --free-only filters out subscription routes
# ---------------------------------------------------------------------------


def test_free_only_excludes_subscription_routes() -> None:
    """run_refresh_action(free_only=True) must pass only 'free' triples to run_refresh."""
    from verdict.discovery.model import Route
    from verdict.discovery.refresh import CatalogResult, run_refresh_action

    free_route = Route(
        provider="omniroute",
        model="free/model-a",
        base_url="http://localhost:20128/v1",
        auth="env:OMNIROUTE_API_KEY",
        source="omniroute_catalog",
        upstream=None,
        billing="free",
    )
    sub_route = Route(
        provider="omniroute",
        model="subscription/model-b",
        base_url="http://localhost:20128/v1",
        auth="env:OMNIROUTE_API_KEY",
        source="omniroute_catalog",
        upstream=None,
        billing="subscription",
    )
    mixed_catalog = CatalogResult(
        routes=[
            (free_route, "free", "plan_free"),
            (sub_route, "subscription", "plan_subscription"),
        ],
        available=True,
    )

    captured_routes: list[Any] = []

    def fake_load_probeable(env: Any = None) -> CatalogResult:
        return mixed_catalog

    def fake_run_refresh(routes: Any, **kwargs: Any) -> Any:
        captured_routes.extend(routes)
        import datetime as _dt

        from verdict.discovery.registry import REGISTRY_SCHEMA, REGISTRY_TTL_S, DiscoveryRegistry

        return DiscoveryRegistry(
            schema=REGISTRY_SCHEMA,
            written_at=_dt.datetime.now(_dt.timezone.utc).isoformat(),
            ttl_s=REGISTRY_TTL_S,
            entries=[],
        )

    with (
        patch("verdict.discovery.refresh.load_probeable_routes", fake_load_probeable),
        patch("verdict.discovery.refresh.run_refresh", fake_run_refresh),
        patch("verdict.discovery.refresh.load_registry", return_value=None),
        patch("verdict.discovery.registry.save_registry"),
    ):
        run_refresh_action(free_only=True, force=True)

    capacity_classes = [cc for (_, cc, _) in captured_routes]
    assert capacity_classes == ["free"], (
        f"free_only=True must pass only 'free' routes; got {capacity_classes}"
    )
    assert all(cc == "free" for cc in capacity_classes), (
        "subscription route reached run_refresh despite free_only=True"
    )
