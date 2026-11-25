"""Tests for context-lift blocked-exit behaviour (issue #1279).

A blocked bench result exits 2 so systemd records a non-success, and
_context_lift_timer_row reports a warning (not a failure) when the last
run was blocked rather than a crash.
"""

from __future__ import annotations

import argparse
import json
import subprocess
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

# systemd's InvocationID / $INVOCATION_ID: 32 lowercase hex characters, new for
# each runtime cycle of a unit (systemd.exec(5)).
_UNIT_ID = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
_OTHER_ID = "0f9e8d7c6b5a49382716a5b4c3d2e1f0"

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

        Requires ExecMainStatus=2 (intentional blocked exit), a receipt written by
        the same unit run (matching invocation ID) and an active timer.
        """
        from verdict.doctor_diagnostics import _context_lift_timer_row

        row = _context_lift_timer_row(
            timer_state=lambda u: {
                "state": "failed",
                "result": "exit-code",
                "exec_main_status": "2",
                "invocation_id": _UNIT_ID,
                "timer_active_state": "active",
            },
            bench_result=lambda: {
                "conclusion": "blocked",
                "block_reason": "no_qualified_identity",
                "invocation_id": _UNIT_ID,
            },
        )
        assert row["status"] == "warning"
        assert "blocked" in row["detail"]
        assert "no_qualified_identity" in row["detail"]

    def test_failed_blocked_no_bench_file_is_failed(self) -> None:
        """Service failed but no bench file (crash before write) -> failed.

        Every other warning prerequisite holds (exit 2, active timer, matching
        invocation), so the missing receipt is the only reason for ``failed``.
        """
        from verdict.doctor_diagnostics import _context_lift_timer_row

        # Control: the same unit state with a blocked receipt of this run is a warning.
        control = _context_lift_timer_row(
            timer_state=lambda u: _warning_ready_state(), bench_result=lambda: _id_receipt(_UNIT_ID)
        )
        assert control["status"] == "warning", control

        row = _context_lift_timer_row(
            timer_state=lambda u: _warning_ready_state(), bench_result=lambda: None
        )
        assert row["status"] == "failed", row

    def test_failed_non_blocked_conclusion_is_failed(self) -> None:
        """Service exited non-zero but bench shows non-blocked conclusion -> failed.

        The receipt is this run's own (matching invocation) and every other warning
        prerequisite holds, so the non-blocked conclusion is the only reason.
        """
        from verdict.doctor_diagnostics import _context_lift_timer_row

        control = _context_lift_timer_row(
            timer_state=lambda u: _warning_ready_state(), bench_result=lambda: _id_receipt(_UNIT_ID)
        )
        assert control["status"] == "warning", control

        row = _context_lift_timer_row(
            timer_state=lambda u: _warning_ready_state(),
            bench_result=lambda: _id_receipt(_UNIT_ID, conclusion="lift"),
        )
        assert row["status"] == "failed", row

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
                "invocation_id": _UNIT_ID,
                "timer_active_state": "active",
            },
            bench_result=lambda: {
                "conclusion": "blocked",
                "block_reason": "no_free_identity",
                "invocation_id": _UNIT_ID,
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
                "invocation_id": _UNIT_ID,
                "timer_active_state": "active",
            },
            bench_result=lambda: {
                "conclusion": "blocked",
                "block_reason": None,
                "invocation_id": _UNIT_ID,
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
                "invocation_id": _UNIT_ID,
                "timer_active_state": "active",
            },
            bench_result=lambda: {
                "conclusion": "blocked",
                "block_reason": "no_qualified_identity",
                "invocation_id": _UNIT_ID,
            },
        )
        assert row["status"] == "failed"


# ---------------------------------------------------------------------------
# The receipt must belong to the unit run that failed (systemd InvocationID)
# ---------------------------------------------------------------------------


def _fake_systemctl(
    *,
    exec_main_status: str = "2",
    invocation_id: str | None = _UNIT_ID,
    timer_active_state: str = "active",
    calls: list[list[str]] | None = None,
) -> Any:
    """Emulate ``systemctl --user show`` for the context-lift timer and service.

    A one-shot unit that ran and exited reports ``ActiveEnterTimestamp=n/a`` and
    keeps its ``InvocationID`` (checked on systemd 249).  ``invocation_id=None``
    omits the property, as systemd older than v232 does.
    """

    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        if calls is not None:
            calls.append(list(args))
        if args[-1].endswith(".timer"):
            props = {"LoadState": "loaded", "ActiveState": timer_active_state}
        else:
            props = {
                "LoadState": "loaded",
                "ActiveState": "failed",
                "Result": "exit-code",
                "ExecMainStatus": exec_main_status,
                "ActiveEnterTimestamp": "n/a",
            }
            if invocation_id:
                props["InvocationID"] = invocation_id
        return subprocess.CompletedProcess(
            args, 0, stdout="".join(f"{k}={v}\n" for k, v in props.items()), stderr=""
        )

    return run


def _id_receipt(invocation_id: Any, *, conclusion: str = "blocked") -> dict[str, Any]:
    """A bench receipt as written by a run, carrying that run's INVOCATION_ID (or None)."""
    return {
        "conclusion": conclusion,
        "block_reason": "no_qualified_identity",
        "invocation_id": invocation_id,
    }


def _warning_ready_state() -> dict[str, Any]:
    """A failed unit state for which every warning prerequisite holds.

    ExecMainStatus is 2, the unit has an InvocationID and the timer is active; only
    the receipt decides between ``warning`` and ``failed``.
    """
    return {
        "state": "failed",
        "result": "exit-code",
        "exec_main_status": "2",
        "invocation_id": _UNIT_ID,
        "timer_active_state": "active",
    }


def _row_via_systemctl(
    receipt: dict[str, Any] | None, monkeypatch: pytest.MonkeyPatch, **fake: Any
) -> dict[str, str]:
    from verdict import context_lift_service, metadata_service
    from verdict.doctor_diagnostics import _context_lift_timer_row

    monkeypatch.setattr(metadata_service, "_run_show", _fake_systemctl(**fake))
    return _context_lift_timer_row(
        timer_state=context_lift_service.timer_state, bench_result=lambda: receipt
    )


class TestReceiptRecordsInvocationId:
    def test_receipt_carries_the_systemd_invocation_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Under systemd the bench records $INVOCATION_ID so doctor can match the unit run."""
        monkeypatch.setenv("VERDICT_HOME", str(tmp_path))
        monkeypatch.setenv("INVOCATION_ID", _UNIT_ID)
        with (
            patch(
                "verdict.context_lift.run_context_lift", return_value={"receipt": _BLOCKED_RECEIPT}
            ),
            pytest.raises(SystemExit),
        ):
            _cmd_bench_context_lift(_make_args())
        data = json.loads((tmp_path / "context-lift" / "latest.json").read_text())
        assert data["invocation_id"] == _UNIT_ID

    def test_receipt_invocation_id_is_null_outside_systemd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A manual run has no INVOCATION_ID, so its receipt can never match a unit run."""
        monkeypatch.setenv("VERDICT_HOME", str(tmp_path))
        monkeypatch.delenv("INVOCATION_ID", raising=False)
        with (
            patch(
                "verdict.context_lift.run_context_lift", return_value={"receipt": _BLOCKED_RECEIPT}
            ),
            pytest.raises(SystemExit),
        ):
            _cmd_bench_context_lift(_make_args())
        data = json.loads((tmp_path / "context-lift" / "latest.json").read_text())
        assert data["invocation_id"] is None

    def test_dry_run_receipt_also_records_the_id_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dry run writes latest.json too; without INVOCATION_ID it must not look like a unit run."""
        monkeypatch.setenv("VERDICT_HOME", str(tmp_path))
        monkeypatch.delenv("INVOCATION_ID", raising=False)
        with patch(
            "verdict.context_lift.run_context_lift", return_value={"receipt": _BLOCKED_RECEIPT}
        ):
            _cmd_bench_context_lift(_make_args(dry_run=True))
        data = json.loads((tmp_path / "context-lift" / "latest.json").read_text())
        assert data["invocation_id"] is None


class TestInvocationOwnership:
    def test_show_requests_invocation_id(self) -> None:
        from verdict import metadata_service

        calls: list[list[str]] = []
        values = metadata_service._show("x.service", _fake_systemctl(calls=calls))
        props = next(a for a in calls[0] if "LoadState" in a)
        assert "InvocationID" in props
        assert values["InvocationID"] == _UNIT_ID

    def test_timer_state_exposes_invocation_id_and_the_timers_own_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from verdict import context_lift_service, metadata_service

        monkeypatch.setattr(metadata_service, "_run_show", _fake_systemctl())
        state = context_lift_service.timer_state()
        assert state["invocation_id"] == _UNIT_ID
        assert state["timer_active_state"] == "active"

    def test_blocked_run_by_this_invocation_is_warning_end_to_end(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """First ever run (ActiveEnterTimestamp is n/a), receipt written by this very run."""
        row = _row_via_systemctl(_id_receipt(_UNIT_ID), monkeypatch)
        assert row["status"] == "warning", row
        assert "no_qualified_identity" in row["detail"]

    def test_manual_blocked_run_after_a_usage_error_exit_is_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unit exits 2 from an argparse usage error and writes no receipt; the operator then
        runs the bench by hand (or with --dry-run), which overwrites latest.json with a blocked
        receipt that has no INVOCATION_ID.  The unit failure must not become a warning."""
        row = _row_via_systemctl(_id_receipt(None), monkeypatch)
        assert row["status"] == "failed", row

    def test_receipt_from_a_different_invocation_is_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        row = _row_via_systemctl(_id_receipt(_OTHER_ID), monkeypatch)
        assert row["status"] == "failed", row

    @pytest.mark.parametrize("receipt_id", [None, "", "   ", 123, ["x"], True])
    def test_missing_or_odd_receipt_id_never_matches(
        self, monkeypatch: pytest.MonkeyPatch, receipt_id: Any
    ) -> None:
        assert _row_via_systemctl(_id_receipt(receipt_id), monkeypatch)["status"] == "failed"

    def test_empty_ids_on_both_sides_never_match(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """systemd older than v232 has no InvocationID: '' must not equal ''."""
        row = _row_via_systemctl(_id_receipt(""), monkeypatch, invocation_id=None)
        assert row["status"] == "failed", row

    def test_unit_without_invocation_id_is_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        row = _row_via_systemctl(_id_receipt(_UNIT_ID), monkeypatch, invocation_id=None)
        assert row["status"] == "failed", row

    def test_id_comparison_ignores_case_and_whitespace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        row = _row_via_systemctl(_id_receipt(f"  {_UNIT_ID.upper()}\n"), monkeypatch)
        assert row["status"] == "warning", row

    def test_crash_with_matching_blocked_receipt_is_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even the right invocation is a failure unless it exited with the blocked code 2."""
        row = _row_via_systemctl(_id_receipt(_UNIT_ID), monkeypatch, exec_main_status="1")
        assert row["status"] == "failed", row

    @pytest.mark.parametrize("timer_state", ["inactive", "failed", "activating", "deactivating"])
    def test_stopped_or_failed_timer_is_failure_despite_a_current_blocked_receipt(
        self, monkeypatch: pytest.MonkeyPatch, timer_state: str
    ) -> None:
        """A blocked service failure is only a warning while the timer will still fire."""
        row = _row_via_systemctl(_id_receipt(_UNIT_ID), monkeypatch, timer_active_state=timer_state)
        assert row["status"] == "failed", row

    def test_active_timer_with_current_blocked_receipt_is_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        row = _row_via_systemctl(_id_receipt(_UNIT_ID), monkeypatch, timer_active_state="active")
        assert row["status"] == "warning", row

    def test_state_without_timer_active_state_is_failure(self) -> None:
        """A caller that does not say the timer is active gets the conservative answer."""
        from verdict.doctor_diagnostics import _context_lift_timer_row

        row = _context_lift_timer_row(
            timer_state=lambda u: {
                "state": "failed",
                "result": "exit-code",
                "exec_main_status": "2",
                "invocation_id": _UNIT_ID,
            },
            bench_result=lambda: _id_receipt(_UNIT_ID),
        )
        assert row["status"] == "failed", row
