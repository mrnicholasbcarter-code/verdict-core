"""First-run messages for ``verdict serve``, ``verdict doctor`` and ``verdict orchestrate``.

Each test names the first-run failure it catches. None of them loosens a rule:
serve still refuses to start, doctor still exits non-zero, admission is unchanged.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from verdict import cli
from verdict import doctor_diagnostics as dd
from verdict.orchestration.contracts import (
    CapacityClass,
    EligibilityStage,
    RouteVerdict,
    TaskRequirements,
)
from verdict.orchestration.explain import SCREEN_COMMAND, missing_proof_hint


@pytest.fixture(autouse=True)
def _fresh_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / ".verdict"))
    monkeypatch.setenv("VERDICT_SKIP_SETUP", "1")


# --- verdict serve ---------------------------------------------------------------


def _serve(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    import uvicorn

    runs: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: runs.append(kw))
    monkeypatch.setattr(sys, "argv", ["verdict", "serve", "--host", "127.0.0.1"])
    return runs


def test_serve_without_auth_token_exits_2_with_one_line_fix_not_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fresh install: no LLMGATE_AUTH_TOKEN used to raise a ValueError traceback."""
    runs = _serve(monkeypatch)
    monkeypatch.delenv("LLMGATE_AUTH_TOKEN", raising=False)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert runs == []  # still fails closed: the server never starts
    err = capsys.readouterr().err.strip()
    assert "\n" not in err and "Traceback" not in err
    assert "LLMGATE_AUTH_TOKEN" in err
    assert "verdict setup --apply --allow host.serve" in err
    assert "docs/CONFIGURATION.md#serve-authentication-and-receipts" in err


def test_serve_with_token_but_no_receipts_db_fails_before_uvicorn(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Token set, no VERDICT_RECEIPTS_DB: used to die in the uvicorn lifespan with a traceback.

    No default path is invented: ADR-017 says authenticated startup fails closed
    when the durable path is absent.
    """
    runs = _serve(monkeypatch)
    monkeypatch.setenv("LLMGATE_AUTH_TOKEN", "first-run-test-token")
    monkeypatch.delenv("VERDICT_RECEIPTS_DB", raising=False)
    monkeypatch.delenv("VERDICT_EVIDENCE_DB", raising=False)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert runs == []
    err = capsys.readouterr().err.strip()
    assert "\n" not in err and "VERDICT_RECEIPTS_DB must be configured" in err
    assert str(Path.home() / ".verdict" / "receipts.db") in err


def test_serve_with_both_settings_still_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The new pre-check must not block a correctly configured server."""
    runs = _serve(monkeypatch)
    monkeypatch.setenv("LLMGATE_AUTH_TOKEN", "first-run-test-token")
    monkeypatch.setenv("VERDICT_RECEIPTS_DB", ":memory:")
    cli.main()
    assert runs == [{"port": 8000, "host": "127.0.0.1"}]


# --- verdict doctor --------------------------------------------------------------


@pytest.mark.parametrize(
    "detail",
    [
        "verdict-identify.timer is missing; repair: verdict identify install-service",
        "verdict-serve.service is not installed; repair: verdict setup --apply --allow host.serve",
        "/x/health-cache.json does not exist; no screening or passive evidence yet",
        "not on PATH; verdict.ocr_release pins v1.12.9; repair: verdict setup --apply --allow host.ocr",
    ],
)
def test_absent_part_before_setup_is_not_configured_never_ok(detail: str) -> None:
    """Fresh install showed 12 FAILED rows; absent parts are NOT-CONFIGURED and still block."""
    row = {"label": "x", "status": "failed", "detail": detail}
    status = dd.first_run_status(row, configured=False)
    assert status == dd.NOT_CONFIGURED
    assert status in dd.BLOCKING_ROW_STATES and status != "ok"


@pytest.mark.parametrize(
    "row,configured",
    [
        # Present but broken: a real failure, even before setup.
        (
            {"label": "x", "status": "failed", "detail": "u.timer is failed; result exit-code"},
            False,
        ),
        ({"label": "Health cache", "status": "failed", "detail": "unreadable: bad json"}, False),
        # After setup a missing unit is a real failure again.
        ({"label": "x", "status": "failed", "detail": "u.timer is missing; repair: y"}, True),
        # Never upgrades a non-failed row.
        ({"label": "x", "status": "warning", "detail": "u.timer is missing"}, False),
    ],
)
def test_genuine_failures_stay_failed(row: dict[str, str], configured: bool) -> None:
    assert dd.first_run_status(row, configured=configured) == row["status"]


def test_unreadable_passive_report_stays_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only an absent identify report is "not set up"; a corrupt one is a failure."""
    import verdict.identify_service as identify_service

    report = tmp_path / "identify-latest.json"
    monkeypatch.setattr(identify_service, "latest_report_path", lambda: report)
    row = {
        "label": "Identify passive report",
        "status": "failed",
        "detail": "x missing or unreadable",
    }
    assert dd.first_run_status(row, configured=False) == dd.NOT_CONFIGURED
    report.write_text("{not json")
    assert dd.first_run_status(row, configured=False) == "failed"


def test_doctor_first_line_points_to_setup_and_exit_stays_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user who configured nothing sees where to start, rows with their next command, exit 1."""
    diag = dd.DoctorDiagnostics()
    diag.config_loaded = False
    diag.sections = [
        (
            "Identify timer",
            dd.NOT_CONFIGURED,
            "t is missing; repair: verdict identify install-service",
        )
    ]
    diag.issues = ["t is missing; repair: verdict identify install-service"]
    monkeypatch.setattr(dd, "_collect_doctor_diagnostics", lambda *a, **k: diag)
    out = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=out, width=120, force_terminal=False))
    with pytest.raises(SystemExit) as exc:
        cli.cmd_doctor()
    assert exc.value.code == 1
    text = out.getvalue()
    body = text[text.index("Doctor") :]
    first = next(
        line.strip() for line in body.splitlines()[1:] if line.strip() and "plan" not in line
    )
    assert first == dd.FIRST_RUN_LINE
    assert "NOT-CONFIGURED" in text and "FAILED" not in text
    assert "Repair: verdict identify install-service" in text


# --- verdict orchestrate (#1135) -------------------------------------------------


def _unproven(route: str) -> RouteVerdict:
    return RouteVerdict(
        route,
        route.split("/")[0],
        EligibilityStage.AVAILABLE,
        EligibilityStage.TASK_ELIGIBLE,
        "no_agentic_probe",
        CapacityClass.FREE,
    )


def test_missing_proof_hint_names_reason_and_bounded_command() -> None:
    hint = missing_proof_hint([_unproven("a/x"), _unproven("b/y")])
    assert "2 free route(s) have no recorded tool-use (agentic) check" in hint
    assert SCREEN_COMMAND in hint
    assert "--allow-live-probe" in SCREEN_COMMAND and "--max-routes 8" in SCREEN_COMMAND
    other = RouteVerdict("c/z", "c", None, EligibilityStage.AVAILABLE, "cooldown")
    assert missing_proof_hint([other]) == ""


def test_screen_command_parses_with_the_real_cli() -> None:
    """The printed command must be one the identify parser accepts."""
    import argparse
    import shlex

    from verdict.commands import parsers_identify

    parser = argparse.ArgumentParser()
    parsers_identify.register(parser.add_subparsers(dest="command"))
    args = parser.parse_args(shlex.split(SCREEN_COMMAND)[1:])
    assert args.screen and args.allow_live_probe
    assert (args.max_per_provider, args.max_routes, args.capacities) == (1, 8, ["free"])


@pytest.mark.asyncio
async def test_cold_state_planner_block_says_why_and_admits_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1135: cold state ended with 'none eligible' and no next step."""
    from datetime import datetime, timedelta, timezone

    from tests.test_planner_band import _record, _score
    from tests.test_planner_band import _snapshot as _pb_snapshot
    from verdict.orchestration.contracts import OrchestrationError
    from verdict.orchestration.run import plan_with_failover

    seen: list[TaskRequirements] = []

    class ColdSelector:
        def select(
            self, req: TaskRequirements, *, now: Any
        ) -> tuple[RouteVerdict | None, tuple[RouteVerdict, ...]]:
            seen.append(req)
            return None, (_unproven("free/a"),)

        def inventory_route_ids(self) -> list[str]:
            return ["free/a"]

        def set_planner_band(self, band: object) -> None:
            pass

    class Events:
        def emit(self, kind: str, **data: Any) -> None:
            pass

    _fetched_at = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(
        "verdict.metadata.store.load_store",
        lambda: _pb_snapshot(
            _record("free_a", ("free/a",), aa_intelligence=_score(90.0, _fetched_at))
        ),
    )

    with pytest.raises(OrchestrationError) as exc:
        await plan_with_failover(
            "g",
            repo=tmp_path,
            selector=ColdSelector(),  # type: ignore[arg-type]
            executor=object(),  # type: ignore[arg-type]
            classifier=object(),  # type: ignore[arg-type]
            events=Events(),
        )
    message = str(exc.value)
    assert "none eligible" in message and SCREEN_COMMAND in message
    assert len(seen) == 1  # no retry, no route admitted by the hint
