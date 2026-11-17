"""Opt-in, read-only live PR watcher smoke coverage."""

from __future__ import annotations

import os

import pytest


@pytest.mark.skipif(os.environ.get("VERDICT_LIVE_SMOKE") != "1", reason="live smoke opt-in")
def test_live_pr_watcher_lists_and_classifies_without_rerun(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from verdict.pr_watcher_github import create_github_client

    repo = os.environ.get("VERDICT_PR_WATCH_SMOKE_REPO")
    if not repo:
        pytest.skip("VERDICT_PR_WATCH_SMOKE_REPO is required")
    client = create_github_client()
    monkeypatch.setattr(
        client, "rerun_check", lambda *_args, **_kwargs: pytest.fail("live smoke must not rerun")
    )
    for pull in client.list_open_prs(repo):
        client.get_pr(repo, int(pull["number"]))
