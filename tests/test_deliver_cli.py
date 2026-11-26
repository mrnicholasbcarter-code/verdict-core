"""CLI tests for ``verdict deliver``. ``deliver_run`` is faked: no git, no ``gh``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from verdict import cli
from verdict.commands.parsers_deliver import EXIT_FAILED, EXIT_REFUSED
from verdict.orchestration import deliver_service
from verdict.orchestration.deliver_service import DeliverOutcome, DeliverStatus

PR_URL = "https://github.com/example/repo/pull/7"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / ".verdict"))


def _run(monkeypatch: pytest.MonkeyPatch, *argv: str) -> None:
    monkeypatch.setattr(cli.sys, "argv", ["verdict", *argv])
    cli.main()


def _fake(monkeypatch: pytest.MonkeyPatch, outcome: DeliverOutcome) -> list[Any]:
    calls: list[Any] = []

    def fake(request: Any, **_kw: Any) -> DeliverOutcome:
        calls.append(request)
        return outcome

    monkeypatch.setattr(deliver_service, "deliver_run", fake)
    return calls


def _delivered() -> DeliverOutcome:
    return DeliverOutcome(
        status=DeliverStatus.DELIVERED,
        message="created draft pull request",
        run_id="run-1",
        pr_url=PR_URL,
        branch="verdict/issue-9-x",
        base_branch="main",
        created=True,
    )


def test_top_level_help_lists_deliver(capsys, monkeypatch):
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, "--help")
    assert exc.value.code == 0
    assert "deliver" in capsys.readouterr().out


def test_deliver_help_documents_arguments(capsys, monkeypatch):
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, "deliver", "--help")
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for token in ("run", "--base", "--dry-run", "--json", "--issue"):
        assert token in out


def test_success_prints_pr_url_and_passes_request(tmp_path, capsys, monkeypatch):
    calls = _fake(monkeypatch, _delivered())
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    _run(monkeypatch, "deliver", str(run_dir), "--issue", "9", "--base", "develop")
    assert capsys.readouterr().out.strip() == PR_URL
    request = calls[0]
    assert Path(request.run_dir) == run_dir
    assert request.issue_number == 9
    assert request.base_branch == "develop"
    assert request.dry_run is False


def test_json_prints_single_object_matching_to_dict(tmp_path, capsys, monkeypatch):
    outcome = _delivered()
    _fake(monkeypatch, outcome)
    _run(monkeypatch, "deliver", str(tmp_path), "--issue", "9", "--json")
    assert json.loads(capsys.readouterr().out) == outcome.to_dict()


def test_refusal_prints_reason_and_exits_with_refusal_code(tmp_path, capsys, monkeypatch):
    _fake(
        monkeypatch,
        DeliverOutcome(
            status=DeliverStatus.REFUSED, reason="run_not_complete", message="ended BLOCKED"
        ),
    )
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, "deliver", str(tmp_path), "--issue", "9")
    assert exc.value.code == EXIT_REFUSED
    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert "run_not_complete" in text
    assert "ended BLOCKED" in text


def test_publish_failure_exits_with_distinct_code(tmp_path, capsys, monkeypatch):
    _fake(
        monkeypatch,
        DeliverOutcome(
            status=DeliverStatus.FAILED, reason="vgate_rejected", message="gate said no"
        ),
    )
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, "deliver", str(tmp_path), "--issue", "9")
    assert exc.value.code == EXIT_FAILED
    assert EXIT_FAILED not in (0, EXIT_REFUSED)
    captured = capsys.readouterr()
    assert "vgate_rejected" in captured.out + captured.err


def test_json_refusal_still_exits_non_zero(tmp_path, capsys, monkeypatch):
    outcome = DeliverOutcome(status=DeliverStatus.REFUSED, reason="receipt_invalid", message="m")
    _fake(monkeypatch, outcome)
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, "deliver", str(tmp_path), "--issue", "9", "--json")
    assert exc.value.code == EXIT_REFUSED
    assert json.loads(capsys.readouterr().out)["reason"] == "receipt_invalid"


def test_dry_run_passes_through_and_prints_plan(tmp_path, capsys, monkeypatch):
    calls = _fake(
        monkeypatch,
        DeliverOutcome(
            status=DeliverStatus.DRY_RUN,
            branch="verdict/issue-9-x",
            base_branch="main",
            title="Planned title",
        ),
    )
    _run(monkeypatch, "deliver", str(tmp_path), "--issue", "9", "--dry-run")
    assert calls[0].dry_run is True
    out = capsys.readouterr().out
    assert "verdict/issue-9-x" in out
    assert "Planned title" in out


def test_already_delivered_exits_zero(tmp_path, capsys, monkeypatch):
    _fake(monkeypatch, DeliverOutcome(status=DeliverStatus.ALREADY_DELIVERED, pr_url=PR_URL))
    _run(monkeypatch, "deliver", str(tmp_path), "--issue", "9")
    assert capsys.readouterr().out.strip() == PR_URL


def test_run_id_resolves_under_runs_dir(tmp_path, monkeypatch):
    calls = _fake(monkeypatch, _delivered())
    (tmp_path / "runs" / "abc").mkdir(parents=True)
    _run(monkeypatch, "deliver", "abc", "--issue", "9", "--runs-dir", str(tmp_path / "runs"))
    assert Path(calls[0].run_dir) == tmp_path / "runs" / "abc"


@pytest.mark.parametrize(
    "value", ["nan", "inf", "-inf", "1e999", "0", "0.0", "-1", "-0.5", "abc", ""]
)
def test_ci_deadline_rejects_non_finite_zero_negative_and_garbage(
    value, tmp_path, capsys, monkeypatch
):
    """An unbounded or zero wait is refused at parse time; deliver_run is never reached."""
    calls = _fake(monkeypatch, _delivered())
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, "deliver", str(tmp_path), "--issue", "9", f"--ci-deadline={value}")
    assert exc.value.code == 2
    assert calls == []
    assert "--ci-deadline" in capsys.readouterr().err


@pytest.mark.parametrize(("value", "seconds"), [("0.5", 30.0), ("1", 60.0), ("90", 5400.0)])
def test_ci_deadline_accepts_finite_positive_minutes(value, seconds, tmp_path, monkeypatch):
    calls = _fake(monkeypatch, _delivered())
    _run(monkeypatch, "deliver", str(tmp_path), "--issue", "9", f"--ci-deadline={value}")
    assert calls[0].ci_deadline_s == seconds
