"""Tests for ``scripts/ci_wip_label.py`` (issue #1412).

The lint job lets a pull request with the ``wip`` label merge with open OpenSpec tasks. The run
on main after the merge is a ``push`` event with no pull request in it, so the label has to be
looked up. These tests pin that lookup: a push is wip exactly when the one merged pull request
that produced the pushed commit carried the label, and every failure to find out is "not wip".
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from scripts.ci_wip_label import PAGE_SIZE, fetch_pulls, is_wip, main

REPO = "owner/repo"
SHA = "a" * 40
BRANCH = "main"


def _pull(
    *,
    labels: list[str] | None = None,
    merged_at: str | None = "2026-11-28T01:19:12Z",
    merge_commit_sha: str = SHA,
    base: str = BRANCH,
    number: int = 7,
) -> dict[str, Any]:
    return {
        "number": number,
        "merged_at": merged_at,
        "merge_commit_sha": merge_commit_sha,
        "base": {"ref": base},
        "labels": [{"name": name} for name in (labels or [])],
    }


def _lookup(payload: object) -> Callable[[str, str], object]:
    def fetch(repo: str, sha: str) -> object:
        assert (repo, sha) == (REPO, SHA)
        return payload

    return fetch


def _failing(error: BaseException) -> Callable[[str, str], object]:
    def fetch(repo: str, sha: str) -> object:
        raise error

    return fetch


def _never(repo: str, sha: str) -> object:
    raise AssertionError("the lookup must not run")


def _push(fetch: Callable[[str, str], object], *, sha: str = SHA, repo: str = REPO) -> bool:
    return is_wip("push", event_wip="false", repo=repo, sha=sha, branch=BRANCH, fetch=fetch)


# ---------------------------------------------------------------------------
# pull_request events: the label comes from the event, nothing is looked up
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("event_wip", "expected"), [("true", True), ("false", False), ("", False)])
def test_a_pull_request_event_uses_its_own_label_and_never_looks_anything_up(
    event_wip: str, expected: bool
) -> None:
    got = is_wip(
        "pull_request", event_wip=event_wip, repo=REPO, sha=SHA, branch=BRANCH, fetch=_never
    )
    assert got is expected


@pytest.mark.parametrize("event", ["schedule", "workflow_dispatch", "", "pull_request_target"])
def test_an_event_that_is_neither_a_push_nor_a_pull_request_is_not_wip(event: str) -> None:
    got = is_wip(event, event_wip="true", repo=REPO, sha=SHA, branch=BRANCH, fetch=_never)
    assert got is False


# ---------------------------------------------------------------------------
# push events: the merged pull request that produced the commit decides
# ---------------------------------------------------------------------------


def test_a_push_from_a_wip_pull_request_is_wip() -> None:
    assert _push(_lookup([_pull(labels=["wip"])])) is True


def test_a_push_from_a_wip_pull_request_with_other_labels_is_wip() -> None:
    assert _push(_lookup([_pull(labels=["bug", "wip", "size:S"])])) is True


def test_the_label_is_matched_without_regard_to_case_like_the_workflow_expression() -> None:
    assert _push(_lookup([_pull(labels=["WIP"])])) is True


def test_a_push_from_an_unlabelled_pull_request_is_not_wip() -> None:
    assert _push(_lookup([_pull(labels=[])])) is False


def test_a_push_from_a_pull_request_with_only_other_labels_is_not_wip() -> None:
    assert _push(_lookup([_pull(labels=["bug", "wip-later", "size:S"])])) is False


@pytest.mark.parametrize(
    "error",
    [
        OSError("gh is not installed"),
        subprocess.CalledProcessError(1, ["gh", "api"]),
        subprocess.TimeoutExpired(["gh", "api"], 30),
        ValueError("not JSON"),
    ],
    ids=["no-gh", "api-error", "timeout", "bad-json"],
)
def test_a_failed_lookup_is_not_wip(error: BaseException) -> None:
    assert _push(_failing(error)) is False


def test_a_push_with_no_associated_pull_request_is_not_wip() -> None:
    assert _push(_lookup([])) is False


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {"message": "Not Found"},
        "wip",
        42,
        ["wip"],
        [None],
        [{"number": 1}],
        [{**_pull(), "labels": None}],
        [{**_pull(), "labels": "wip"}],
        [{**_pull(), "labels": ["wip"]}],
        [{**_pull(), "labels": [{"name": None}]}],
        [{**_pull(), "labels": [{"name": ["wip"]}]}],
        [{**_pull(labels=["wip"]), "base": None}],
        [{**_pull(labels=["wip"]), "base": {"ref": None}}],
    ],
    ids=lambda p: json.dumps(p)[:40],
)
def test_a_response_of_the_wrong_shape_is_not_wip(payload: object) -> None:
    assert _push(_lookup(payload)) is False


def test_a_pull_request_that_was_not_merged_into_the_pushed_branch_is_not_wip() -> None:
    # An open pull request that contains the commit is also listed by the endpoint.
    assert _push(_lookup([_pull(labels=["wip"], merged_at=None)])) is False


def test_a_pull_request_merged_as_another_commit_is_not_wip() -> None:
    assert _push(_lookup([_pull(labels=["wip"], merge_commit_sha="b" * 40)])) is False


def test_a_pull_request_merged_into_another_branch_is_not_wip() -> None:
    assert _push(_lookup([_pull(labels=["wip"], base="develop")])) is False


def test_only_the_pull_request_that_produced_the_commit_counts() -> None:
    other = _pull(labels=["wip"], merge_commit_sha="b" * 40, number=3)
    mine = _pull(labels=[], number=7)
    assert _push(_lookup([other, mine])) is False
    assert (
        _push(_lookup([_pull(labels=["wip"], number=7), _pull(merged_at=None, number=8)])) is True
    )


def test_two_pull_requests_for_one_commit_are_ambiguous_and_not_wip() -> None:
    two = [_pull(labels=["wip"], number=1), _pull(labels=["wip"], number=2)]
    assert _push(_lookup(two)) is False


def test_a_full_first_page_may_hide_a_second_match_and_is_not_wip() -> None:
    others = [_pull(merge_commit_sha="c" * 40, number=n) for n in range(100, 198)]
    mine = _pull(labels=["wip"], number=7)
    assert len(others) + 1 == PAGE_SIZE - 1
    assert _push(_lookup([mine, *others])) is True
    assert _push(_lookup([mine, *others, _pull(merge_commit_sha="d" * 40, number=300)])) is False


@pytest.mark.parametrize(("repo", "sha"), [("", SHA), (REPO, ""), ("", "")])
def test_a_push_without_a_repository_or_commit_is_not_wip_and_looks_nothing_up(
    repo: str, sha: str
) -> None:
    assert _push(_never, sha=sha, repo=repo) is False


# ---------------------------------------------------------------------------
# fetch_pulls: the one place that talks to GitHub, through gh
# ---------------------------------------------------------------------------


def _gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> Path:
    stubs = tmp_path / "bin"
    stubs.mkdir()
    log = tmp_path / "gh.log"
    gh = stubs / "gh"
    gh.write_text(f'#!/bin/sh\necho "$*" >> "{log}"\n{body}\n', encoding="utf-8")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stubs}:/usr/bin:/bin")
    return log


def test_fetch_pulls_asks_for_the_pull_requests_of_the_commit_and_parses_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = _gh(tmp_path, monkeypatch, f"cat <<'EOF'\n{json.dumps([_pull(labels=['wip'])])}\nEOF")
    got = fetch_pulls(REPO, SHA)
    assert got == [_pull(labels=["wip"])]
    args = log.read_text(encoding="utf-8").split()
    assert args[0] == "api"
    assert args[-1] == f"repos/{REPO}/commits/{SHA}/pulls?per_page={PAGE_SIZE}"


def test_fetch_pulls_raises_when_gh_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _gh(tmp_path, monkeypatch, "echo 'HTTP 403' >&2\nexit 1")
    with pytest.raises(subprocess.CalledProcessError):
        fetch_pulls(REPO, SHA)


def test_fetch_pulls_raises_when_the_answer_is_not_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _gh(tmp_path, monkeypatch, "echo '<html>'")
    with pytest.raises(ValueError):
        fetch_pulls(REPO, SHA)


def test_fetch_pulls_raises_when_gh_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    with pytest.raises(OSError):
        fetch_pulls(REPO, SHA)


# ---------------------------------------------------------------------------
# The command line: always exits 0 and prints exactly "true" or "false"
# ---------------------------------------------------------------------------


def _cli(*extra: str) -> list[str]:
    return ["--event", "push", "--repo", REPO, "--sha", SHA, "--branch", BRANCH, *extra]


def test_the_command_prints_true_for_a_wip_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _gh(tmp_path, monkeypatch, f"cat <<'EOF'\n{json.dumps([_pull(labels=['wip'])])}\nEOF")
    assert main(_cli()) == 0
    assert capsys.readouterr().out.strip() == "true"


def test_the_command_prints_false_for_an_unlabelled_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _gh(tmp_path, monkeypatch, f"cat <<'EOF'\n{json.dumps([_pull(labels=[])])}\nEOF")
    assert main(_cli()) == 0
    assert capsys.readouterr().out.strip() == "false"


def test_the_command_prints_false_and_says_why_when_the_lookup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _gh(tmp_path, monkeypatch, "exit 1")
    assert main(_cli()) == 0
    out = capsys.readouterr()
    assert out.out.strip() == "false"
    assert "not wip" in out.err


def test_the_command_uses_the_event_label_for_a_pull_request(
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv = ["--event", "pull_request", "--event-wip", "true", "--repo", REPO, "--sha", SHA]
    assert main([*argv, "--branch", BRANCH]) == 0
    assert capsys.readouterr().out.strip() == "true"
