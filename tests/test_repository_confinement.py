import os
import subprocess
from pathlib import Path

import pytest

from verdict.autodev_run import AutodevError
from verdict.patch_executor import PatchExecutorError, build_unit_prompt
from verdict.repository_files import UnsafeRepositoryPathError, read_repository_text
from verdict.work_unit import WorkUnit


def _unit(owned: str) -> WorkUnit:
    return WorkUnit("repair", "repair owned file", (owned,), ("true",))


def test_prompt_rejects_parent_symlink(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "sentinel.txt").write_text("HOST_BYTES_MUST_NOT_LEAK", encoding="utf-8")
    (repo / "linked").symlink_to(external, target_is_directory=True)
    unit = WorkUnit("repair", "repair owned file", ("linked/sentinel.txt",), ("true",))

    with pytest.raises(PatchExecutorError, match=r"unsafe|symlink"):
        build_unit_prompt(unit, repo)


def test_prompt_rejects_leaf_symlink(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    secret = tmp_path / "host-secret.txt"
    secret.write_text("must-not-reach-model\n", encoding="utf-8")
    (repo / "owned.py").symlink_to(secret)

    with pytest.raises(PatchExecutorError, match="symlink"):
        build_unit_prompt(_unit("owned.py"), repo)


def test_prompt_reads_nested_regular_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "pkg" / "sub").mkdir(parents=True)
    (repo / "pkg" / "sub" / "owned.py").write_text("print('ok')\n", encoding="utf-8")

    prompt = build_unit_prompt(_unit("pkg/sub/owned.py"), repo)

    assert "print('ok')" in prompt


def test_absent_file_remains_ordinary_omission(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    prompt = build_unit_prompt(_unit("missing.py"), repo)

    assert "missing.py (unreadable: FileNotFoundError)" in prompt


def test_fifo_leaf_is_refused(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    os.mkfifo(str(repo / "owned.py"))

    with pytest.raises(UnsafeRepositoryPathError, match="not a regular file"):
        read_repository_text(repo, "owned.py")


def test_traversal_and_absolute_paths_are_refused(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    for bad in ("../escape.txt", "/etc/passwd", "a/../../escape.txt"):
        with pytest.raises(UnsafeRepositoryPathError):
            read_repository_text(repo, bad)


def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def _fresh_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "main"
    repo.mkdir()
    _git(["init", "-q", "--initial-branch=main"], repo)
    _git(["config", "user.email", "t@t"], repo)
    _git(["config", "user.name", "t"], repo)
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "base"], repo)
    return repo


def test_attempt_digest_refuses_parent_symlink(tmp_path: Path) -> None:
    from verdict.autodev_run import _attempt_digest

    repo = _fresh_repo(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    (external / "sentinel.txt").write_text("HOST", encoding="utf-8")
    (repo / "linked").symlink_to(external, target_is_directory=True)

    with pytest.raises(UnsafeRepositoryPathError):
        _attempt_digest(repo, ("linked/sentinel.txt",))


def test_replay_refuses_source_parent_symlink_before_applying_diff(tmp_path: Path) -> None:
    from verdict.autodev_run import _make_attempt_worktree, _replay_attempt

    repo = _fresh_repo(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    (external / "secret.txt").write_text("HOST_SECRET", encoding="utf-8")
    attempt = _make_attempt_worktree(repo, _git(["rev-parse", "HEAD"], repo).strip())
    try:
        (attempt / "tracked-change").write_text("x\n", encoding="utf-8")
        _git(["add", "tracked-change"], attempt)
        (attempt / "pkg").symlink_to(external, target_is_directory=True)
        # ls-files --others lists pkg/secret.txt through the symlinked dir

        with pytest.raises((AutodevError, UnsafeRepositoryPathError), match="symlink"):
            _replay_attempt(attempt, repo)
        assert not (repo / "tracked-change").exists(), (
            "diff must not apply when untracked replay is refused"
        )
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(attempt)],
            cwd=repo,
            check=False,
            capture_output=True,
        )


def test_replay_refuses_destination_parent_symlink(tmp_path: Path) -> None:
    from verdict.autodev_run import _make_attempt_worktree, _replay_attempt

    repo = _fresh_repo(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    (repo / "out").symlink_to(external, target_is_directory=True)
    attempt = _make_attempt_worktree(repo, _git(["rev-parse", "HEAD"], repo).strip())
    try:
        (attempt / "out").mkdir()
        (attempt / "out" / "f.txt").write_text("worker bytes\n", encoding="utf-8")

        with pytest.raises((AutodevError, UnsafeRepositoryPathError), match="symlink"):
            _replay_attempt(attempt, repo)
        assert not (external / "f.txt").exists(), (
            "replay bytes must never land outside the main repo"
        )
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(attempt)],
            cwd=repo,
            check=False,
            capture_output=True,
        )


def test_replay_destination_rejects_absolute_and_traversal_paths(tmp_path: Path) -> None:
    from verdict.autodev_run import _create_confined_file, _preflight_replay_destination

    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside.txt"

    for relpath in ("../outside.txt", str(outside), "missing/../../outside.txt"):
        with pytest.raises(UnsafeRepositoryPathError):
            _preflight_replay_destination(repo, relpath)
        with pytest.raises(UnsafeRepositoryPathError):
            _create_confined_file(repo, relpath, b"attacker-controlled")

    assert not outside.exists()
