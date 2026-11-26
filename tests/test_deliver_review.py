"""Tests for the production cross-family reviewer of the ready gate.

The review runner and the route source are fakes: no process, network, paid or
model call runs and no real home directory is touched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from verdict import cli
from verdict.orchestration import deliver_review, deliver_service
from verdict.orchestration.contracts import ReviewFinding, ReviewResult
from verdict.orchestration.deliver_review import make_cross_family_reviewer
from verdict.orchestration.deliver_service import DeliverOutcome, DeliverStatus
from verdict.orchestration.ready_gate import CrossFamilyReviewRequest, NoCrossFamilyReviewerError

HEAD = "a1b2c3d4e5f6a7b8c9d0a1b2c3d4e5f6a7b8c9d0"
PR = "https://github.com/acme/repo/pull/77"


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / ".verdict"))


class FakeSelector:
    """Stands in for the route source; nothing here is a live ladder."""


class FakeReviewRunner:
    def __init__(self, result: ReviewResult) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def review(self, **kwargs: Any) -> ReviewResult:
        self.calls.append(kwargs)
        return self.result


def _result(status: str = "PASS", route: str = "cx/gpt-5.5-low", **kw: Any) -> ReviewResult:
    return ReviewResult(status=status, reviewer="open-code-review", route_id=route, **kw)


def _reviewer(
    tmp_path: Path, result: ReviewResult
) -> tuple[Any, FakeReviewRunner, list[FakeSelector]]:
    runner = FakeReviewRunner(result)
    selectors: list[FakeSelector] = []

    def source() -> Any:
        selector = FakeSelector()
        selectors.append(selector)
        return selector

    def factory(selector: Any) -> Any:
        assert selector is selectors[-1]
        return runner

    reviewer = make_cross_family_reviewer(
        repo=tmp_path / "repo",
        out_dir=tmp_path / "out",
        selector_source=source,
        reviewer_factory=factory,
    )
    return reviewer, runner, selectors


def _request(*families: str) -> CrossFamilyReviewRequest:
    return CrossFamilyReviewRequest(HEAD, "main", PR, frozenset(families))


def test_cross_family_route_is_chosen_and_range_is_reviewed(tmp_path: Path) -> None:
    reviewer, runner, selectors = _reviewer(tmp_path, _result())
    result = reviewer(_request("claude"))
    assert result.passed
    assert len(selectors) == 1
    (call,) = runner.calls
    assert call["base_ref"] == "main"
    assert call["head_ref"] == HEAD
    assert call["repo"] == tmp_path / "repo"
    assert call["exclude_families"] == frozenset({"claude"})
    assert call["exclude_routes"] == frozenset()


def test_every_excluded_family_is_passed_to_the_review(tmp_path: Path) -> None:
    reviewer, runner, _ = _reviewer(tmp_path, _result(route="cx/gpt-5.5-low"))
    reviewer(_request("claude", "gemini"))
    assert runner.calls[0]["exclude_families"] == frozenset({"claude", "gemini"})


def test_reviewer_route_in_an_excluded_family_is_never_accepted(tmp_path: Path) -> None:
    reviewer, _, _ = _reviewer(tmp_path, _result(route="cc/claude-sonnet-5-5"))
    with pytest.raises(NoCrossFamilyReviewerError):
        reviewer(_request("claude"))


def test_no_admissible_route_raises(tmp_path: Path) -> None:
    result = _result(status="ERROR", route="", detail="no independent reviewer eligible")
    reviewer, _, _ = _reviewer(tmp_path, result)
    with pytest.raises(NoCrossFamilyReviewerError):
        reviewer(_request("claude"))


def test_no_recorded_implementer_family_raises_without_reviewing(tmp_path: Path) -> None:
    reviewer, runner, selectors = _reviewer(tmp_path, _result())
    with pytest.raises(NoCrossFamilyReviewerError):
        reviewer(_request())
    assert runner.calls == []
    assert selectors == []


@pytest.mark.parametrize(
    "detail", ["review skipped: no items reviewed", "review skipped: no items selected"]
)
def test_skipped_or_empty_review_is_an_error_never_a_pass(tmp_path: Path, detail: str) -> None:
    reviewer, _, _ = _reviewer(tmp_path, _result(status="ERROR", detail=detail))
    result = reviewer(_request("claude"))
    assert result.status == "ERROR"
    assert not result.passed
    assert detail in result.detail


def test_pass_without_a_reviewer_route_is_an_error(tmp_path: Path) -> None:
    reviewer, _, _ = _reviewer(tmp_path, _result(route=""))
    result = reviewer(_request("claude"))
    assert result.status == "ERROR"
    assert not result.passed


def test_pass_with_lost_findings_is_an_error(tmp_path: Path) -> None:
    reviewer, _, _ = _reviewer(tmp_path, _result(findings_lost=1))
    result = reviewer(_request("claude"))
    assert result.status == "ERROR"
    assert not result.passed


def test_pass_with_blocking_finding_is_an_error(tmp_path: Path) -> None:
    finding = ReviewFinding("high", "bug", "a.py", 1, "broken")
    reviewer, _, _ = _reviewer(tmp_path, _result(findings=(finding,)))
    assert not reviewer(_request("claude")).passed


def test_failed_review_is_returned_as_failed(tmp_path: Path) -> None:
    finding = ReviewFinding("critical", "bug", "a.py", 1, "broken")
    reviewer, _, _ = _reviewer(tmp_path, _result(status="FAIL", findings=(finding,)))
    result = reviewer(_request("claude"))
    assert result.status == "FAIL"
    assert not result.passed


def test_selector_is_built_lazily(tmp_path: Path) -> None:
    calls: list[int] = []

    def source() -> Any:
        calls.append(1)
        return FakeSelector()

    make_cross_family_reviewer(repo=tmp_path, out_dir=tmp_path / "out", selector_source=source)
    assert calls == []


def test_production_defaults_reuse_the_open_code_reviewer(tmp_path: Path) -> None:
    factory = deliver_review._open_code_reviewer_factory("http://gw.invalid:1/", tmp_path / "out")
    built = factory(FakeSelector())  # type: ignore[arg-type]
    assert isinstance(built, deliver_review.OpenCodeReviewer)


def test_deliver_command_passes_the_reviewer_to_deliver_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = object()
    built: list[dict[str, Any]] = []

    def fake_make(**kwargs: Any) -> Any:
        built.append(kwargs)
        return sentinel

    seen: list[dict[str, Any]] = []

    def fake_deliver(request: Any, **kwargs: Any) -> DeliverOutcome:
        seen.append(kwargs)
        return DeliverOutcome(status=DeliverStatus.DELIVERED, pr_url=PR)

    monkeypatch.setattr(deliver_review, "make_cross_family_reviewer", fake_make)
    monkeypatch.setattr(deliver_service, "deliver_run", fake_deliver)
    monkeypatch.setattr(cli.sys, "argv", ["verdict", "deliver", str(tmp_path), "--issue", "9"])
    cli.main()
    assert seen[0]["reviewer"] is sentinel
    assert len(built) == 1
    assert built[0]["out_dir"] == tmp_path / "deliver-review"
