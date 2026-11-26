"""Branch slug, branch name and push of the integration SHA, against a real local bare remote."""

from __future__ import annotations

import ast
import re
import subprocess
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tests.test_deliver_publish import _plan
from verdict.orchestration import deliver_publish
from verdict.orchestration.deliver import head_branch_name, slugify_title
from verdict.orchestration.deliver_publish import (
    CommandResult,
    PublishError,
    PublishReason,
    push_branch,
    run_command,
)

BRANCH = "verdict/issue-7-add-thing"
CREATE_ONLY_LEASE = f"--force-with-lease=refs/heads/{BRANCH}:"


@pytest.fixture(autouse=True)
def isolated_git_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    monkeypatch.delenv("VERDICT_PREPUSH_SKIP", raising=False)


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True, timeout=60
    )
    return proc.stdout.strip()


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    work.mkdir()
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.name", "Test")
    _git(work, "config", "user.email", "test@example.invalid")
    _git(work, "config", "commit.gpgsign", "false")
    _git(work, "remote", "add", "origin", str(remote))
    return work


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    return tmp_path / "remote.git"


def _remote_sha(remote: Path, branch: str = BRANCH) -> str | None:
    out = _git(remote, "for-each-ref", "--format=%(objectname)", f"refs/heads/{branch}")
    return out or None


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str], cwd: Path | None) -> CommandResult:
        self.calls.append(list(argv))
        return run_command(argv, cwd)


def _install_hook(repo: Path, body: str) -> None:
    hook = repo / ".git" / "hooks" / "pre-push"
    hook.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    hook.chmod(0o755)


# --- slug and branch name -------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Fix: the `rm -rf /` bug; $(whoami)!", "fix-the-rm-rf-bug-whoami"),
        ("Add   the\tthing\n now", "add-the-thing-now"),
        ("  --Leading and trailing--  ", "leading-and-trailing"),
        ("Hello, World!!!", "hello-world"),
        ("a/b\\c..d", "a-b-c-d"),
        ("UPPER lower 123", "upper-lower-123"),
    ],
)
def test_slug_drops_unsafe_characters(title: str, expected: str) -> None:
    assert slugify_title(title) == expected


def test_slug_long_title_is_capped_without_trailing_hyphen() -> None:
    slug = slugify_title("word " * 30)
    assert len(slug) <= 40
    assert not slug.endswith("-") and not slug.startswith("-")
    # cut lands exactly on a separator: 39 letters, a hyphen, then more letters
    cut = slugify_title("a" * 39 + "-bbb")
    assert cut == "a" * 39
    assert len(slugify_title("x" * 100)) == 40


@pytest.mark.parametrize("title", ["", "   ", "!!!???", "日本語", "---", "\n\t"])
def test_slug_is_empty_for_all_symbol_title(title: str) -> None:
    assert slugify_title(title) == ""


def test_branch_name_is_exact() -> None:
    assert head_branch_name(1269, "Deliver it!") == "verdict/issue-1269-deliver-it"
    assert head_branch_name(5, "") == "verdict/issue-5"
    assert head_branch_name(7, "Add-thing") == "verdict/issue-7-add-thing"
    assert re.fullmatch(r"verdict/issue-7-[a-z0-9-]{1,40}", head_branch_name(7, "Z" * 100))
    assert (
        head_branch_name(1368, "Salvage drops an attempt's edit")
        == "verdict/issue-1368-salvage-drops-an-attempt-s-edit"
    )


def test_punctuation_only_title_gives_bare_branch() -> None:
    assert head_branch_name(9, "!!! ??? ---") == "verdict/issue-9"


@pytest.mark.parametrize(
    "title",
    [
        "--- a ---",
        "a   b\t\tc",
        "word " * 30,
        "a" * 39 + " bbb",
        "x" * 38 + "!!! yy",
        "!lead ... trail!",
        "a--b__c  d",
    ],
)
def test_slug_never_has_edge_or_doubled_hyphens(title: str) -> None:
    slug = slugify_title(title)
    assert slug
    assert len(slug) <= 40
    assert not slug.startswith("-") and not slug.endswith("-")
    assert "--" not in slug


# --- push -----------------------------------------------------------------------------


def test_push_puts_exact_sha_on_remote_branch(repo: Path, remote: Path) -> None:
    _commit(repo, "one")
    sha = _commit(repo, "two")
    _commit(repo, "three")  # HEAD moves on; only the plan's SHA must be pushed
    runner = RecordingRunner()
    result = push_branch(replace(_plan(head=BRANCH), integration_commit=sha), runner, repo)
    assert result is None
    assert _remote_sha(remote) == sha
    assert [
        "git",
        "push",
        CREATE_ONLY_LEASE,
        "origin",
        f"{sha}:refs/heads/{BRANCH}",
    ] in runner.calls


def test_push_runs_pre_push_hook_and_hook_pass_allows_push(repo: Path, remote: Path) -> None:
    sha = _commit(repo, "one")
    marker = repo / "hook-ran"
    _install_hook(repo, f"echo ran > {marker}\nexit 0")
    assert (
        push_branch(replace(_plan(head=BRANCH), integration_commit=sha), run_command, repo) is None
    )
    assert marker.exists()
    assert _remote_sha(remote) == sha


def test_failing_pre_push_hook_is_vgate_rejected_with_tail(repo: Path, remote: Path) -> None:
    sha = _commit(repo, "one")
    _install_hook(
        repo,
        'i=0\nwhile [ $i -lt 400 ]; do echo "gate noise $i"; i=$((i+1)); done\n'
        'echo "ruff: 3 errors" >&2\necho "GATE-TAIL-MARKER"\nexit 1',
    )
    result = push_branch(replace(_plan(head=BRANCH), integration_commit=sha), run_command, repo)
    assert isinstance(result, PublishError)
    assert result.reason is PublishReason.VGATE_REJECTED
    assert "GATE-TAIL-MARKER" in result.message or "ruff: 3 errors" in result.message
    assert "gate noise 0\n" not in result.message  # tail only, not the whole output
    assert _remote_sha(remote) is None
    assert _git(remote, "for-each-ref", "refs/heads") == ""


def test_existing_branch_at_other_commit_is_branch_exists_and_unchanged(
    repo: Path, remote: Path
) -> None:
    first = _commit(repo, "one")
    _git(repo, "push", "-q", "origin", f"{first}:refs/heads/{BRANCH}")
    second = _commit(repo, "two")
    runner = RecordingRunner()
    result = push_branch(replace(_plan(head=BRANCH), integration_commit=second), runner, repo)
    assert isinstance(result, PublishError)
    assert result.reason is PublishReason.BRANCH_EXISTS
    assert _remote_sha(remote) == first
    assert not any(c[:2] == ["git", "push"] for c in runner.calls)


def test_existing_branch_at_same_commit_is_reused(repo: Path, remote: Path) -> None:
    sha = _commit(repo, "one")
    _git(repo, "push", "-q", "origin", f"{sha}:refs/heads/{BRANCH}")
    assert (
        push_branch(replace(_plan(head=BRANCH), integration_commit=sha), run_command, repo) is None
    )
    assert _remote_sha(remote) == sha


def test_push_conflict_after_lookup_is_branch_exists(repo: Path, remote: Path) -> None:
    """A writer that lands between ls-remote and push is still branch_exists, never forced."""
    first = _commit(repo, "one")
    _git(repo, "push", "-q", "origin", f"{first}:refs/heads/{BRANCH}")
    _git(repo, "checkout", "-q", "--orphan", "diverged")
    second = _commit(repo, "two")  # unrelated history: not a fast-forward of the remote branch

    def blind_lookup(argv: Sequence[str], cwd: Path | None) -> CommandResult:
        if list(argv[:2]) == ["git", "ls-remote"]:
            return CommandResult(0, "")
        return run_command(argv, cwd)

    result = push_branch(replace(_plan(head=BRANCH), integration_commit=second), blind_lookup, repo)
    assert isinstance(result, PublishError)
    assert result.reason is PublishReason.BRANCH_EXISTS
    assert _remote_sha(remote) == first


def test_ancestor_race_after_lookup_is_branch_exists_and_the_other_branch_is_untouched(
    repo: Path, remote: Path
) -> None:
    """Another writer creates the branch at an ancestor of our commit after ls-remote.

    A plain ``git push`` would fast-forward their branch to our commit. The create-only
    push must be rejected instead, and their branch must stay where they put it.
    """
    first = _commit(repo, "one")
    second = _commit(repo, "two")  # a descendant of ``first``: a plain push fast-forwards
    runner = RecordingRunner()

    def racing_lookup(argv: Sequence[str], cwd: Path | None) -> CommandResult:
        result = runner(argv, cwd)
        if list(argv[:2]) == ["git", "ls-remote"]:
            assert result.stdout == ""  # the branch is absent when we look
            _git(repo, "push", "-q", "origin", f"{first}:refs/heads/{BRANCH}")  # they land now
        return result

    result = push_branch(
        replace(_plan(head=BRANCH), integration_commit=second), racing_lookup, repo
    )
    assert isinstance(result, PublishError)
    assert result.reason is PublishReason.BRANCH_EXISTS
    assert _remote_sha(remote) == first
    assert any(c[:2] == ["git", "push"] for c in runner.calls)  # the push was attempted


def test_race_inside_the_remote_update_is_refused_and_leaves_their_branch(
    repo: Path, remote: Path
) -> None:
    """The branch appears after the push already saw no ref, while the remote applies it.

    The client advertised no branch, so only the remote's own old-value check (create-only)
    can stop this. The pre-receive hook below plays the competing writer.
    """
    first = _commit(repo, "one")
    second = _commit(repo, "two")
    _git(repo, "push", "-q", "origin", f"{first}:refs/heads/parked")
    hook = remote / "hooks" / "pre-receive"
    hook.write_text(
        "#!/bin/sh\n"
        "# receive-pack runs hooks in an object quarantine; the competing writer is not in it\n"
        "unset GIT_QUARANTINE_PATH GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES\n"
        f"git update-ref refs/heads/{BRANCH} {first} || exit 1\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    result = push_branch(replace(_plan(head=BRANCH), integration_commit=second), run_command, repo)
    assert isinstance(result, PublishError)
    assert result.reason is PublishReason.BRANCH_EXISTS
    assert _remote_sha(remote) == first


def test_unreachable_remote_is_push_failed_not_vgate(repo: Path, tmp_path: Path) -> None:
    sha = _commit(repo, "one")
    _git(repo, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    result = push_branch(replace(_plan(head=BRANCH), integration_commit=sha), run_command, repo)
    assert isinstance(result, PublishError)
    assert result.reason is PublishReason.PUSH_FAILED


# --- argv hygiene ---------------------------------------------------------------------


def test_every_git_call_is_an_argv_list_without_shell_and_never_forces(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sha = _commit(repo, "one")
    seen: list[tuple[Any, dict[str, Any]]] = []
    real_popen = subprocess.Popen

    # ``run_command`` starts every command with ``subprocess.Popen`` (it needs its own
    # process group). ``subprocess.run`` also goes through ``Popen``, so this spy sees a
    # command however the runner starts it.
    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append((args[0] if args else kwargs.get("args"), kwargs))
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(deliver_publish.subprocess, "Popen", spy)
    runner = RecordingRunner()
    assert push_branch(replace(_plan(head=BRANCH), integration_commit=sha), runner, repo) is None
    assert seen
    for argv, kwargs in seen:
        assert isinstance(argv, list) and all(isinstance(a, str) for a in argv)
        assert not kwargs.get("shell")
    # every started process is one of the runner's calls: nothing else is spawned
    assert [argv for argv, _ in seen] == runner.calls
    assert runner.calls
    for argv in runner.calls:
        assert argv[0] == "git"
        assert "--force" not in argv and "-f" not in argv
        assert "--force-with-lease" not in argv and "--no-verify" not in argv
        # the one allowed lease is create-only: it names the ref and expects it not to exist
        assert not any(a.startswith("--force") and a != CREATE_ONLY_LEASE for a in argv)
        assert not any(a.startswith("+") for a in argv)
        assert not any(a.startswith("--no-verify") for a in argv)


def test_source_has_no_force_or_skip_hook_flags() -> None:
    source = Path(deliver_publish.__file__).read_text(encoding="utf-8")
    assert "shell=True" not in source
    for needle in ('"--force"', '"--force-with-lease"', '"--no-verify"', '"-f"', 'f"+'):
        assert needle not in source
    # the only force-like string in the code is the create-only lease on one named ref
    literals = [
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "--force" in node.value
    ]
    assert literals == ["--force-with-lease=refs/heads/"]
