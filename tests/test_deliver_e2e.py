"""End-to-end tests for ``verdict deliver`` through the real CLI entry.

The run is an on-disk verified COMPLETE fixture; every git/``gh`` call goes through a
fake command runner installed in place of the default one. Nothing live runs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_deliver_service import BRANCH, INTEGRATION, PR_URL, FakeRunner, _make_run
from verdict import cli
from verdict.commands.parsers_deliver import EXIT_REFUSED
from verdict.orchestration import deliver_service
from verdict.orchestration.deliver_service import DELIVERY_FILE

ISSUE = "1269"


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / ".verdict"))


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch) -> FakeRunner:
    fake = FakeRunner()
    monkeypatch.setattr(deliver_service, "run_command", fake)
    return fake


def _deliver(monkeypatch: pytest.MonkeyPatch, run_dir: Path, *extra: str) -> None:
    argv = ["verdict", "deliver", str(run_dir), "--issue", ISSUE, "--issue-title", "Deliver it!"]
    monkeypatch.setattr(cli.sys, "argv", [*argv, *extra])
    cli.main()


def test_success_prints_draft_pr_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: FakeRunner,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir = _make_run(tmp_path)
    _deliver(monkeypatch, run_dir)

    assert capsys.readouterr().out.strip() == PR_URL
    assert ["git", "push", "origin", f"{INTEGRATION}:refs/heads/{BRANCH}"] in runner.calls
    creates = [c for c in runner.calls if c[:3] == ["gh", "pr", "create"]]
    assert len(creates) == 1
    assert "--draft" in creates[0]
    assert (run_dir / DELIVERY_FILE).exists()


def test_redelivery_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: FakeRunner,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir = _make_run(tmp_path)
    _deliver(monkeypatch, run_dir)
    assert capsys.readouterr().out.strip() == PR_URL
    calls_after_first = list(runner.calls)

    _deliver(monkeypatch, run_dir)

    assert capsys.readouterr().out.strip() == PR_URL
    assert runner.calls == calls_after_first
    assert len([c for c in runner.calls if c[:3] == ["gh", "pr", "create"]]) == 1
    assert len([c for c in runner.calls if c[:2] == ["git", "push"]]) == 1


def test_dry_run_issues_no_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: FakeRunner,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir = _make_run(tmp_path)
    _deliver(monkeypatch, run_dir, "--dry-run")

    out = capsys.readouterr().out
    assert "dry run" in out
    assert BRANCH in out
    assert PR_URL not in out
    assert runner.mutating == []
    assert runner.calls == []
    assert not (run_dir / DELIVERY_FILE).exists()


def test_dry_run_json_reports_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: FakeRunner,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir = _make_run(tmp_path)
    _deliver(monkeypatch, run_dir, "--dry-run", "--json")

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "dry_run"
    assert payload["branch"] == BRANCH
    assert payload["pr_url"] == ""
    assert runner.calls == []


def test_run_that_is_not_complete_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: FakeRunner,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir = _make_run(tmp_path, outcome="BLOCKED")
    with pytest.raises(SystemExit) as exc:
        _deliver(monkeypatch, run_dir)

    assert exc.value.code == EXIT_REFUSED
    captured = capsys.readouterr()
    assert "run_not_complete" in captured.err
    assert PR_URL not in captured.out
    assert runner.calls == []
    assert runner.mutating == []
    assert not (run_dir / DELIVERY_FILE).exists()
