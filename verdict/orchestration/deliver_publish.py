"""Git and GitHub CLI adapter behind ``verdict deliver``.

Every external command goes through an injectable ``CommandRunner`` that takes an
argv list and a working directory. The default runner starts each command with an
argument list, a timeout, no shell and its own process group, and ends that whole
group when the command times out or is interrupted. This module never
force-pushes, never pushes to the base or default branch, and redacts secrets
from any captured output before it reaches an error message.

``publish_branch_and_pr`` pushes the integration commit to the plan's head
branch and opens (or reuses) a draft pull request. See the ``verdict-deliver``
OpenSpec change.
"""

from __future__ import annotations

import contextlib
import functools
import json
import os
import re
import signal
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from verdict.orchestration.deliver import DeliverPlan
from verdict.security import redact_text

DEFAULT_COMMAND_TIMEOUT_S = 120.0
DEFAULT_PUSH_TIMEOUT_S = 1800.0
PROTECTED_BRANCHES = frozenset({"main", "master"})
MAX_ERROR_DETAIL = 2000
_MISSING_EXECUTABLE_RC = 127
_TIMEOUT_RC = 124
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")
_PR_URL_RE = re.compile(r"https?://\S+/pull/\d+")
_REF_REJECTED_RE = re.compile(r"^\s*!\s*\[", re.MULTILINE)
_REF_CONFLICT_MARKERS = ("non-fast-forward", "fetch first", "already exists", "stale info")
_PUSH_FAILED_LINE = "failed to push some refs"
_TIMED_OUT_RE = re.compile(r"^command timed out after (\S+)s$")
_KILL_GRACE_S = 5.0


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


CommandRunner = Callable[[Sequence[str], Path | None], CommandResult]


def run_command(
    argv: Sequence[str], cwd: Path | None = None, *, timeout: float = DEFAULT_COMMAND_TIMEOUT_S
) -> CommandResult:
    """Default runner: an argv list, no shell, a timeout, and its own process group.

    On a timeout, or when the caller is interrupted, the whole group gets SIGTERM, then
    SIGKILL after a short grace period, so a hook or helper the command started cannot
    outlive it.
    """
    try:
        proc = subprocess.Popen(
            list(argv),
            cwd=str(cwd) if cwd is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        return CommandResult(_MISSING_EXECUTABLE_RC, "", f"{exc}")
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _end_process_group(proc)
        return CommandResult(_TIMEOUT_RC, "", f"command timed out after {timeout:g}s")
    except BaseException:
        # The child is in its own session, so Ctrl-C never reaches it: end it here.
        _end_process_group(proc)
        raise
    return CommandResult(proc.returncode, stdout or "", stderr or "")


def _signal_group(pgid: int, sig: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, sig)


def _end_process_group(proc: subprocess.Popen[str]) -> None:
    """Stop ``proc`` and everything in its session, then reap it."""
    pgid = proc.pid
    _signal_group(pgid, signal.SIGTERM)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.communicate(timeout=_KILL_GRACE_S)
    _signal_group(pgid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.communicate(timeout=_KILL_GRACE_S)
    proc.wait()


class PublishReason(str, Enum):
    """Stable failure codes. Values are part of the delivery record contract."""

    INVALID_REQUEST = "invalid_request"
    HEAD_IS_BASE = "head_is_base"
    GIT_MISSING = "git_missing"
    GH_MISSING = "gh_missing"
    GH_UNAUTHENTICATED = "gh_unauthenticated"
    BRANCH_EXISTS = "branch_exists"
    VGATE_REJECTED = "vgate_rejected"
    PUSH_FAILED = "push_failed"
    PR_LOOKUP_FAILED = "pr_lookup_failed"
    PR_CREATE_FAILED = "pr_create_failed"
    PR_NOT_DRAFT = "pr_not_draft"
    WORKTREE_NOT_AT_INTEGRATION_COMMIT = "worktree_not_at_integration_commit"


@dataclass(frozen=True)
class PublishError:
    reason: PublishReason
    message: str


@dataclass(frozen=True)
class PublishResult:
    pr_url: str
    branch: str
    created: bool

    @property
    def reused(self) -> bool:
        return not self.created


def _detail(result: CommandResult) -> str:
    text = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
    return redact_text(text)[-MAX_ERROR_DETAIL:]


def _error(
    reason: PublishReason, message: str, result: CommandResult | None = None
) -> PublishError:
    detail = _detail(result) if result is not None else ""
    text = f"{message}: {detail}" if detail else message
    return PublishError(reason, redact_text(text))


def validate_request(plan: DeliverPlan) -> PublishError | None:
    """Refuse an unsafe branch plan before any command runs."""
    head = plan.head_branch.strip()
    base = plan.base_branch.strip()
    if not head or not base:
        return _error(PublishReason.INVALID_REQUEST, "head and base branch are required")
    if head == base or head.lower() in PROTECTED_BRANCHES or head.lower() == base.lower():
        return _error(
            PublishReason.HEAD_IS_BASE, f"refusing to publish to protected branch {head!r}"
        )
    if not _BRANCH_RE.match(head) or ".." in head or head.endswith(("/", ".lock")):
        return _error(PublishReason.INVALID_REQUEST, f"unsafe head branch name {head!r}")
    if not _BRANCH_RE.match(base):
        return _error(PublishReason.INVALID_REQUEST, f"unsafe base branch name {base!r}")
    if not _SHA_RE.match(plan.integration_commit.strip()):
        return _error(PublishReason.INVALID_REQUEST, "integration commit is not a git sha")
    return None


def check_tools(runner: CommandRunner, cwd: Path | None = None) -> PublishError | None:
    """Check that git and gh are installed and gh is authenticated."""
    git = runner(["git", "--version"], cwd)
    if git.returncode != 0:
        return _error(PublishReason.GIT_MISSING, "git is not available", git)
    gh = runner(["gh", "--version"], cwd)
    if gh.returncode != 0:
        return _error(PublishReason.GH_MISSING, "gh is not available", gh)
    auth = runner(["gh", "auth", "status"], cwd)
    if auth.returncode != 0:
        return _error(PublishReason.GH_UNAUTHENTICATED, "gh is not authenticated", auth)
    return None


def _remote_branch_sha(
    runner: CommandRunner, branch: str, cwd: Path | None
) -> tuple[str | None, PublishError | None]:
    result = runner(["git", "ls-remote", "--heads", "origin", f"refs/heads/{branch}"], cwd)
    if result.returncode != 0:
        return None, _error(PublishReason.PUSH_FAILED, "could not read remote branch", result)
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == f"refs/heads/{branch}":
            return parts[0], None
    return None, None


def _classify_push_failure(result: CommandResult, branch: str) -> PublishError:
    """Map a failed ``git push`` to ``branch_exists``, ``vgate_rejected`` or ``push_failed``."""
    timed_out = _TIMED_OUT_RE.search(result.stderr) if result.returncode == _TIMEOUT_RC else None
    if timed_out is not None:
        return _error(
            PublishReason.PUSH_FAILED,
            f"git push timed out after {timed_out.group(1)} seconds; "
            "raise the limit with --push-timeout MINUTES",
        )
    text = f"{result.stderr}\n{result.stdout}".lower()
    if any(marker in text for marker in _REF_CONFLICT_MARKERS):
        return _error(
            PublishReason.BRANCH_EXISTS, f"remote branch {branch!r} already exists", result
        )
    # A pre-push hook that exits non-zero aborts git before any ref is sent: git
    # exits 1 and prints only its summary line, with no per-ref "! [..." status.
    if (
        result.returncode == 1
        and _PUSH_FAILED_LINE in text
        and _REF_REJECTED_RE.search(text) is None
    ):
        return _error(PublishReason.VGATE_REJECTED, "pre-push gate rejected the push", result)
    return _error(PublishReason.PUSH_FAILED, "git push failed", result)


def push_branch(
    plan: DeliverPlan,
    runner: CommandRunner,
    cwd: Path | None = None,
    *,
    push_runner: CommandRunner | None = None,
) -> PublishError | None:
    """Push the integration commit to the head branch as a create-only push; never overwrite.

    A remote branch at the same commit is reused. One at a different commit, or a
    push that races with another writer, is refused with ``branch_exists``. A
    non-zero pre-push hook (vgate) gives ``vgate_rejected`` with the gate tail.
    The push never skips the hook and cannot move an existing remote branch. Only the
    ``git push`` call goes to ``push_runner`` (default: ``runner``).
    """
    refused = validate_request(plan)
    if refused is not None:
        return refused
    branch = plan.head_branch.strip()
    commit = plan.integration_commit.strip()
    remote_sha, failure = _remote_branch_sha(runner, branch, cwd)
    if failure is not None:
        return failure
    if remote_sha is not None:
        if remote_sha == commit or (
            len(commit) < len(remote_sha) and remote_sha.startswith(commit)
        ):
            return None
        return _error(
            PublishReason.BRANCH_EXISTS,
            f"remote branch {branch!r} already points at a different commit",
        )
    # The lookup above is only advice: another writer can create the branch before this
    # push. An ordinary push would then fast-forward their branch to ours when it sits at an
    # ancestor of the commit. The lease below names this one ref with an EMPTY expected
    # value, which git defines as "the ref must not already exist" (git-push, the
    # force-with-lease option). That is a create-only update, checked by the client against
    # the ref advertisement and again by the remote when it applies the update. It is not a
    # force-push: a lease whose expectation is "absent" can only ever create the ref, so
    # it cannot overwrite or rewind a branch, which the design forbids. The pre-push hook
    # still runs.
    result = (push_runner or runner)(
        [
            "git",
            "push",
            f"--force-with-lease=refs/heads/{branch}:",
            "origin",
            f"{commit}:refs/heads/{branch}",
        ],
        cwd,
    )
    if result.returncode != 0:
        return _classify_push_failure(result, branch)
    return None


def check_worktree_at_commit(
    plan: DeliverPlan, runner: CommandRunner, cwd: Path | None = None
) -> PublishError | None:
    """Check that ``cwd`` has the integration commit checked out and a clean tree.

    The pre-push hook tests the checkout, not the pushed commit, so pushing from a
    directory at another commit, or with local changes, would gate the wrong content.
    Ignored files are not counted. The status command runs only after HEAD matched.
    """
    commit = plan.integration_commit.strip()
    where = str(cwd) if cwd is not None else "the current directory"
    fix = (
        f"run `git merge --ff-only {commit}` or `git checkout --detach {commit}` in {where}, "
        "with a clean working tree, then try again"
    )
    head = runner(["git", "rev-parse", "HEAD"], cwd)
    if head.returncode != 0:
        return _error(
            PublishReason.WORKTREE_NOT_AT_INTEGRATION_COMMIT,
            f"could not read HEAD in {where}; {fix}",
            head,
        )
    head_sha = head.stdout.strip()
    if not head_sha:
        return _error(
            PublishReason.WORKTREE_NOT_AT_INTEGRATION_COMMIT,
            f"git rev-parse HEAD printed nothing in {where}; {fix}",
        )
    if not (head_sha == commit or (len(commit) < len(head_sha) and head_sha.startswith(commit))):
        return _error(
            PublishReason.WORKTREE_NOT_AT_INTEGRATION_COMMIT,
            f"{where} is at {head_sha}, not the integration commit {commit}; {fix}",
        )
    # Explicit flags: a local ``status.showUntrackedFiles`` or submodule-ignore setting
    # must not hide a change from this check.
    status = runner(
        ["git", "status", "--porcelain", "--untracked-files=all", "--ignore-submodules=none"], cwd
    )
    if status.returncode != 0:
        return _error(
            PublishReason.WORKTREE_NOT_AT_INTEGRATION_COMMIT,
            f"could not read the working tree status in {where}; {fix}",
            status,
        )
    if status.stdout.strip():
        return _error(
            PublishReason.WORKTREE_NOT_AT_INTEGRATION_COMMIT,
            f"{where} has uncommitted or untracked changes: "
            f"{status.stdout.strip()[:MAX_ERROR_DETAIL]}; {fix}",
        )
    return None


def find_open_pr(
    plan: DeliverPlan, runner: CommandRunner, cwd: Path | None = None
) -> tuple[str | None, PublishError | None]:
    """Return the URL of an open draft PR for the head branch, if one exists.

    Only a draft is ever reused: the ready gate leaves a pull request unchanged when it
    stops, so reusing one that is already ready would record a ready PR as not ready.
    An open PR that is ready is ``pr_not_draft``; one whose draft state cannot be read
    is ``pr_lookup_failed``.
    """
    result = runner(
        [
            "gh",
            "pr",
            "list",
            "--state",
            "open",
            "--head",
            plan.head_branch.strip(),
            "--base",
            plan.base_branch.strip(),
            "--json",
            "number,url,isDraft",
            "--limit",
            "1",
        ],
        cwd,
    )
    if result.returncode != 0:
        return None, _error(PublishReason.PR_LOOKUP_FAILED, "gh pr list failed", result)
    try:
        rows = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return None, _error(PublishReason.PR_LOOKUP_FAILED, "gh pr list returned invalid JSON")
    if not isinstance(rows, list):
        return None, _error(PublishReason.PR_LOOKUP_FAILED, "gh pr list returned unexpected data")
    for row in rows:
        url = row.get("url") if isinstance(row, dict) else None
        if not isinstance(url, str) or not url:
            continue
        is_draft = row.get("isDraft")
        if not isinstance(is_draft, bool):
            return None, _error(
                PublishReason.PR_LOOKUP_FAILED,
                f"gh pr list did not say whether open pull request {url} is a draft",
            )
        if not is_draft:
            return None, _error(
                PublishReason.PR_NOT_DRAFT,
                f"open pull request {url} is already marked ready for review; "
                "verdict deliver reuses only a draft and never changes a ready pull request",
            )
        return url, None
    return None, None


def create_draft_pr(
    plan: DeliverPlan, title: str, body: str, runner: CommandRunner, cwd: Path | None = None
) -> tuple[str | None, PublishError | None]:
    """Create a draft PR. The body goes through a temp file, never argv."""
    fd, name = tempfile.mkstemp(prefix="verdict-deliver-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
        result = runner(
            [
                "gh",
                "pr",
                "create",
                "--draft",
                "--base",
                plan.base_branch.strip(),
                "--head",
                plan.head_branch.strip(),
                "--title",
                title,
                "--body-file",
                name,
            ],
            cwd,
        )
    finally:
        with contextlib.suppress(OSError):
            os.unlink(name)
    if result.returncode != 0:
        return None, _error(PublishReason.PR_CREATE_FAILED, "gh pr create failed", result)
    match = _PR_URL_RE.search(result.stdout)
    if match is None:
        return None, _error(PublishReason.PR_CREATE_FAILED, "gh pr create printed no PR URL")
    return match.group(0), None


def publish_branch_and_pr(
    plan: DeliverPlan,
    title: str,
    body: str,
    *,
    runner: CommandRunner = run_command,
    push_runner: CommandRunner | None = None,
    cwd: Path | None = None,
) -> PublishResult | PublishError:
    """Push the head branch and create or reuse the draft PR.

    ``push_runner`` runs the ``git push`` call only. Left unset, the default runner
    pushes with ``DEFAULT_PUSH_TIMEOUT_S`` (a pre-push hook can be slow) and an
    injected ``runner`` runs the push as well.
    """
    refused = validate_request(plan)
    if refused is not None:
        return refused
    if not title.strip():
        return _error(PublishReason.INVALID_REQUEST, "pull request title is required")
    unavailable = check_tools(runner, cwd)
    if unavailable is not None:
        return unavailable
    if push_runner is None and runner is run_command:
        push_runner = functools.partial(run_command, timeout=DEFAULT_PUSH_TIMEOUT_S)
    pushed = push_branch(plan, runner, cwd, push_runner=push_runner)
    if pushed is not None:
        return pushed
    branch = plan.head_branch.strip()
    existing, lookup_error = find_open_pr(plan, runner, cwd)
    if lookup_error is not None:
        return lookup_error
    if existing is not None:
        return PublishResult(pr_url=existing, branch=branch, created=False)
    url, create_error = create_draft_pr(plan, title, body, runner, cwd)
    if create_error is not None:
        return create_error
    assert url is not None
    return PublishResult(pr_url=url, branch=branch, created=True)
