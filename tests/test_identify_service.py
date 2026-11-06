"""Identify maintenance uses read-only reporting and bounded daily qualification."""

import io
import json
import shlex
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from rich.console import Console

from verdict import cli, doctor_diagnostics, identify
from verdict.home_setup_render import _warm_cache
from verdict.terminal_ui import TerminalUI

NOW = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / ".verdict"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))


def test_units_install_idempotently_and_uninstall(tmp_path):
    from verdict.identify_service import (
        PASSIVE_INTERVAL_SECONDS,
        SCREEN_CALENDAR,
        SCREEN_MAX_PER_PROVIDER,
        SCREEN_MAX_ROUTES,
        SCREEN_SERVICE,
        SCREEN_TIMER,
        SERVICE,
        TIMER,
        manage_service,
    )

    commands = []
    directory = tmp_path / "units"
    dry = manage_service(unit_dir=directory, dry_run=True, run=commands.append)
    assert not directory.exists() and commands == []
    passive = dry["files"][SERVICE]
    screen = dry["files"][SCREEN_SERVICE]
    assert "-m verdict identify --passive --json --save-report --refresh-health-cache" in passive
    assert "UMask=0077" in passive
    argv = shlex.split(
        next(
            line.removeprefix("ExecStart=")
            for line in screen.splitlines()
            if line.startswith("ExecStart=")
        )
    )
    assert argv[argv.index("--capacity") + 1] == "free,subscription"
    assert "--screen" in argv and "--allow-live-probe" in argv
    assert int(argv[argv.index("--max-per-provider") + 1]) == SCREEN_MAX_PER_PROVIDER > 0
    assert int(argv[argv.index("--max-routes") + 1]) == SCREEN_MAX_ROUTES > 0
    assert f"OnUnitActiveSec={PASSIVE_INTERVAL_SECONDS}" in dry["files"][TIMER]
    assert f"OnCalendar={SCREEN_CALENDAR}" in dry["files"][SCREEN_TIMER]
    assert "prove-at-rest" not in str(dry) and "census" not in str(dry)
    first = manage_service(unit_dir=directory, run=commands.append)
    assert first["changed"] and len(list(directory.iterdir())) == 4
    assert ["systemctl", "--user", "enable", "--now", TIMER, SCREEN_TIMER] in commands
    commands.clear()
    assert not manage_service(unit_dir=directory, run=commands.append)["changed"]
    assert all("start" not in c and "try-restart" not in c for c in commands)
    manage_service(unit_dir=directory, uninstall=True, run=commands.append)
    assert list(directory.iterdir()) == []
    assert [
        "systemctl",
        "--user",
        "disable",
        "--now",
        TIMER,
        SCREEN_TIMER,
        SERVICE,
        SCREEN_SERVICE,
    ] in commands


def test_native_saved_passive_report_is_private_and_atomic(tmp_path, monkeypatch, capsys):
    from verdict.identify_service import latest_report_path

    report = {
        "mode": "passive",
        "fetched_at": NOW.isoformat(),
        "model_calls": 0,
        "source_errors": [],
    }
    monkeypatch.setattr(identify, "identify_report", lambda: report)
    path = latest_report_path()
    path.parent.mkdir(parents=True)
    path.write_text("old")
    path.chmod(0o644)
    replacements = []
    original = Path.replace

    def replace(self, target):
        assert Path(target).read_text() == "old"
        assert self.parent == Path(target).parent
        assert self.stat().st_mode & 0o777 == 0o600
        replacements.append(self)
        return original(self, target)

    monkeypatch.setattr(Path, "replace", replace)
    monkeypatch.setattr("sys.argv", ["verdict", "identify", "--passive", "--json", "--save-report"])
    cli.main()
    assert json.loads(path.read_text()) == json.loads(capsys.readouterr().out) == report
    assert replacements and path.stat().st_mode & 0o777 == 0o600
    assert list(path.parent.iterdir()) == [path]


CLEAN_REFRESH = {
    "refreshed_at": NOW.isoformat(),
    "recorded": 2,
    "newest_observed_at": (NOW - timedelta(seconds=90)).isoformat(),
    "source_errors": [],
    "model_calls": 0,
    "gateway_writes": 0,
}


@pytest.mark.parametrize(
    "age,errors,expected",
    [
        (0, [], "ok"),
        (21599, [], "ok"),
        (21600, [], "failed"),
        (-1, [], "failed"),
        (0, ["providers"], "failed"),
        (None, [], "failed"),
    ],
)
def test_doctor_identify_report_freshness(age, errors, expected):
    from verdict.identify_service import latest_report_path

    path = latest_report_path()
    if age is not None:
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "mode": "passive",
                    "model_calls": 0,
                    "fetched_at": (NOW - timedelta(seconds=age)).isoformat(),
                    "source_errors": errors,
                    "health_refresh": CLEAN_REFRESH,
                }
            )
        )
    rows = doctor_diagnostics.installed_service_rows(
        now=NOW, timer_state=lambda unit: {"state": "active", "installed": True}
    )
    row = next(r for r in rows if r["label"] == "Identify passive report")
    assert row["status"] == expected
    assert not any(r["label"] == "Prove-at-rest timer" for r in rows)


@pytest.mark.parametrize(
    "refresh,expected",
    [
        (CLEAN_REFRESH, "health refresh recorded 2, newest evidence age 90s"),
        (None, "does not refresh the health cache; repair: verdict identify install-service"),
        ({**CLEAN_REFRESH, "source_errors": ["health-matrix"]}, "source errors"),
        ({**CLEAN_REFRESH, "model_calls": 1}, "not zero-call and zero-write"),
        ({**CLEAN_REFRESH, "gateway_writes": 1}, "not zero-call and zero-write"),
    ],
)
def test_doctor_requires_a_clean_health_refresh(refresh, expected):
    from verdict.identify_service import latest_report_path, report_row

    report = {
        "mode": "passive",
        "model_calls": 0,
        "fetched_at": NOW.isoformat(),
        "source_errors": [],
    }
    if refresh is not None:
        report["health_refresh"] = refresh
    latest_report_path().parent.mkdir(parents=True)
    latest_report_path().write_text(json.dumps(report))
    row = report_row(now=NOW)
    assert row["status"] == ("ok" if refresh is CLEAN_REFRESH else "failed")
    assert expected in row["detail"]


def test_doctor_reports_missing_identify_timer():
    from verdict.identify_service import TIMER

    rows = doctor_diagnostics.installed_service_rows(
        now=NOW,
        timer_state=lambda unit: {
            "state": "missing" if unit == TIMER else "active",
            "installed": unit != TIMER,
        },
    )
    row = next(r for r in rows if r["label"] == "Identify timer")
    assert row["status"] == "failed" and "identify install-service" in row["detail"]


def test_setup_installs_identify_not_legacy(monkeypatch):
    from verdict import identify_service, prove_at_rest_service

    calls = []
    monkeypatch.setattr(
        identify_service,
        "manage_service",
        lambda **kwargs: calls.append(kwargs) or {"changed": True},
    )
    monkeypatch.setattr(
        prove_at_rest_service,
        "manage_service",
        lambda **kwargs: pytest.fail("legacy installer called"),
    )
    monkeypatch.setattr(identify_service, "timer_state", lambda unit: {"state": "active"})
    ui = TerminalUI(Console(file=io.StringIO()))
    answers = iter(["on", "y", "off", "y"])
    notes = []
    _warm_cache(ui, answers.__next__, "http://localhost:20128", notes)
    assert calls == [{"dry_run": False}]
    assert any("Identify" in note for note in notes)


def test_screen_capacity_comma_list_reaches_domain(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(
        identify, "identify_report", lambda **kwargs: calls.append(kwargs) or {"mode": "screen"}
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "verdict",
            "identify",
            "--screen",
            "--allow-live-probe",
            "--max-per-provider",
            "1",
            "--max-routes",
            "8",
            "--capacity",
            "free,subscription",
            "--json",
        ],
    )
    cli.main()
    assert calls[0]["capacities"] == ["free", "subscription"]
    assert json.loads(capsys.readouterr().out)["mode"] == "screen"


def test_service_cli_dry_run_does_not_fetch(monkeypatch, capsys):
    monkeypatch.setattr(
        identify,
        "identify_report",
        lambda **kwargs: pytest.fail("service install fetched evidence"),
    )
    monkeypatch.setattr("sys.argv", ["verdict", "identify", "install-service", "--dry-run"])
    cli.main()
    assert json.loads(capsys.readouterr().out)["dry_run"]


def test_screen_unit_command_parses_with_the_approved_bounds(monkeypatch, capsys):
    from verdict.identify_service import SCREEN_SERVICE, manage_service

    unit = manage_service(dry_run=True)["files"][SCREEN_SERVICE]
    argv = shlex.split(
        next(line.split("=", 1)[1] for line in unit.splitlines() if line.startswith("ExecStart="))
    )
    calls = []
    monkeypatch.setattr(
        identify, "identify_report", lambda **kwargs: calls.append(kwargs) or {"mode": "screen"}
    )
    monkeypatch.setattr("sys.argv", ["verdict", *argv[3:]])
    cli.main()
    assert calls[0]["max_per_provider"] == 1
    assert calls[0]["max_routes"] == 8
    assert calls[0]["capacities"] == ["free", "subscription"]
    assert calls[0]["allow_live_probe"]


def test_doctor_never_recommends_retired_installers():
    section = doctor_diagnostics.census_runtime_section()
    assert "census --install" not in str(section)
    assert "prove-at-rest install-service" not in str(section)
    assert "retired" in str(section)


@pytest.mark.parametrize("unit", ["verdict-identify.timer", "verdict-identify-screen.timer"])
@pytest.mark.parametrize("state", ["inactive", "failed", "missing"])
def test_doctor_requires_both_identify_timers_active(unit, state):
    rows = doctor_diagnostics.installed_service_rows(
        now=NOW, timer_state=lambda name: {"state": state if name == unit else "active"}
    )
    row = next(row for row in rows if row["detail"].startswith(unit + " is"))
    assert row["status"] == "failed"
    assert "identify install-service" in row["detail"]


def test_repeated_and_comma_capacity_filters_both_work(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(
        identify, "identify_report", lambda **kwargs: calls.append(kwargs) or {"mode": "screen"}
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "verdict",
            "identify",
            "--screen",
            "--allow-live-probe",
            "--max-per-provider",
            "1",
            "--max-routes",
            "8",
            "--capacity",
            "free",
            "--capacity",
            "subscription",
            "--json",
        ],
    )
    cli.main()
    assert calls[0]["capacities"] == ["free", "subscription"]


@pytest.mark.parametrize(
    "realtime,expected",
    [
        ("", "unknown"),
        ("n/a", "unknown"),
        ("Wed 2026-10-21 15:00:00 UTC", "Wed 2026-10-21 15:00:00 UTC"),
    ],
)
def test_timer_next_run_never_treats_monotonic_duration_as_calendar(
    monkeypatch, realtime, expected
):
    from verdict import identify_service

    monkeypatch.setattr(
        identify_service,
        "_show",
        lambda *_: {
            "LoadState": "loaded",
            "ActiveState": "active",
            "NextElapseUSecRealtime": realtime,
            "NextElapseUSecMonotonic": "3w 1d 16h 20min 31.9s",
        },
    )
    assert identify_service.timer_state()["next_run"] == expected


def _write_ok_report(**extra):
    from verdict.identify_service import latest_report_path

    path = latest_report_path()
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "mode": "passive",
                "model_calls": 0,
                "fetched_at": NOW.isoformat(),
                "source_errors": [],
                **extra,
            }
        )
    )
    return path


def _pools(eligible_free=2, eligible_total=5, fallback=7):
    by_capacity = {"free": eligible_free, "subscription": 0, "metered": 0, "unknown": 0}
    by_capacity["subscription"] = eligible_total - eligible_free
    return {
        "implementation": {
            "task_family": "code_edit",
            "eligible_count": eligible_total,
            "fallback_count": fallback,
            "eligible_by_capacity": by_capacity,
        },
        "dispatch_authorized": False,
    }


def test_doctor_report_row_summarizes_implementation_pool_and_supply():
    from verdict.identify_service import report_row

    path = _write_ok_report(
        task_pools=_pools(), supply={"complete": False, "unknowns": ["a", "b", "c"]}
    )
    row = report_row(now=NOW)
    assert row["status"] == "ok"
    assert row["detail"] == (
        f"{path} age 0s; source errors none; model calls 0"
        "; implementation pool eligible 5 (free 2), fallback 7"
        "; supply complete false, 3 unknowns"
    )


def test_doctor_report_row_keeps_old_text_without_pool_keys():
    from verdict.identify_service import report_row

    path = _write_ok_report(supply={"complete": False, "unknowns": []})
    row = report_row(now=NOW)
    assert row == {
        "label": "Identify passive report",
        "status": "ok",
        "detail": f"{path} age 0s; source errors none; model calls 0",
    }


def test_doctor_report_row_pool_summary_never_changes_status():
    from verdict.identify_service import report_row

    _write_ok_report(task_pools={"implementation": None}, supply={"complete": "yes"})
    row = report_row(now=NOW)
    assert row["status"] == "ok"
    assert row["detail"].endswith("; task pools unreadable")


def test_doctor_report_row_with_source_errors_stays_failed_with_pools():
    from verdict.identify_service import latest_report_path, report_row

    path = latest_report_path()
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "mode": "passive",
                "model_calls": 0,
                "fetched_at": NOW.isoformat(),
                "source_errors": ["candidates"],
                "task_pools": _pools(),
                "supply": {"complete": False, "unknowns": []},
            }
        )
    )
    row = report_row(now=NOW)
    assert row["status"] == "failed"
    assert "pool" not in row["detail"]
