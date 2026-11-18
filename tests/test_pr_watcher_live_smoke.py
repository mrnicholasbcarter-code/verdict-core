"""Opt-in live smoke test for the PR watcher.

This test runs run_watcher with a temporary state directory against the
repository specified by VERDICT_PR_WATCH_SMOKE_REPO (default: mrnicholasbcarter-code/verdict-core).
It uses a real GitHub client (via gh CLI) but wraps rerun_check to fail, so no
actual reruns are performed.

Requirements:
- GH_TOKEN must be exported in the environment (conftest replaces HOME with a temp dir)
- VERDICT_LIVE_SMOKE=1 must be set to run (enforced by conftest)
- VERDICT_PR_WATCH_SMOKE_REPO can be set to override the default repository (comma-separated for multiple)

The test asserts that each observation has a classification.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from verdict.pr_watcher import run_watcher
from verdict.pr_watcher_github import create_github_client
from verdict.pr_watcher_state import WatcherState


@pytest.mark.skipif(
    os.environ.get("VERDICT_LIVE_SMOKE") != "1",
    reason="Live smoke test requires VERDICT_LIVE_SMOKE=1",
)
class TestPrWatcherLiveSmoke:
    """Live smoke test for the PR watcher (opt-in only)."""

    def test_live_smoke_classifies_open_prs(self, tmp_path: Path) -> None:
        """Run watcher against a live repo and verify observations have classifications."""
        repo_env = os.environ.get(
            "VERDICT_PR_WATCH_SMOKE_REPO", "mrnicholasbcarter-code/verdict-core"
        )
        repos = [r.strip() for r in repo_env.split(",") if r.strip()]

        # Create real GitHub client
        gh = create_github_client(linear_team_keys=None)

        # Wrap rerun_check to fail so no actual reruns happen
        original_rerun = gh.rerun_check

        def failing_rerun(job_id: int, repo: str) -> None:
            raise AssertionError("Live smoke test must not call rerun_check")

        gh.rerun_check = failing_rerun

        # Create watcher state in temp directory
        state_dir = tmp_path / "verdict-prime"
        state_dir.mkdir(parents=True)
        state = WatcherState(state_dir)

        # Run watcher
        result = run_watcher(gh, state, repos, linear=None, max_attempts=1)

        # Restore original rerun_check
        gh.rerun_check = original_rerun

        # Assert basic structure
        assert "observations" in result
        assert "repo_errors" in result
        assert "reruns" in result
        assert "records" in result

        # Each observation must have a classification
        for obs in result["observations"]:
            assert "classification" in obs, f"Observation missing classification: {obs}"
            assert obs["classification"] in {
                "GREEN",
                "PENDING",
                "INFRA_FAILURE",
                "CANCELLED",
                "CODE_FAILURE",
                "CONFLICT",
                "BEHIND",
                "EMPTY",
                "MISSING_REQUIRED",
                "BLOCKED",
                "STALLED",
                "HEAD_CHANGED",
                "RETRY_CI",
                "CORRECTIVE_IMPLEMENTATION",
                "REBASE_AND_REPROOF",
                "PROCEED_MERGE",
                "WAIT_CI",
            }, f"Unknown classification: {obs['classification']}"

        # If there are repo errors, they should have kind field
        for err in result["repo_errors"]:
            assert "kind" in err, f"Repo error missing kind: {err}"
            assert err["kind"] in {"github_error", "unsafe_state_key"}, (
                f"Unknown kind: {err['kind']}"
            )
