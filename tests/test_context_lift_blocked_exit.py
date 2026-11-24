"""Tests for context-lift blocked-exit behaviour (issue #1279).

A blocked bench result exits 2 so systemd records a non-success, and
_context_lift_timer_row reports a warning (not a failure) when the last
run was blocked rather than a crash.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from verdict.commands.dispatch import _cmd_bench_context_lift

_BLOCKED_RECEIPT: dict[str, Any] = {
    "schema_version": "1",
    "identity_id": None,
    "cost_class": None,
    "endpoint": None,
    "pack_digest": None,
    "unaided_passed": None,
    "packed_passed": None,
    "conclusion": "blocked",
    "block_reason": "no_qualified_identity",
    "omissions": None,
}

# Timestamps used in timer-row tests that require invocation binding.
# ExecMainStartTimestamp as printed by `systemctl show --timestamp=us+utc`
# (verified against systemd 249): explicit UTC, microseconds, any host TZ.
_SERVICE_START_TS = "Thu 2026-01-01 12:00:00.000000 UTC"  # ExecMainStartTimestamp
_RECEIPT_AFTER_TS = "2026-01-01T12:00:30+00:00"  # finish_time 30 s after start

_LIFT_RECEIPT: dict[str, Any] = {
    "schema_version": "1",
    "identity_id": "free/model",
    "cost_class": "free",
    "endpoint": "http://localhost:20128/v1",
    "pack_digest": "sha256:" + "a" * 64,
    "unaided_passed": False,
    "packed_passed": True,
    "conclusion": "lift",
    "block_reason": None,
    "omissions": None,
}


def _make_args(**overrides: Any) -> argparse.Namespace:
    defaults: dict[str, Any] = {
        "install_service": False,
        "uninstall": False,
        "base_url": None,
        "dry_run": False,
        "allow_priced": False,
        "json": True,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestBenchExitCode:
    def test_blocked_bench_exits_2(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """verdict bench context-lift --json exits 2 when blocked (not dry-run)."""
        monkeypatch.setenv("VERDICT_HOME", str(tmp_path))

        with (
            patch(
                "verdict.context_lift.run_context_lift", return_value={"receipt": _BLOCKED_RECEIPT}
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            _cmd_bench_context_lift(_make_args())
        assert exc_info.value.code == 2

    def test_lift_bench_exits_0(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A successful lift bench exits 0 (no SystemExit)."""
        monkeypatch.setenv("VERDICT_HOME", str(tmp_path))

        with patch(
            "verdict.context_lift.run_context_lift", return_value={"receipt": _LIFT_RECEIPT}
        ):
            # Must not raise SystemExit
            _cmd_bench_context_lift(_make_args())

    def test_blocked_dry_run_does_not_exit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--dry-run never raises SystemExit, even for a blocked result."""
        monkeypatch.setenv("VERDICT_HOME", str(tmp_path))

        with patch(
            "verdict.context_lift.run_context_lift", return_value={"receipt": _BLOCKED_RECEIPT}
        ):
            # Must not raise SystemExit for dry-run
            _cmd_bench_context_lift(_make_args(dry_run=True))

    def test_blocked_evidence_written_before_exit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Evidence is written to disk before SystemExit is raised."""
        monkeypatch.setenv("VERDICT_HOME", str(tmp_path))

        with (
            patch(
                "verdict.context_lift.run_context_lift", return_value={"receipt": _BLOCKED_RECEIPT}
            ),
            pytest.raises(SystemExit),
        ):
            _cmd_bench_context_lift(_make_args())

        latest = tmp_path / "context-lift" / "latest.json"
        assert latest.exists(), "latest.json must be written before exit"
        data = json.loads(latest.read_text())
        assert data["conclusion"] == "blocked"


class TestBenchResultReader:
    def test_reads_latest_json(self, tmp_path: Path) -> None:
        """_context_lift_last_bench_result reads latest.json from VERDICT_HOME."""
        from verdict.doctor_diagnostics import _context_lift_last_bench_result

        bench_dir = tmp_path / "context-lift"
        bench_dir.mkdir()
        (bench_dir / "latest.json").write_text(json.dumps(_BLOCKED_RECEIPT), encoding="utf-8")
        result = _context_lift_last_bench_result(home=tmp_path)
        assert result is not None
        assert result["conclusion"] == "blocked"

    def test_returns_none_when_missing(self, tmp_path: Path) -> None:
        """_context_lift_last_bench_result returns None when no file exists."""
        from verdict.doctor_diagnostics import _context_lift_last_bench_result

        result = _context_lift_last_bench_result(home=tmp_path)
        assert result is None

    def test_returns_none_on_corrupt_json(self, tmp_path: Path) -> None:
        """_context_lift_last_bench_result returns None on corrupt JSON."""
        from verdict.doctor_diagnostics import _context_lift_last_bench_result

        bench_dir = tmp_path / "context-lift"
        bench_dir.mkdir()
        (bench_dir / "latest.json").write_text("not json", encoding="utf-8")
        result = _context_lift_last_bench_result(home=tmp_path)
        assert result is None

    @pytest.mark.parametrize("content", ["[1, 2, 3]", '"blocked"', "42"])
    def test_returns_none_for_non_dict_json(self, tmp_path: Path, content: str) -> None:
        """_context_lift_last_bench_result returns None for valid JSON that is not an object.

        A list, string or number is never a valid receipt; .get() on the result
        would raise AttributeError and silently downgrade a crash to a warning.
        """
        from verdict.doctor_diagnostics import _context_lift_last_bench_result

        bench_dir = tmp_path / "context-lift"
        bench_dir.mkdir()
        (bench_dir / "latest.json").write_text(content, encoding="utf-8")
        result = _context_lift_last_bench_result(home=tmp_path)
        assert result is None


class TestDoctorTimerRow:
    def test_active_success_is_ok(self) -> None:
        """Active timer with success result is ok."""
        from verdict.doctor_diagnostics import _context_lift_timer_row

        row = _context_lift_timer_row(
            timer_state=lambda u: {"state": "active", "result": "success"}
        )
        assert row["status"] == "ok"

    def test_active_unknown_is_ok(self) -> None:
        """Active timer with unknown result is ok (timer not yet fired)."""
        from verdict.doctor_diagnostics import _context_lift_timer_row

        row = _context_lift_timer_row(
            timer_state=lambda u: {"state": "active", "result": "unknown"}
        )
        assert row["status"] == "ok"

    def test_failed_blocked_is_warning(self) -> None:
        """Service failed with exit-code and bench shows blocked -> warning, not failed.

        Requires ExecMainStatus=2 (intentional blocked exit) AND the receipt's
        finish_time to be no earlier than the service's ExecMainStartTimestamp.
        """
        from verdict.doctor_diagnostics import _context_lift_timer_row

        row = _context_lift_timer_row(
            timer_state=lambda u: {
                "state": "failed",
                "result": "exit-code",
                "exec_main_status": "2",
                "exec_main_start_timestamp": _SERVICE_START_TS,
            },
            bench_result=lambda: {
                "conclusion": "blocked",
                "block_reason": "no_qualified_identity",
                "finish_time": _RECEIPT_AFTER_TS,
            },
        )
        assert row["status"] == "warning"
        assert "blocked" in row["detail"]
        assert "no_qualified_identity" in row["detail"]

    def test_failed_blocked_no_bench_file_is_failed(self) -> None:
        """Service failed but no bench file (crash before write) -> failed."""
        from verdict.doctor_diagnostics import _context_lift_timer_row

        row = _context_lift_timer_row(
            timer_state=lambda u: {"state": "failed", "result": "exit-code"},
            bench_result=lambda: None,
        )
        assert row["status"] == "failed"

    def test_failed_non_blocked_conclusion_is_failed(self) -> None:
        """Service exited non-zero but bench shows non-blocked conclusion -> failed."""
        from verdict.doctor_diagnostics import _context_lift_timer_row

        row = _context_lift_timer_row(
            timer_state=lambda u: {"state": "failed", "result": "exit-code"},
            bench_result=lambda: {"conclusion": "lift"},
        )
        assert row["status"] == "failed"

    def test_missing_timer_is_failed(self) -> None:
        """Missing timer is failed."""
        from verdict.doctor_diagnostics import _context_lift_timer_row

        row = _context_lift_timer_row(
            timer_state=lambda u: {"state": "missing", "result": "unknown"}
        )
        assert row["status"] == "failed"

    def test_inactive_timer_is_failed(self) -> None:
        """Inactive timer is failed."""
        from verdict.doctor_diagnostics import _context_lift_timer_row

        row = _context_lift_timer_row(
            timer_state=lambda u: {"state": "inactive", "result": "unknown"}
        )
        assert row["status"] == "failed"

    def test_warning_includes_block_reason_in_detail(self) -> None:
        """Warning detail includes block_reason from the bench receipt."""
        from verdict.doctor_diagnostics import _context_lift_timer_row

        row = _context_lift_timer_row(
            timer_state=lambda u: {
                "state": "failed",
                "result": "exit-code",
                "exec_main_status": "2",
                "exec_main_start_timestamp": _SERVICE_START_TS,
            },
            bench_result=lambda: {
                "conclusion": "blocked",
                "block_reason": "no_free_identity",
                "finish_time": _RECEIPT_AFTER_TS,
            },
        )
        assert "no_free_identity" in row["detail"]

    def test_warning_detail_fallback_when_block_reason_none(self) -> None:
        """Warning detail uses generic message when block_reason is None."""
        from verdict.doctor_diagnostics import _context_lift_timer_row

        row = _context_lift_timer_row(
            timer_state=lambda u: {
                "state": "failed",
                "result": "exit-code",
                "exec_main_status": "2",
                "exec_main_start_timestamp": _SERVICE_START_TS,
            },
            bench_result=lambda: {
                "conclusion": "blocked",
                "block_reason": None,
                "finish_time": _RECEIPT_AFTER_TS,
            },
        )
        assert row["status"] == "warning"
        assert "blocked" in row["detail"]

    def test_blocked_then_crash_stays_failed(self) -> None:
        """A crash (exit != 2) must not be downgraded to warning by a stale blocked receipt.

        Regression test for the finding: a stale receipt with conclusion=blocked
        could previously mask a real crash.  The check now requires ExecMainStatus=2
        (the intentional blocked-exit code); anything else stays failed.
        """
        from verdict.doctor_diagnostics import _context_lift_timer_row

        # exec_main_status=1 simulates an unexpected crash (not the intentional exit 2).
        # The bench receipt still says blocked (a previous successful blocked run)
        # but that receipt should NOT downgrade this crash to a warning.
        row = _context_lift_timer_row(
            timer_state=lambda u: {
                "state": "failed",
                "result": "exit-code",
                "exec_main_status": "1",
                "exec_main_start_timestamp": _SERVICE_START_TS,
            },
            bench_result=lambda: {
                "conclusion": "blocked",
                "block_reason": "no_qualified_identity",
                "finish_time": _RECEIPT_AFTER_TS,
            },
        )
        assert row["status"] == "failed"


# ---------------------------------------------------------------------------
# Invocation binding: the receipt must belong to the unit invocation that failed
# ---------------------------------------------------------------------------

_START = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _fake_systemctl(
    *,
    start: datetime = _START,
    exec_main_status: str = "2",
    local_offset_hours: int = 0,
    local_name: str = "UTC",
    utc_flag_supported: bool = True,
    calls: list[list[str]] | None = None,
) -> Any:
    """Emulate ``systemctl --user show`` for the context-lift timer and service.

    Mirrors systemd 249: without ``--timestamp`` timestamps print in the host's
    local zone ("pretty"); ``--timestamp=us+utc`` prints explicit UTC with
    microseconds.  A one-shot unit that ran and exited reports
    ``ActiveEnterTimestamp=n/a`` but a populated ``ExecMainStartTimestamp``.
    """

    def render(dt: datetime, *, utc: bool) -> str:
        if utc:
            return dt.strftime("%a %Y-%m-%d %H:%M:%S.%f UTC")
        local = dt + timedelta(hours=local_offset_hours)
        return local.strftime("%a %Y-%m-%d %H:%M:%S ") + local_name

    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        if calls is not None:
            calls.append(list(args))
        utc = any(a == "--timestamp=us+utc" for a in args)
        if any(a.startswith("--timestamp=") for a in args) and not utc_flag_supported:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="Invalid value: us+utc.")
        if args[-1].endswith(".timer"):
            props = {"LoadState": "loaded", "ActiveState": "active"}
        else:
            props = {
                "LoadState": "loaded",
                "ActiveState": "failed",
                "Result": "exit-code",
                "ExecMainStatus": exec_main_status,
                "ActiveEnterTimestamp": "n/a",
                "ExecMainStartTimestamp": render(start, utc=utc),
            }
        return subprocess.CompletedProcess(
            args, 0, stdout="".join(f"{k}={v}\n" for k, v in props.items()), stderr=""
        )

    return run


def _blocked_receipt(finish: str | None) -> dict[str, Any]:
    return {"conclusion": "blocked", "block_reason": "no_qualified_identity", "finish_time": finish}


class TestInvocationBinding:
    def test_show_requests_exec_main_start_and_utc_timestamps(self) -> None:
        """The shared show helper can ask systemd for UTC and returns ExecMainStartTimestamp."""
        from verdict import metadata_service

        calls: list[list[str]] = []
        values = metadata_service._show(
            "verdict-context-lift.service", _fake_systemctl(calls=calls), timestamp="us+utc"
        )
        argv = calls[0]
        assert "--timestamp=us+utc" in argv
        props = next(a for a in argv if "LoadState" in a)
        assert "ExecMainStartTimestamp" in props
        assert values["ExecMainStartTimestamp"] == "Thu 2026-01-01 12:00:00.000000 UTC"

    def test_show_default_argv_is_unchanged(self) -> None:
        """Callers that do not ask for a timestamp format (metadata refresh) are unaffected."""
        from verdict import metadata_service

        calls: list[list[str]] = []
        metadata_service._show("x.service", _fake_systemctl(calls=calls))
        assert not any(a.startswith("--timestamp") for a in calls[0])

    def test_timer_state_exposes_exec_main_start_not_active_enter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A one-shot unit has ActiveEnterTimestamp=n/a; the invocation start is ExecMainStart."""
        from verdict import context_lift_service, metadata_service

        monkeypatch.setattr(metadata_service, "_run_show", _fake_systemctl())
        state = context_lift_service.timer_state()
        assert state["exec_main_start_timestamp"] == "Thu 2026-01-01 12:00:00.000000 UTC"
        assert "active_enter_timestamp" not in state

    def test_first_blocked_run_is_warning_end_to_end(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """First ever blocked run: ActiveEnterTimestamp is n/a, yet the row is a warning."""
        from verdict import context_lift_service, metadata_service
        from verdict.doctor_diagnostics import _context_lift_timer_row

        monkeypatch.setattr(metadata_service, "_run_show", _fake_systemctl())
        row = _context_lift_timer_row(
            timer_state=context_lift_service.timer_state,
            bench_result=lambda: _blocked_receipt((_START + timedelta(seconds=30)).isoformat()),
        )
        assert row["status"] == "warning", row

    def test_receipt_from_before_this_invocation_is_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A blocked receipt written before the failing invocation started is stale."""
        from verdict import context_lift_service, metadata_service
        from verdict.doctor_diagnostics import _context_lift_timer_row

        monkeypatch.setattr(metadata_service, "_run_show", _fake_systemctl())
        row = _context_lift_timer_row(
            timer_state=context_lift_service.timer_state,
            bench_result=lambda: _blocked_receipt((_START - timedelta(hours=24)).isoformat()),
        )
        assert row["status"] == "failed", row


class TestTimezoneIndependentStart:
    """Finding 2: the start time must not depend on the host's local timezone."""

    def test_non_utc_host_blocked_run_is_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """On a UTC-5 host systemd prints 'EST' unless asked for UTC; the row must still work."""
        from verdict import context_lift_service, metadata_service
        from verdict.doctor_diagnostics import _context_lift_timer_row

        calls: list[list[str]] = []
        monkeypatch.setattr(
            metadata_service,
            "_run_show",
            _fake_systemctl(local_offset_hours=-5, local_name="EST", calls=calls),
        )
        row = _context_lift_timer_row(
            timer_state=context_lift_service.timer_state,
            bench_result=lambda: _blocked_receipt((_START + timedelta(seconds=30)).isoformat()),
        )
        assert row["status"] == "warning", row
        assert any("--timestamp=us+utc" in c for c in calls)

    def test_microsecond_precision_orders_receipt_against_start(self) -> None:
        """A receipt 25 ms before the start is stale; 75 ms after is current."""
        from verdict.doctor_diagnostics import _context_lift_timer_row

        start_text = "Thu 2026-01-01 12:00:00.725894 UTC"

        def row_for(finish: str) -> str:
            return _context_lift_timer_row(
                timer_state=lambda u: {
                    "state": "failed",
                    "result": "exit-code",
                    "exec_main_status": "2",
                    "exec_main_start_timestamp": start_text,
                },
                bench_result=lambda: _blocked_receipt(finish),
            )["status"]

        assert row_for("2026-01-01T12:00:00.700000+00:00") == "failed"
        assert row_for("2026-01-01T12:00:00.800000+00:00") == "warning"

    def test_default_format_utc_string_without_fraction_is_accepted(self) -> None:
        """systemd's default format on a UTC host has no fraction; it is still explicit UTC."""
        from verdict.doctor_diagnostics import _context_lift_timer_row

        row = _context_lift_timer_row(
            timer_state=lambda u: {
                "state": "failed",
                "result": "exit-code",
                "exec_main_status": "2",
                "exec_main_start_timestamp": "Thu 2026-01-01 12:00:00 UTC",
            },
            bench_result=lambda: _blocked_receipt(_RECEIPT_AFTER_TS),
        )
        assert row["status"] == "warning"

    def test_flag_rejected_by_old_systemd_falls_back_and_still_works_on_utc_host(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """systemd older than v246 has no --timestamp; the unit must not read as 'missing'."""
        from verdict import context_lift_service, metadata_service
        from verdict.doctor_diagnostics import _context_lift_timer_row

        monkeypatch.setattr(
            metadata_service, "_run_show", _fake_systemctl(utc_flag_supported=False)
        )
        assert context_lift_service.timer_state()["state"] == "failed"
        row = _context_lift_timer_row(
            timer_state=context_lift_service.timer_state,
            bench_result=lambda: _blocked_receipt(_RECEIPT_AFTER_TS),
        )
        assert row["status"] == "warning", row

    def test_flag_rejected_on_non_utc_host_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without the UTC flag and with a local-zone string the row is a failure, not a crash."""
        from verdict import context_lift_service, metadata_service
        from verdict.doctor_diagnostics import _context_lift_timer_row

        monkeypatch.setattr(
            metadata_service,
            "_run_show",
            _fake_systemctl(local_offset_hours=-5, local_name="EST", utc_flag_supported=False),
        )
        row = _context_lift_timer_row(
            timer_state=context_lift_service.timer_state,
            bench_result=lambda: _blocked_receipt(_RECEIPT_AFTER_TS),
        )
        assert row["status"] == "failed", row

    def test_local_zone_string_is_never_read_as_utc(self) -> None:
        """'07:00 EST' is 12:00 UTC.  A receipt at 09:00 UTC is stale and must not pass."""
        from verdict.doctor_diagnostics import _context_lift_timer_row

        row = _context_lift_timer_row(
            timer_state=lambda u: {
                "state": "failed",
                "result": "exit-code",
                "exec_main_status": "2",
                "exec_main_start_timestamp": "Thu 2026-01-01 07:00:00 EST",
            },
            bench_result=lambda: _blocked_receipt("2026-01-01T09:00:00+00:00"),
        )
        assert row["status"] == "failed"


class TestReceiptTimestampAwareness:
    """Finding 3: a receipt time without a UTC offset is invalid, never compared."""

    @staticmethod
    def _row(finish: Any) -> dict[str, str]:
        from verdict.doctor_diagnostics import _context_lift_timer_row

        return _context_lift_timer_row(
            timer_state=lambda u: {
                "state": "failed",
                "result": "exit-code",
                "exec_main_status": "2",
                "exec_main_start_timestamp": _SERVICE_START_TS,
            },
            bench_result=lambda: _blocked_receipt(finish),
        )

    @pytest.mark.parametrize(
        "naive",
        ["2026-01-01T12:00:30", "2026-01-01 12:00:30", "2026-01-01T12:00:30.123456", "2026-01-01"],
    )
    def test_naive_receipt_time_is_failure_not_a_crash(self, naive: str) -> None:
        """An aware start vs a naive finish used to raise TypeError out of the doctor row."""
        row = self._row(naive)
        assert row["status"] == "failed", row

    @pytest.mark.parametrize(
        "junk", [1767268830, ["2026-01-01T12:00:30+00:00"], {"t": 1}, "", True]
    )
    def test_non_string_or_empty_receipt_time_is_failure(self, junk: Any) -> None:
        assert self._row(junk)["status"] == "failed"

    @pytest.mark.parametrize(
        "aware", ["2026-01-01T12:00:30+00:00", "2026-01-01T12:00:30Z", "2026-01-01T13:00:30+01:00"]
    )
    def test_receipt_time_with_offset_is_accepted(self, aware: str) -> None:
        """Any explicit offset is fine; 13:00:30+01:00 is 12:00:30 UTC."""
        assert self._row(aware)["status"] == "warning"

    def test_receipt_time_with_offset_before_start_is_failure(self) -> None:
        """11:59:30+00:00 is before the 12:00:00 UTC start."""
        assert self._row("2026-01-01T11:59:30+00:00")["status"] == "failed"
