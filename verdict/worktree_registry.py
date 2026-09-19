"""One-story-one-worktree registry and safe lifecycle rules (BOD-65/66).

Foundations for durable resume: discover existing worktrees/branches before
create, enforce reattach-before-create, protect dirty/unmerged trees from
silent cleanup, and optionally discover PR/merged state via ``gh``.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from verdict.handoff import discover_handoff

_STORY_ID_RE = re.compile(r"^([A-Za-z]{2,10})-(\d+)$")


class WorktreeRegistryError(ValueError):
    """Base error for worktree registry operations."""


class DuplicateWorktreeError(WorktreeRegistryError):
    """Raised when creating a second worktree for an already-bound story."""


class DirtyWorktreeError(WorktreeRegistryError):
    """Raised when a destructive op targets a dirty worktree."""


def normalize_story_id(raw: str) -> str:
    text = (raw or "").strip()
    match = _STORY_ID_RE.match(text)
    if not match:
        raise WorktreeRegistryError(f"invalid story id: {raw!r}")
    return f"{match.group(1).upper()}-{match.group(2)}"


@dataclass(frozen=True)
class WorktreeEntry:
    """One ``git worktree list --porcelain`` entry plus dirty flag."""

    path: str
    head_sha: str
    branch: str | None
    bare: bool = False
    locked: bool = False
    prunable: bool = False
    dirty: bool = False

    @property
    def resolved(self) -> Path:
        return Path(self.path).resolve()


@dataclass(frozen=True)
class StoryWorktreeBinding:
    """Story → worktree mapping used by resume and scheduling."""

    story_id: str
    path: str
    branch: str | None
    head_sha: str
    base_sha: str = ""
    dirty: bool = False
    reattached: bool = False
    handoff_present: bool = False
    pr_url: str | None = None
    pr_state: str | None = None
    merged: bool | None = None


@dataclass(frozen=True)
class CleanupDecision:
    allowed: bool
    reason_code: str
    detail: str = ""


@dataclass(frozen=True)
class StaleHooks:
    """Heuristic hooks for abandoned/stale worktree detection (Wave-1)."""

    no_handoff: bool = False
    no_unique_commits: bool = False
    dirty: bool = False
    abandoned_candidate: bool = False
    details: tuple[str, ...] = ()


GhRunner = Callable[..., Any]


@dataclass
class WorktreeRegistry:
    """Discover and manage story worktrees for a repository common dir."""

    repo: Path
    gh_runner: GhRunner | None = None
    _git_timeout: float = 30.0

    def __post_init__(self) -> None:
        self.repo = Path(self.repo).resolve()

    # --- git helpers ------------------------------------------------------

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        target = Path(cwd or self.repo)
        result = subprocess.run(
            ["git", "-C", str(target), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=self._git_timeout,
        )
        return result.stdout

    def _run_gh(self, *args: str, cwd: Path | None = None) -> str:
        runner = self.gh_runner
        if runner is not None:
            return str(runner(*args, cwd=cwd))
        result = subprocess.run(
            ["gh", *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=self._git_timeout,
            cwd=str(cwd or self.repo),
        )
        return result.stdout

    def is_dirty(self, worktree: Path | str) -> bool:
        porcelain = self._git("status", "--porcelain", cwd=Path(worktree))
        return bool(porcelain.strip())

    def head_sha(self, worktree: Path | str | None = None) -> str:
        return self._git("rev-parse", "HEAD", cwd=worktree or self.repo).strip()

    def rev_parse(self, ref: str, *, worktree: Path | str | None = None) -> str:
        return self._git("rev-parse", ref, cwd=worktree or self.repo).strip()

    # --- discovery --------------------------------------------------------

    def list_worktrees(self) -> list[WorktreeEntry]:
        raw = self._git("worktree", "list", "--porcelain")
        entries: list[WorktreeEntry] = []
        current: dict[str, Any] = {}

        def flush() -> None:
            nonlocal current
            if not current.get("path"):
                current = {}
                return
            path = str(current["path"])
            dirty = False
            try:
                dirty = self.is_dirty(path)
            except (subprocess.CalledProcessError, OSError):
                dirty = False
            entries.append(
                WorktreeEntry(
                    path=path,
                    head_sha=str(current.get("head") or ""),
                    branch=current.get("branch"),
                    bare=bool(current.get("bare")),
                    locked=bool(current.get("locked")),
                    prunable=bool(current.get("prunable")),
                    dirty=dirty,
                )
            )
            current = {}

        for line in raw.splitlines():
            if not line.strip():
                flush()
                continue
            if line.startswith("worktree "):
                flush()
                current["path"] = line[len("worktree ") :]
            elif line.startswith("HEAD "):
                current["head"] = line[len("HEAD ") :]
            elif line.startswith("branch "):
                ref = line[len("branch ") :]
                current["branch"] = (
                    ref[len("refs/heads/") :] if ref.startswith("refs/heads/") else ref
                )
            elif line == "bare":
                current["bare"] = True
            elif line.startswith("locked"):
                current["locked"] = True
            elif line.startswith("prunable"):
                current["prunable"] = True
            elif line == "detached":
                current["branch"] = None
        flush()
        return entries

    def story_matches_entry(self, story_id: str, entry: WorktreeEntry) -> bool:
        sid = normalize_story_id(story_id)
        token = sid.lower()
        # Path or branch naming conventions: bod-65, BOD-65, feat/bod-65-...
        haystacks = [entry.path.lower(), (entry.branch or "").lower()]
        if any(token in h or token.replace("-", "") in h.replace("-", "") for h in haystacks):
            # Prefer explicit token presence (bod-65) over digit-only collisions.
            if token in entry.path.lower() or token in (entry.branch or "").lower():
                return True
            # Also accept path segments like bod-65-66-harness for BOD-65.
            parts = re.split(r"[^a-z0-9]+", entry.path.lower() + " " + (entry.branch or "").lower())
            if token in parts:
                return True
            # feat/bod-65-harness style: story id is a prefix of a hyphenated segment
            for part in parts:
                if part.startswith(token + "-") or part == token:
                    return True
        handoff = discover_handoff(entry.path)
        return handoff is not None and sid in handoff.primary_story_ids()

    def find_story_worktree(self, story_id: str) -> StoryWorktreeBinding | None:
        sid = normalize_story_id(story_id)
        matches: list[WorktreeEntry] = [
            e for e in self.list_worktrees() if self.story_matches_entry(sid, e)
        ]
        if not matches:
            return None

        # Prefer handoff-exact match, then non-main path.
        def rank(entry: WorktreeEntry) -> tuple[int, str]:
            handoff = discover_handoff(entry.path)
            exact = 0 if handoff and sid in handoff.primary_story_ids() else 1
            is_main = 1 if entry.resolved == self.repo else 0
            return (exact, str(is_main) + entry.path)

        matches.sort(key=rank)
        chosen = matches[0]
        handoff = discover_handoff(chosen.path)
        base_sha = ""
        if handoff and handoff.base_sha:
            base_sha = handoff.base_sha
        return StoryWorktreeBinding(
            story_id=sid,
            path=str(chosen.resolved),
            branch=chosen.branch,
            head_sha=chosen.head_sha,
            base_sha=base_sha,
            dirty=chosen.dirty,
            reattached=False,
            handoff_present=handoff is not None,
        )

    def find_all_story_bindings(self, story_id: str) -> list[StoryWorktreeBinding]:
        sid = normalize_story_id(story_id)
        out: list[StoryWorktreeBinding] = []
        for entry in self.list_worktrees():
            if not self.story_matches_entry(sid, entry):
                continue
            handoff = discover_handoff(entry.path)
            out.append(
                StoryWorktreeBinding(
                    story_id=sid,
                    path=str(entry.resolved),
                    branch=entry.branch,
                    head_sha=entry.head_sha,
                    base_sha=(handoff.base_sha if handoff else ""),
                    dirty=entry.dirty,
                    handoff_present=handoff is not None,
                )
            )
        return out

    # --- create / reattach ------------------------------------------------

    def create_story_worktree(
        self, story_id: str, *, path: Path | str, branch: str, base_ref: str = "main"
    ) -> StoryWorktreeBinding:
        sid = normalize_story_id(story_id)
        existing = self.find_all_story_bindings(sid)
        if existing:
            raise DuplicateWorktreeError(
                f"story {sid} already bound to worktree {existing[0].path}; "
                "reattach instead of creating a duplicate"
            )
        dest = Path(path)
        if dest.exists():
            raise WorktreeRegistryError(f"worktree path already exists: {dest}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        base_sha = self.rev_parse(base_ref)
        self._git("worktree", "add", "-b", branch, str(dest), base_ref)
        return StoryWorktreeBinding(
            story_id=sid,
            path=str(dest.resolve()),
            branch=branch,
            head_sha=self.head_sha(dest),
            base_sha=base_sha,
            dirty=False,
            reattached=False,
            handoff_present=False,
        )

    def ensure_story_worktree(
        self,
        story_id: str,
        *,
        desired_path: Path | str,
        desired_branch: str,
        base_ref: str = "main",
    ) -> StoryWorktreeBinding:
        """Reattach existing story worktree; create only when absent."""
        sid = normalize_story_id(story_id)
        found = self.find_story_worktree(sid)
        if found is not None:
            return StoryWorktreeBinding(
                story_id=found.story_id,
                path=found.path,
                branch=found.branch,
                head_sha=found.head_sha,
                base_sha=found.base_sha or self.rev_parse(base_ref),
                dirty=found.dirty,
                reattached=True,
                handoff_present=found.handoff_present,
                pr_url=found.pr_url,
                pr_state=found.pr_state,
                merged=found.merged,
            )
        return self.create_story_worktree(
            sid, path=desired_path, branch=desired_branch, base_ref=base_ref
        )

    # --- PR / merge discovery ---------------------------------------------

    def discover_pr(self, branch: str | None) -> dict[str, Any]:
        """Discover PR for ``branch`` via gh; degrade gracefully if unavailable."""
        if not branch:
            return {
                "available": False,
                "url": None,
                "state": None,
                "merged": None,
                "error": "no branch",
            }
        try:
            raw = self._run_gh(
                "pr",
                "list",
                "--head",
                branch,
                "--state",
                "all",
                "--json",
                "url,state,mergedAt,number",
                "--limit",
                "1",
            )
        except FileNotFoundError as exc:
            return {
                "available": False,
                "url": None,
                "state": None,
                "merged": None,
                "error": str(exc),
            }
        except (subprocess.CalledProcessError, OSError, TimeoutError) as exc:
            return {
                "available": False,
                "url": None,
                "state": None,
                "merged": None,
                "error": str(exc),
            }
        try:
            rows = json.loads(raw or "[]")
        except json.JSONDecodeError:
            return {
                "available": False,
                "url": None,
                "state": None,
                "merged": None,
                "error": "invalid gh json",
            }
        if not rows:
            return {"available": True, "url": None, "state": None, "merged": False, "error": None}
        row = rows[0]
        merged = bool(row.get("mergedAt"))
        return {
            "available": True,
            "url": row.get("url"),
            "state": row.get("state"),
            "merged": merged,
            "error": None,
        }

    # --- stale / cleanup --------------------------------------------------

    def detect_stale_hooks(self, worktree: Path | str, *, base_ref: str = "main") -> StaleHooks:
        wt = Path(worktree)
        no_handoff = discover_handoff(wt) is None
        dirty = self.is_dirty(wt)
        no_unique = False
        try:
            base = self.rev_parse(base_ref)
            head = self.head_sha(wt)
            # Commits reachable from HEAD but not base.
            out = self._git("rev-list", "--count", f"{base}..{head}", cwd=wt)
            no_unique = int(out.strip() or "0") == 0
        except (subprocess.CalledProcessError, OSError, ValueError):
            no_unique = False
        abandoned = no_handoff and no_unique and not dirty
        details: list[str] = []
        if no_handoff:
            details.append("missing .verdict/handoff.md")
        if no_unique:
            details.append("no unique commits vs base")
        if dirty:
            details.append("dirty worktree")
        return StaleHooks(
            no_handoff=no_handoff,
            no_unique_commits=no_unique,
            dirty=dirty,
            abandoned_candidate=abandoned,
            details=tuple(details),
        )

    def evaluate_cleanup(
        self, worktree: Path | str, *, merged: bool = False, proof_verified: bool = False
    ) -> CleanupDecision:
        """Safe cleanup rules: never silently delete dirty or unmerged work."""
        wt = Path(worktree)
        if self.is_dirty(wt):
            return CleanupDecision(
                allowed=False, reason_code="dirty", detail="worktree has uncommitted changes"
            )
        if not merged:
            return CleanupDecision(
                allowed=False, reason_code="unmerged", detail="branch/PR not verified merged"
            )
        if not proof_verified:
            return CleanupDecision(
                allowed=False, reason_code="proof_missing", detail="post-merge proof not verified"
            )
        return CleanupDecision(
            allowed=True, reason_code="ok", detail="clean, merged, and proof-verified"
        )

    def cleanup_worktree(
        self,
        worktree: Path | str,
        *,
        force: bool = False,
        merged: bool = False,
        proof_verified: bool = False,
        remove_branch: bool = False,
    ) -> CleanupDecision:
        decision = self.evaluate_cleanup(worktree, merged=merged, proof_verified=proof_verified)
        if not decision.allowed and not force:
            if decision.reason_code == "dirty":
                raise DirtyWorktreeError(decision.detail)
            raise WorktreeRegistryError(
                f"cleanup refused ({decision.reason_code}): {decision.detail}"
            )
        # Even force must not silently wipe dirty trees.
        if force and not decision.allowed and decision.reason_code == "dirty":
            raise DirtyWorktreeError(
                "refusing force cleanup of dirty worktree; commit or stash first"
            )
        wt = Path(worktree).resolve()
        entry = next((e for e in self.list_worktrees() if e.resolved == wt), None)
        branch = entry.branch if entry else None
        self._git("worktree", "remove", str(wt))
        if remove_branch and branch:
            try:
                self._git("branch", "-d", branch)
            except subprocess.CalledProcessError as exc:
                raise WorktreeRegistryError(
                    f"worktree removed but branch {branch} not deleted: {exc}"
                ) from exc
        return decision


def default_worktree_path(story_id: str, *, root: Path | str | None = None) -> Path:
    sid = normalize_story_id(story_id)
    base = Path(root) if root else Path.home() / "worktrees"
    return base / sid.lower()


def default_branch_name(story_id: str, *, slug: str = "work") -> str:
    sid = normalize_story_id(story_id)
    return f"feat/{sid.lower()}-{slug}"
