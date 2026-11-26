"""``--push-timeout`` from the CLI to the ``git push`` call of ``verdict deliver``."""

from __future__ import annotations

import functools
import math
from pathlib import Path
from typing import Any

import pytest

from tests.test_deliver_cli import _delivered, _fake, _run
from tests.test_deliver_service import FakeRunner, _clock, _make_run, _request
from verdict.orchestration import deliver_service
from verdict.orchestration.deliver_publish import DEFAULT_PUSH_TIMEOUT_S, PublishResult, run_command
from verdict.orchestration.deliver_service import DELIVERY_FILE, DeliverStatus, deliver_run

PR_URL = "https://github.com/acme/repo/pull/77"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / ".verdict"))


class _Publisher:
    """Stands in for the publisher: records how it was called, never runs a command."""

    def __init__(self) -> None:
        self.kwargs: list[dict[str, Any]] = []

    def __call__(self, plan: Any, title: str, body: str, **kwargs: Any) -> PublishResult:
        self.kwargs.append(kwargs)
        return PublishResult(pr_url=PR_URL, branch=plan.head_branch, created=True)


# --- the parser and the request -----------------------------------------------------------


def test_default_push_timeout_is_thirty_minutes_and_reaches_the_request_in_seconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake(monkeypatch, _delivered())
    _run(monkeypatch, "deliver", str(tmp_path), "--issue", "9")
    assert calls[0].push_timeout_s == 1800.0
    assert calls[0].push_timeout_s == DEFAULT_PUSH_TIMEOUT_S


@pytest.mark.parametrize(("value", "seconds"), [("45", 2700.0), ("0.5", 30.0), ("1", 60.0)])
def test_push_timeout_minutes_reach_the_request_in_seconds(
    value: str, seconds: float, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake(monkeypatch, _delivered())
    _run(monkeypatch, "deliver", str(tmp_path), "--issue", "9", "--push-timeout", value)
    assert calls[0].push_timeout_s == seconds


@pytest.mark.parametrize(
    "value", ["nan", "inf", "-inf", "1e999", "1e308", "0", "0.0", "-1", "-0.5", "abc", ""]
)
def test_push_timeout_rejects_non_finite_zero_negative_and_garbage(
    value: str, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake(monkeypatch, _delivered())
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, "deliver", str(tmp_path), "--issue", "9", f"--push-timeout={value}")
    assert exc.value.code == 2
    assert calls == []
    # the option exists and rejected this value (not "unrecognized arguments")
    err = capsys.readouterr().err
    assert "argument --push-timeout:" in err
    assert "unrecognized" not in err


def test_help_describes_push_timeout_as_covering_the_pre_push_hook(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, "deliver", "--help")
    assert exc.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "--push-timeout MINUTES" in help_text
    assert "git push" in help_text and "pre-push hook" in help_text
    assert "default: 30" in help_text


# --- deliver_run: validation ----------------------------------------------------------------


@pytest.mark.parametrize("timeout", [math.nan, math.inf, -math.inf, 0.0, -5.0, None, "soon"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_unbounded_zero_or_malformed_push_timeout_is_refused_before_any_command(
    tmp_path: Path, timeout: Any, dry_run: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _make_run(tmp_path)
    runner = FakeRunner()
    publisher = _Publisher()
    monkeypatch.setattr(deliver_service, "_publish", publisher)
    outcome = deliver_run(
        _request(run_dir, push_timeout_s=timeout, dry_run=dry_run), runner=runner, clock=_clock
    )
    assert outcome.status is DeliverStatus.REFUSED
    assert outcome.reason == "invalid_request"
    assert "push timeout" in outcome.message
    assert runner.calls == []
    assert publisher.kwargs == []
    assert not (run_dir / DELIVERY_FILE).exists()


# --- deliver_run: what the publisher receives -----------------------------------------------


def test_without_an_injected_runner_the_publisher_gets_a_push_runner_with_the_requests_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _make_run(tmp_path)
    publisher = _Publisher()
    monkeypatch.setattr(deliver_service, "_publish", publisher)
    outcome = deliver_run(
        _request(run_dir, push_timeout_s=2700.0, run_ready_gate=False), clock=_clock
    )
    assert outcome.status is DeliverStatus.DELIVERED
    (kwargs,) = publisher.kwargs
    push_runner = kwargs["push_runner"]
    assert isinstance(push_runner, functools.partial)
    assert push_runner.func is run_command
    assert push_runner.args == ()
    assert push_runner.keywords == {"timeout": 2700.0}
    assert kwargs["runner"] is run_command


def test_the_default_request_pushes_with_the_default_push_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _make_run(tmp_path)
    publisher = _Publisher()
    monkeypatch.setattr(deliver_service, "_publish", publisher)
    deliver_run(_request(run_dir, run_ready_gate=False), clock=_clock)
    assert publisher.kwargs[0]["push_runner"].keywords == {"timeout": DEFAULT_PUSH_TIMEOUT_S}


def test_an_injected_runner_runs_the_push_and_the_publisher_gets_no_push_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _make_run(tmp_path)
    publisher = _Publisher()
    runner = FakeRunner()
    monkeypatch.setattr(deliver_service, "_publish", publisher)
    deliver_run(
        _request(run_dir, push_timeout_s=2700.0, run_ready_gate=False), runner=runner, clock=_clock
    )
    (kwargs,) = publisher.kwargs
    assert kwargs.get("push_runner") is None
    assert kwargs["runner"] is runner


# --- the whole path: argv to the publisher --------------------------------------------------


def test_cli_push_timeout_reaches_the_publishers_push_runner_in_seconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _make_run(tmp_path)
    publisher = _Publisher()
    monkeypatch.setattr(deliver_service, "_publish", publisher)
    _run(
        monkeypatch,
        "deliver",
        str(run_dir),
        "--issue",
        "1269",
        "--push-timeout",
        "45",
        "--no-ready",
    )
    (kwargs,) = publisher.kwargs
    assert kwargs["push_runner"].keywords == {"timeout": 2700.0}


def test_cli_push_timeout_applies_to_the_git_push_call_and_to_no_other_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _make_run(tmp_path)
    runner = FakeRunner()
    monkeypatch.setattr(deliver_service, "run_command", runner)
    _run(
        monkeypatch,
        "deliver",
        str(run_dir),
        "--issue",
        "1269",
        "--push-timeout",
        "45",
        "--no-ready",
    )
    pushes = [
        t for c, t in zip(runner.calls, runner.timeouts, strict=True) if c[:2] == ["git", "push"]
    ]
    others = [
        t for c, t in zip(runner.calls, runner.timeouts, strict=True) if c[:2] != ["git", "push"]
    ]
    assert pushes == [2700.0]
    assert others and all(t is None for t in others)
