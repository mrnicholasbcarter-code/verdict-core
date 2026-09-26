"""Normalized cross-harness resume context and prompt (cross-harness resume context).

Workers (Cursor / Claude / Codex / Prime) are interchangeable. Resume state is
reconstructed from Git + worktree + branch + handoff (+ optional PR discovery),
never from proprietary chat history.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from verdict.handoff import discover_handoff
from verdict.worktree_registry import (
    StoryWorktreeBinding,
    WorktreeRegistry,
    WorktreeRegistryError,
    normalize_story_id,
)

PrDiscovery = Callable[[str | None], Mapping[str, Any]]


@dataclass(frozen=True)
class ResumeContext:
    """Deterministic resume projection for any harness."""

    story_id: str
    worktree_path: str
    branch: str | None
    base_sha: str
    head_sha: str
    dirty: bool
    worker: str
    previous_worker: str
    objective: str
    completed: tuple[str, ...] = ()
    currently_working_on: tuple[str, ...] = ()
    next_exact_steps: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    tests_proof: tuple[str, ...] = ()
    files_changed: tuple[str, ...] = ()
    contracts_changed: tuple[str, ...] = ()
    important_decisions: tuple[str, ...] = ()
    known_failures: tuple[str, ...] = ()
    dependencies_blockers: tuple[str, ...] = ()
    do_not: tuple[str, ...] = ()
    handoff_path: str | None = None
    handoff_present: bool = False
    reattached: bool = True
    pr_url: str | None = None
    pr_state: str | None = None
    merged: bool | None = None
    stale_abandoned_candidate: bool = False
    repo_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_resume_context(
    repo: Path | str,
    story_id: str,
    *,
    pr_discovery: PrDiscovery | None = None,
    registry: WorktreeRegistry | None = None,
) -> ResumeContext:
    """Assemble resume context from durable authorities only."""
    sid = normalize_story_id(story_id)
    reg = registry or WorktreeRegistry(Path(repo))
    binding = reg.find_story_worktree(sid)
    if binding is None:
        raise WorktreeRegistryError(f"no worktree found for story {sid}")

    wt = Path(binding.path)
    handoff = discover_handoff(wt)
    dirty = reg.is_dirty(wt)
    head = reg.head_sha(wt)
    base = binding.base_sha
    if handoff and handoff.base_sha:
        base = handoff.base_sha
    if not base:
        try:
            base = reg.rev_parse("main")
        except Exception:
            base = ""

    pr_url = None
    pr_state = None
    merged: bool | None = None
    discover = pr_discovery or (lambda branch: reg.discover_pr(branch))
    pr_info = discover(binding.branch)
    if pr_info:
        pr_url = pr_info.get("url")
        pr_state = pr_info.get("state")
        merged = pr_info.get("merged")

    stale = reg.detect_stale_hooks(wt, base_ref="main" if base else "HEAD")

    worker = handoff.worker if handoff else ""
    previous = handoff.previous_worker if handoff else "none"
    objective = handoff.objective if handoff else ""

    def _tup(items: list[str] | None) -> tuple[str, ...]:
        return tuple(items or ())

    return ResumeContext(
        story_id=sid,
        worktree_path=str(wt.resolve()),
        branch=binding.branch,
        base_sha=base,
        head_sha=head,
        dirty=dirty,
        worker=worker,
        previous_worker=previous,
        objective=objective,
        completed=_tup(handoff.completed if handoff else None),
        currently_working_on=_tup(handoff.currently_working_on if handoff else None),
        next_exact_steps=_tup(handoff.next_exact_steps if handoff else None),
        acceptance_criteria=_tup(handoff.acceptance_criteria if handoff else None),
        tests_proof=_tup(handoff.tests_proof if handoff else None),
        files_changed=_tup(handoff.files_changed if handoff else None),
        contracts_changed=_tup(handoff.contracts_changed if handoff else None),
        important_decisions=_tup(handoff.important_decisions if handoff else None),
        known_failures=_tup(handoff.known_failures if handoff else None),
        dependencies_blockers=_tup(handoff.dependencies_blockers if handoff else None),
        do_not=_tup(handoff.do_not if handoff else None),
        handoff_path=str(wt / ".verdict" / "handoff.md") if handoff else None,
        handoff_present=handoff is not None,
        reattached=True,
        pr_url=pr_url if isinstance(pr_url, str) else None,
        pr_state=pr_state if isinstance(pr_state, str) else None,
        merged=merged if isinstance(merged, bool) else None,
        stale_abandoned_candidate=stale.abandoned_candidate,
        repo_path=str(Path(repo).resolve()),
    )


def build_resume_prompt(ctx: ResumeContext, *, harness: str | None = None) -> str:
    """Generate a normalized resume prompt usable by any worker harness."""
    lines = [
        f"# Verdict resume: {ctx.story_id}",
        "",
        "You are resuming durable project work. Canonical state is Git + worktree +",
        "branch + Linear story + test/proof state + `.verdict/handoff.md`.",
        "Do not rely on proprietary chat history.",
        "",
        "## Authorities",
        f"- Story: {ctx.story_id}",
        f"- Worktree: {ctx.worktree_path}",
        f"- Branch: {ctx.branch or '(detached)'}",
        f"- Base SHA: {ctx.base_sha or '(unknown)'}",
        f"- HEAD SHA: {ctx.head_sha}",
        f"- Dirty: {'yes' if ctx.dirty else 'no'}",
        f"- Current worker/harness: {ctx.worker or '(unset)'}",
        f"- Previous worker: {ctx.previous_worker or 'none'}",
    ]
    if harness:
        lines.append(f"- Requested launcher: {harness}")
    if ctx.handoff_path:
        lines.append(f"- Handoff: {ctx.handoff_path}")
    else:
        lines.append("- Handoff: MISSING — create `.verdict/handoff.md` before yielding")
    if ctx.pr_url:
        lines.append(f"- PR: {ctx.pr_url} (state={ctx.pr_state}, merged={ctx.merged})")
    if ctx.dirty:
        lines.append("- WARNING: worktree is dirty — preserve changes; do not reset/rebase/delete")
    if ctx.stale_abandoned_candidate:
        lines.append(
            "- NOTE: stale/abandoned candidate hooks fired (no handoff + no unique commits)"
        )
    lines.extend(
        [
            "",
            "## Objective",
            ctx.objective or "(read Linear story + handoff)",
            "",
            "## Completed (do not redo)",
        ]
    )
    lines.extend(f"- {item}" for item in (ctx.completed or ("(none recorded)",)))
    lines.extend(["", "## Currently working on"])
    lines.extend(f"- {item}" for item in (ctx.currently_working_on or ("(none)",)))
    lines.extend(["", "## Next exact steps"])
    if ctx.next_exact_steps:
        for idx, item in enumerate(ctx.next_exact_steps, start=1):
            lines.append(f"{idx}. {item}")
    else:
        lines.append("1. Read handoff + Linear ACs; continue first unfinished item")
    lines.extend(["", "## Acceptance criteria"])
    lines.extend(f"- {item}" for item in (ctx.acceptance_criteria or ("(see Linear)",)))
    if ctx.important_decisions:
        lines.extend(["", "## Preserve these decisions unless new evidence"])
        lines.extend(f"- {item}" for item in ctx.important_decisions)
    if ctx.do_not:
        lines.extend(["", "## Do not"])
        lines.extend(f"- {item}" for item in ctx.do_not)
    lines.extend(
        [
            "",
            "## Required resume protocol",
            "1. Read `.verdict/handoff.md` and `git status` / recent history in this worktree.",
            "2. Treat filesystem, Git, and Linear as authoritative over chat memory.",
            "3. Do not redo completed work; continue the next unfinished acceptance criterion.",
            "4. Preserve architectural decisions unless new evidence contradicts them.",
            "5. Run required proof/tests before claiming done.",
            "6. Update `.verdict/handoff.md` before yielding (previous worker = current worker).",
            "7. Never silently rebase, reset, or delete dirty/unmerged worktrees.",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def resume_story(
    repo: Path | str,
    story_id: str,
    *,
    with_harness: str | None = None,
    create_if_missing: bool = False,
    desired_path: Path | str | None = None,
    desired_branch: str | None = None,
    base_ref: str = "main",
) -> dict[str, Any]:
    """High-level resume entry used by the CLI.

    Returns a JSON-serializable payload including normalized resume prompt.
    Launcher ``--with`` is recorded as a stub until harness adapters land.
    """
    reg = WorktreeRegistry(Path(repo))
    sid = normalize_story_id(story_id)
    binding: StoryWorktreeBinding | None = reg.find_story_worktree(sid)
    reattached = True
    if binding is None:
        if not create_if_missing:
            raise WorktreeRegistryError(
                f"no worktree for {sid}; pass create_if_missing or create via ensure_story_worktree"
            )
        from verdict.worktree_registry import default_branch_name, default_worktree_path

        binding = reg.ensure_story_worktree(
            sid,
            desired_path=desired_path or default_worktree_path(sid),
            desired_branch=desired_branch or default_branch_name(sid),
            base_ref=base_ref,
        )
        reattached = binding.reattached

    ctx = build_resume_context(repo, sid, registry=reg)
    prompt = build_resume_prompt(ctx, harness=with_harness)
    payload: dict[str, Any] = {
        "story_id": sid,
        "worktree": ctx.worktree_path,
        "branch": ctx.branch,
        "base_sha": ctx.base_sha,
        "head_sha": ctx.head_sha,
        "dirty": ctx.dirty,
        "reattach": reattached or ctx.reattached,
        "previous_worker": ctx.previous_worker,
        "worker": ctx.worker,
        "handoff_present": ctx.handoff_present,
        "handoff_path": ctx.handoff_path,
        "pr_url": ctx.pr_url,
        "pr_state": ctx.pr_state,
        "merged": ctx.merged,
        "stale_abandoned_candidate": ctx.stale_abandoned_candidate,
        "resume_prompt": prompt,
        "context": ctx.to_dict(),
        "with_harness": with_harness,
        "launcher_status": "stub" if with_harness else None,
    }
    return payload
