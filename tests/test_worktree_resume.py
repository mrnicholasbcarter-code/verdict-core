"""the worktree registry/handoff/resume design/66 Wave-1 foundations: worktree registry, handoff, resume."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from verdict.handoff import (
    HandoffDocument,
    HandoffError,
    parse_handoff,
    read_handoff,
    write_handoff,
)
from verdict.resume import ResumeContext, build_resume_context, build_resume_prompt
from verdict.worktree_registry import (
    CleanupDecision,
    DirtyWorktreeError,
    DuplicateWorktreeError,
    WorktreeRegistry,
    WorktreeRegistryError,
    normalize_story_id,
)


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=check, capture_output=True, text=True
    )


def _init_repo(root: Path) -> Path:
    repo = root / "main"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "init")
    # Default branch name varies; normalize to main.
    _git(repo, "branch", "-M", "main")
    return repo


def _add_worktree(repo: Path, path: Path, branch: str, *, start_point: str = "main") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "-b", branch, str(path), start_point)
    return path


# --- story id / discovery -------------------------------------------------


def test_normalize_story_id() -> None:
    assert normalize_story_id("bod-65") == "BOD-65"
    assert normalize_story_id("BOD-65") == "BOD-65"
    assert normalize_story_id("  bod-123  ") == "BOD-123"
    with pytest.raises(WorktreeRegistryError):
        normalize_story_id("not-a-story")


def test_discover_lists_worktrees_with_branch_and_head(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    wt = _add_worktree(repo, tmp_path / "worktrees" / "bod-65", "feat/bod-65-harness")
    registry = WorktreeRegistry(repo)
    entries = registry.list_worktrees()
    paths = {Path(e.path).resolve() for e in entries}
    assert repo.resolve() in paths
    assert wt.resolve() in paths
    match = next(e for e in entries if Path(e.path).resolve() == wt.resolve())
    assert match.branch == "feat/bod-65-harness"
    assert len(match.head_sha) == 40
    assert match.dirty is False


def test_find_story_worktree_by_branch_and_path(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    wt = _add_worktree(repo, tmp_path / "worktrees" / "bod-65-harness", "feat/bod-65-harness")
    registry = WorktreeRegistry(repo)
    found = registry.find_story_worktree("BOD-65")
    assert found is not None
    assert Path(found.path).resolve() == wt.resolve()
    assert found.story_id == "BOD-65"
    assert found.branch == "feat/bod-65-harness"


def test_find_story_worktree_via_handoff(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    wt = _add_worktree(repo, tmp_path / "worktrees" / "custom-name", "feat/custom")
    handoff_dir = wt / ".verdict"
    handoff_dir.mkdir()
    write_handoff(
        handoff_dir / "handoff.md",
        HandoffDocument(
            story="BOD-66",
            worker="Cursor",
            worktree=str(wt),
            branch="feat/custom",
            base_sha="a" * 40,
            current_sha=_git(wt, "rev-parse", "HEAD").stdout.strip(),
            previous_worker="none",
            objective="test",
        ),
    )
    registry = WorktreeRegistry(repo)
    found = registry.find_story_worktree("BOD-66")
    assert found is not None
    assert Path(found.path).resolve() == wt.resolve()


# --- one-story-one-worktree / reattach-before-create ----------------------


def test_ensure_reattaches_existing_before_create(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    wt = _add_worktree(repo, tmp_path / "worktrees" / "bod-65", "feat/bod-65")
    registry = WorktreeRegistry(repo)
    binding = registry.ensure_story_worktree(
        "BOD-65",
        desired_path=tmp_path / "worktrees" / "bod-65-NEW",
        desired_branch="feat/bod-65-NEW",
    )
    assert Path(binding.path).resolve() == wt.resolve()
    assert binding.reattached is True
    assert not (tmp_path / "worktrees" / "bod-65-NEW").exists()


def test_refuse_duplicate_create_when_story_already_bound(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _add_worktree(repo, tmp_path / "worktrees" / "bod-65", "feat/bod-65")
    registry = WorktreeRegistry(repo)
    with pytest.raises(DuplicateWorktreeError, match="BOD-65"):
        registry.create_story_worktree(
            "BOD-65", path=tmp_path / "worktrees" / "bod-65-dup", branch="feat/bod-65-dup"
        )


def test_create_when_absent(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    registry = WorktreeRegistry(repo)
    binding = registry.ensure_story_worktree(
        "BOD-99",
        desired_path=tmp_path / "worktrees" / "bod-99",
        desired_branch="feat/bod-99",
        base_ref="main",
    )
    assert binding.reattached is False
    assert Path(binding.path).is_dir()
    assert binding.branch == "feat/bod-99"
    assert binding.base_sha == _git(repo, "rev-parse", "main").stdout.strip()


# --- dirty protection / safe cleanup --------------------------------------


def test_dirty_blocks_destructive_cleanup(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    wt = _add_worktree(repo, tmp_path / "worktrees" / "bod-65", "feat/bod-65")
    (wt / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
    registry = WorktreeRegistry(repo)
    decision = registry.evaluate_cleanup(wt)
    assert decision.allowed is False
    assert decision.reason_code == "dirty"
    with pytest.raises(DirtyWorktreeError):
        registry.cleanup_worktree(wt, force=False)


def test_cleanup_refuses_unmerged_without_proof(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    wt = _add_worktree(repo, tmp_path / "worktrees" / "bod-65", "feat/bod-65")
    (wt / "change.txt").write_text("commit me\n", encoding="utf-8")
    _git(wt, "add", "change.txt")
    _git(wt, "commit", "-q", "-m", "story work")
    registry = WorktreeRegistry(repo)
    decision = registry.evaluate_cleanup(wt, merged=False, proof_verified=False)
    assert decision.allowed is False
    assert decision.reason_code in {"unmerged", "proof_missing"}


def test_cleanup_allowed_only_when_clean_merged_and_proofed(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    wt = _add_worktree(repo, tmp_path / "worktrees" / "bod-65", "feat/bod-65")
    registry = WorktreeRegistry(repo)
    decision = registry.evaluate_cleanup(wt, merged=True, proof_verified=True)
    assert isinstance(decision, CleanupDecision)
    assert decision.allowed is True


def test_stale_abandoned_hook_flags_inactive_worktree(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    wt = _add_worktree(repo, tmp_path / "worktrees" / "bod-65", "feat/bod-65")
    registry = WorktreeRegistry(repo)
    # No handoff, no unique commits beyond base → stale candidate hook.
    flags = registry.detect_stale_hooks(wt, base_ref="main")
    assert flags.no_handoff is True
    assert flags.no_unique_commits is True
    assert flags.abandoned_candidate is True


# --- handoff parse/write --------------------------------------------------


def test_handoff_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "handoff.md"
    doc = HandoffDocument(
        story="BOD-65 + BOD-66",
        worker="Cursor",
        worktree="/tmp/wt",
        branch="feat/bod-65-66",
        base_sha="b" * 40,
        current_sha="c" * 40,
        previous_worker="Codex",
        objective="Durable resume",
        completed=["step a"],
        currently_working_on=["step b"],
        next_exact_steps=["step c"],
        acceptance_criteria=["[ ] ac1", "[x] ac2"],
        tests_proof=["pytest -q"],
        files_changed=["verdict/resume.py"],
        contracts_changed=["handoff schema"],
        important_decisions=["no chat history"],
        known_failures=["none"],
        dependencies_blockers=["none"],
        do_not=["edit primary"],
    )
    write_handoff(path, doc)
    loaded = read_handoff(path)
    assert loaded.story == "BOD-65 + BOD-66"
    assert loaded.previous_worker == "Codex"
    assert loaded.worker == "Cursor"
    assert "step a" in loaded.completed
    assert "[x] ac2" in loaded.acceptance_criteria
    assert loaded.primary_story_ids() == ("BOD-65", "BOD-66")


def test_parse_handoff_requires_story() -> None:
    with pytest.raises(HandoffError, match="Story"):
        parse_handoff("# Verdict Handoff\n\nObjective: x\n")


# --- resume context + prompt ----------------------------------------------


def test_build_resume_context_without_chat_history(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    wt = _add_worktree(repo, tmp_path / "worktrees" / "bod-65", "feat/bod-65")
    base = _git(repo, "rev-parse", "main").stdout.strip()
    write_handoff(
        wt / ".verdict" / "handoff.md",
        HandoffDocument(
            story="BOD-65",
            worker="Claude",
            worktree=str(wt),
            branch="feat/bod-65",
            base_sha=base,
            current_sha=_git(wt, "rev-parse", "HEAD").stdout.strip(),
            previous_worker="Cursor",
            objective="Resume foundations",
            next_exact_steps=["Implement CLI"],
            acceptance_criteria=["[ ] resume prompt"],
        ),
    )
    _git(wt, "add", ".verdict/handoff.md")
    _git(wt, "commit", "-q", "-m", "handoff")
    ctx = build_resume_context(repo, "BOD-65")
    assert isinstance(ctx, ResumeContext)
    assert ctx.story_id == "BOD-65"
    assert Path(ctx.worktree_path).resolve() == wt.resolve()
    assert ctx.previous_worker == "Cursor"
    assert ctx.worker == "Claude"
    assert ctx.base_sha == base
    assert ctx.dirty is False
    assert "chat" not in ctx.to_dict()
    prompt = build_resume_prompt(ctx)
    assert "BOD-65" in prompt
    assert ".verdict/handoff.md" in prompt
    assert "do not redo completed work" in prompt.lower()
    assert ctx.previous_worker in prompt


def test_resume_prompt_mentions_dirty_and_pr_when_present(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    wt = _add_worktree(repo, tmp_path / "worktrees" / "bod-65", "feat/bod-65")
    (wt / "x.txt").write_text("dirty\n", encoding="utf-8")
    write_handoff(
        wt / ".verdict" / "handoff.md",
        HandoffDocument(
            story="BOD-65",
            worker="Codex",
            worktree=str(wt),
            branch="feat/bod-65",
            base_sha=_git(repo, "rev-parse", "main").stdout.strip(),
            current_sha=_git(wt, "rev-parse", "HEAD").stdout.strip(),
            previous_worker="none",
            objective="obj",
        ),
    )
    ctx = build_resume_context(
        repo,
        "BOD-65",
        pr_discovery=lambda _branch: {
            "url": "https://example.com/pr/1",
            "state": "OPEN",
            "merged": False,
        },
    )
    assert ctx.dirty is True
    assert ctx.pr_url == "https://example.com/pr/1"
    prompt = build_resume_prompt(ctx)
    assert "dirty" in prompt.lower()
    assert "https://example.com/pr/1" in prompt


def test_cli_resume_foundations_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from verdict import cli

    repo = _init_repo(tmp_path)
    wt = _add_worktree(repo, tmp_path / "worktrees" / "bod-65", "feat/bod-65")
    write_handoff(
        wt / ".verdict" / "handoff.md",
        HandoffDocument(
            story="BOD-65",
            worker="Cursor",
            worktree=str(wt),
            branch="feat/bod-65",
            base_sha=_git(repo, "rev-parse", "main").stdout.strip(),
            current_sha=_git(wt, "rev-parse", "HEAD").stdout.strip(),
            previous_worker="none",
            objective="CLI resume",
            next_exact_steps=["Ship foundations"],
        ),
    )
    monkeypatch.chdir(repo)
    # Capture stdout via cmd returning payload path — call cmd_resume directly.
    payload = cli.cmd_resume("BOD-65", with_harness=None, output_json=True, repo=repo)
    assert payload["story_id"] == "BOD-65"
    assert payload["reattach"] is True
    assert "resume_prompt" in payload
    # --with stub records launcher without executing proprietary tooling.
    stub = cli.cmd_resume("BOD-65", with_harness="claude", output_json=True, repo=repo)
    assert stub["with_harness"] == "claude"
    assert stub["launcher_status"] == "stub"


def test_pr_discovery_degrades_when_gh_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path)
    registry = WorktreeRegistry(repo)

    def _boom(*_a, **_k):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(registry, "_run_gh", _boom)
    info = registry.discover_pr("feat/bod-65")
    assert info["available"] is False
    assert info["url"] is None
    assert info["state"] is None
