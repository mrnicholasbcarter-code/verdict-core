"""Tests for context-lift blocked-exit behaviour (issue #1279).

A blocked bench result exits 2 so systemd records a non-success, and
_context_lift_timer_row reports a warning (not a failure) when the last
run was blocked rather than a crash.
"""

from __future__ import annotations

import argparse
import json
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
# The helper accepts ISO format (the systemd "day-of-week date time UTC" path
# falls through to fromisoformat on a T-separated string with no space).
_SERVICE_START_TS = "2026-01-01T12:00:00+00:00"  # ActiveEnterTimestamp
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
        finish_time to post-date the service's ActiveEnterTimestamp.
        """
        from verdict.doctor_diagnostics import _context_lift_timer_row

        row = _context_lift_timer_row(
            timer_state=lambda u: {
                "state": "failed",
                "result": "exit-code",
                "exec_main_status": "2",
                "active_enter_timestamp": _SERVICE_START_TS,
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
                "active_enter_timestamp": _SERVICE_START_TS,
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
                "active_enter_timestamp": _SERVICE_START_TS,
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
                "active_enter_timestamp": _SERVICE_START_TS,
            },
            bench_result=lambda: {
                "conclusion": "blocked",
                "block_reason": "no_qualified_identity",
                "finish_time": _RECEIPT_AFTER_TS,
            },
        )
        assert row["status"] == "failed"
