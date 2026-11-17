"""Tests for ``verdict pr-watch`` CLI command behavior."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class MockArgumentParser:
    """Mock parser that raises SystemExit on error like argparse.ArgumentParser does."""
    def __init__(self):
        self.errors: list[str] = []

    def error(self, message: str) -> None:
        self.errors.append(message)
        raise SystemExit(2)


def _run_pr_watch(argv: list[str], env: dict[str, str] | None = None, cwd: str | None = None) -> subprocess.CompletedProcess:
    """Run verdict pr-watch with the given arguments."""
    cmd = [sys.executable, "-m", "verdict", "pr-watch", *argv]
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, env=merged_env)


class TestPrWatchExitCodes:
    """Exit code behavior for various run_watcher outcomes."""

    def test_exit_0_when_ok_true(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """run_watcher returning ok=True exits 0."""
        from verdict.commands.dispatch import _cmd_pr_watch

        parser = MockArgumentParser()
        args = MagicMock()
        args.repo = ["owner/repo"]
        args.state_dir = str(tmp_path / "state")
        args.linear_team_keys = []
        args.json = False
        args.max_attempts = 1

        with patch("verdict.pr_watcher.run_watcher", return_value={"ok": True, "observations": [], "reruns": [], "notifications": [], "repo_errors": []}), \
             patch("verdict.pr_watcher_github.create_github_client"), \
             patch("verdict.pr_watcher_state.WatcherState"), \
             pytest.raises(SystemExit) as excinfo:
            _cmd_pr_watch(parser, args)
        assert excinfo.value.code == 0

    def test_exit_1_when_ok_false(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """run_watcher returning ok=False exits 1."""
        from verdict.commands.dispatch import _cmd_pr_watch

        parser = MockArgumentParser()
        args = MagicMock()
        args.repo = ["owner/repo"]
        args.state_dir = str(tmp_path / "state")
        args.linear_team_keys = []
        args.json = False
        args.max_attempts = 1

        with patch("verdict.pr_watcher.run_watcher", return_value={"ok": False, "error": "something failed", "repo_errors": [], "observations": [], "reruns": [], "notifications": []}), \
             patch("verdict.pr_watcher_github.create_github_client"), \
             patch("verdict.pr_watcher_state.WatcherState"), \
             pytest.raises(SystemExit) as excinfo:
            _cmd_pr_watch(parser, args)
        assert excinfo.value.code == 1

    def test_exit_1_when_repo_errors(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """run_watcher with repo_errors exits 1."""
        from verdict.commands.dispatch import _cmd_pr_watch

        parser = MockArgumentParser()
        args = MagicMock()
        args.repo = ["owner/repo"]
        args.state_dir = str(tmp_path / "state")
        args.linear_team_keys = []
        args.json = False
        args.max_attempts = 1

        with patch("verdict.pr_watcher.run_watcher", return_value={"ok": False, "repo_errors": [{"repo": "owner/repo", "error": "gh failed"}], "observations": [], "reruns": [], "notifications": []}), \
             patch("verdict.pr_watcher_github.create_github_client"), \
             patch("verdict.pr_watcher_state.WatcherState"), \
             pytest.raises(SystemExit) as excinfo:
            _cmd_pr_watch(parser, args)
        assert excinfo.value.code == 1

    def test_exit_1_on_client_refused(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """run_watcher returning client_refused type exits 1."""
        from verdict.commands.dispatch import _cmd_pr_watch

        parser = MockArgumentParser()
        args = MagicMock()
        args.repo = ["owner/repo"]
        args.state_dir = str(tmp_path / "state")
        args.linear_team_keys = []
        args.json = False
        args.max_attempts = 1

        with patch("verdict.pr_watcher.run_watcher", return_value={"ok": False, "type": "client_refused", "error": "client refused", "repo_errors": [], "observations": [], "reruns": [], "notifications": []}), \
             patch("verdict.pr_watcher_github.create_github_client"), \
             patch("verdict.pr_watcher_state.WatcherState"), \
             pytest.raises(SystemExit) as excinfo:
            _cmd_pr_watch(parser, args)
        assert excinfo.value.code == 1

    def test_exit_1_on_run_watcher_exception(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        """run_watcher raising an exception exits 1 with a one-line message, no traceback."""
        from verdict.commands.dispatch import _cmd_pr_watch

        parser = MockArgumentParser()
        args = MagicMock()
        args.repo = ["owner/repo"]
        args.state_dir = str(tmp_path / "state")
        args.linear_team_keys = []
        args.json = False
        args.max_attempts = 1

        with patch("verdict.pr_watcher.run_watcher", side_effect=RuntimeError("boom")), \
             patch("verdict.pr_watcher_github.create_github_client"), \
             patch("verdict.pr_watcher_state.WatcherState"), \
             pytest.raises(SystemExit) as excinfo:
            _cmd_pr_watch(parser, args)
        assert excinfo.value.code == 1
        stderr = capsys.readouterr().err
        assert "error: boom" in stderr
        assert "Traceback" not in stderr

    def test_exit_2_when_no_repos(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """No repositories (no --repo and no VERDICT_PR_WATCH_REPOS) exits 2."""
        from verdict.commands.dispatch import _cmd_pr_watch

        parser = MockArgumentParser()
        args = MagicMock()
        args.repo = []
        args.state_dir = str(tmp_path / "state")
        args.linear_team_keys = []
        args.json = False
        args.max_attempts = 1

        with patch("verdict.commands.dispatch._repos_from_env", return_value=[]), \
             pytest.raises(SystemExit) as excinfo:
            _cmd_pr_watch(parser, args)
        assert excinfo.value.code == 2

    def test_exit_2_when_not_in_git_and_no_state_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Outside a git repository without --state-dir exits 2."""
        from verdict.commands.dispatch import _cmd_pr_watch

        parser = MockArgumentParser()
        args = MagicMock()
        args.repo = ["owner/repo"]
        args.state_dir = None
        args.linear_team_keys = []
        args.json = False
        args.max_attempts = 1

        with patch("verdict.commands.dispatch._repos_from_env", return_value=["owner/repo"]), \
             patch("verdict.commands.dispatch._default_state_dir", return_value=None), \
             pytest.raises(SystemExit) as excinfo:
            _cmd_pr_watch(parser, args)
        assert excinfo.value.code == 2


class TestPrWatchDefaultStateDir:
    """Default state directory resolution."""

    def test_default_state_dir_uses_git_common_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Default state dir is git common dir / verdict-prime / pr-watcher."""
        # Create a temporary git repository
        git_dir = tmp_path / "myrepo"
        git_dir.mkdir()
        subprocess.run(["git", "init"], cwd=git_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=git_dir, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=git_dir, check=True)
        (git_dir / "README.md").write_text("# Test\n")
        subprocess.run(["git", "add", "README.md"], cwd=git_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=git_dir, check=True, capture_output=True)

        # Get the git common dir
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=git_dir, capture_output=True, text=True, check=True
        )
        git_common = result.stdout.strip()

        # Test the function
        from verdict.commands.parsers_pr_watcher import _default_state_dir

        monkeypatch.chdir(git_dir)
        state_dir = _default_state_dir()
        expected = os.path.join(git_common, "verdict-prime", "pr-watcher")
        assert state_dir == expected


class TestPrWatchEnvRepos:
    """VERDICT_PR_WATCH_REPOS environment variable handling."""

    def test_two_comma_separated_repos(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """VERDICT_PR_WATCH_REPOS with two comma-separated repos gives two repos."""
        from verdict.commands.parsers_pr_watcher import _repos_from_env

        monkeypatch.setenv("VERDICT_PR_WATCH_REPOS", "owner/repo1, owner/repo2")
        repos = _repos_from_env()
        assert repos == ["owner/repo1", "owner/repo2"]

    def test_env_repos_used_when_no_cli_repo(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """CLI uses env repos when --repo not provided."""
        from verdict.commands.dispatch import _cmd_pr_watch

        parser = MockArgumentParser()
        args = MagicMock()
        args.repo = []
        args.state_dir = str(tmp_path / "state")
        args.linear_team_keys = []
        args.json = False
        args.max_attempts = 1

        with patch("verdict.commands.dispatch._repos_from_env", return_value=["owner/repo1", "owner/repo2"]), \
             patch("verdict.commands.dispatch._default_state_dir", return_value=str(tmp_path / "state")), \
             patch("verdict.pr_watcher.run_watcher", return_value={"ok": True, "observations": [], "reruns": [], "notifications": [], "repo_errors": []}), \
             patch("verdict.pr_watcher_github.create_github_client"), \
             patch("verdict.pr_watcher_state.WatcherState"), \
             pytest.raises(SystemExit) as excinfo:
            _cmd_pr_watch(parser, args)
        assert excinfo.value.code == 0


class TestPrWatchOutput:
    """Output formatting."""

    def test_json_output(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        """--json prints machine-readable JSON."""
        from verdict.commands.dispatch import _cmd_pr_watch

        parser = MockArgumentParser()
        args = MagicMock()
        args.repo = ["owner/repo"]
        args.state_dir = str(tmp_path / "state")
        args.linear_team_keys = []
        args.json = True
        args.max_attempts = 1

        result_data = {"ok": True, "observations": [{"repo": "owner/repo", "pr": 1}], "reruns": [], "notifications": [], "repo_errors": []}
        with patch("verdict.pr_watcher.run_watcher", return_value=result_data), \
             patch("verdict.pr_watcher_github.create_github_client"), \
             patch("verdict.pr_watcher_state.WatcherState"), \
             pytest.raises(SystemExit) as excinfo:
            _cmd_pr_watch(parser, args)
        assert excinfo.value.code == 0
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert parsed["ok"] is True
        assert len(parsed["observations"]) == 1

    def test_summary_output_per_repo(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        """Non-JSON prints one-line summary per repo."""
        from verdict.commands.dispatch import _cmd_pr_watch

        parser = MockArgumentParser()
        args = MagicMock()
        args.repo = ["owner/repo1", "owner/repo2"]
        args.state_dir = str(tmp_path / "state")
        args.linear_team_keys = []
        args.json = False
        args.max_attempts = 1

        result_data = {
            "ok": True,
            "observations": [{"repo": "owner/repo1", "pr": 1}, {"repo": "owner/repo2", "pr": 2}],
            "reruns": [{"repo": "owner/repo1"}],
            "notifications": [{"repo": "owner/repo2"}],
            "repo_errors": []
        }
        with patch("verdict.pr_watcher.run_watcher", return_value=result_data), \
             patch("verdict.pr_watcher_github.create_github_client"), \
             patch("verdict.pr_watcher_state.WatcherState"), \
             pytest.raises(SystemExit) as excinfo:
            _cmd_pr_watch(parser, args)
        assert excinfo.value.code == 0
        out = capsys.readouterr().out
        lines = out.strip().split("\n")
        assert len(lines) == 2
        assert "owner/repo1" in lines[0]
        assert "PRs seen=2" in lines[0]  # Both observations counted
        assert "actions=2" in lines[0]   # 1 rerun + 1 notification (global counts)
        assert "owner/repo2" in lines[1]
        assert "actions=2" in lines[1]   # 1 rerun + 1 notification (global counts)

    def test_summary_output_shows_repo_errors(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        """Non-JSON prints ERROR for repos with errors."""
        from verdict.commands.dispatch import _cmd_pr_watch

        parser = MockArgumentParser()
        args = MagicMock()
        args.repo = ["owner/repo"]
        args.state_dir = str(tmp_path / "state")
        args.linear_team_keys = []
        args.json = False
        args.max_attempts = 1

        result_data = {
            "ok": False,
            "observations": [],
            "reruns": [],
            "notifications": [],
            "repo_errors": [{"repo": "owner/repo", "error": "gh api failed"}]
        }
        with patch("verdict.pr_watcher.run_watcher", return_value=result_data), \
             patch("verdict.pr_watcher_github.create_github_client"), \
             patch("verdict.pr_watcher_state.WatcherState"), \
             pytest.raises(SystemExit) as excinfo:
            _cmd_pr_watch(parser, args)
        assert excinfo.value.code == 1
        out = capsys.readouterr().out
        assert "owner/repo: ERROR gh api failed" in out