"""OC2a (#1301): the isolated OpenCode executor for `verdict autodev` units.

Task groups 2 and 3 of ``openspec/changes/autodev-opencode-isolated-executor``. Nothing here calls a
model, a probe or the real ``opencode``; every test runs in a temporary home with git's own global and
system configuration switched off.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import hashlib
import inspect
import os
import shutil
import signal
import stat
import subprocess
import sys
import textwrap
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from verdict import autodev_opencode as oc
from verdict.orchestration.contracts import AttemptUsage, WorkerTerminal
from verdict.orchestration.executor_opencode import OpenCodeExecutor
from verdict.work_unit import WorkUnit

# --------------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("VERDICT_HOME", str(home / ".verdict"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@t")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@t")


def git(repo: Path, *args: str, check: bool = True) -> str:
    done = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=False, env=os.environ
    )
    if check and done.returncode != 0:
        raise AssertionError(f"git {args} failed: {done.stderr}")
    return done.stdout


def write(repo: Path, rel: str, text: str | bytes, *, mode: int | None = None) -> Path:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode() if isinstance(text, str) else text)
    if mode is not None:
        path.chmod(mode)
    return path


def make_repo(tmp_path: Path, files: Mapping[str, str | bytes] | None = None) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    for rel, text in (files or {"a.py": "import os\nx = 1\n", "b.py": "y = 2\n"}).items():
        write(repo, rel, text)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    return repo


def unit(*owned: str, command: tuple[str, ...] = (sys.executable, "-c", "pass")) -> WorkUnit:
    return WorkUnit(
        unit_id="u1",
        objective="repair the owned file",
        owned_files=owned or ("a.py",),
        verification_command=command,
    )


def snapshot(repo: Path) -> dict[str, Any]:
    """The spec's definition of byte-identical for a user repository."""
    files: dict[str, Any] = {}
    dirs: set[str] = set()
    for root, dirnames, filenames in os.walk(repo, followlinks=False):
        rel_root = Path(root).relative_to(repo)
        if rel_root.parts and rel_root.parts[0] == ".git":
            dirnames[:] = []
            continue
        if ".git" in dirnames and not rel_root.parts:
            dirnames.remove(".git")
        for name in dirnames:
            dirs.add(str(rel_root / name))
        for name in filenames:
            path = Path(root) / name
            st = path.lstat()
            rel = str(rel_root / name)
            if stat.S_ISLNK(st.st_mode):
                files[rel] = ("symlink", os.readlink(path))
            elif stat.S_ISREG(st.st_mode):
                files[rel] = ("file", bool(st.st_mode & stat.S_IXUSR), path.read_bytes())
            else:
                files[rel] = ("other",)
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=repo,
        capture_output=True,
        check=True,
        env=env,
    ).stdout
    refs = git(repo, "for-each-ref")
    head = git(repo, "rev-parse", "HEAD")
    git_dir = repo / ".git"
    meta: dict[str, bytes] = {}
    for rel in ("config", "info/exclude"):
        target = git_dir / rel
        meta[rel] = target.read_bytes() if target.exists() else b""
    hooks = git_dir / "hooks"
    if hooks.is_dir():
        for hook in sorted(hooks.iterdir()):
            if hook.is_file():
                meta[f"hooks/{hook.name}"] = hook.read_bytes()
    return {
        "files": files,
        "dirs": dirs,
        "status": status,
        "refs": refs,
        "head": head,
        "meta": meta,
    }


def seed(
    repo: Path, tmp_path: Path, owned: tuple[str, ...] = ("a.py",), name: str = "attempt"
) -> oc.AttemptTree:
    parent = tmp_path / name
    parent.mkdir(mode=0o700)
    return oc.seed_attempt_tree(repo, parent, owned)


# --------------------------------------------------------------------------------------------
# 2.1 seed
# --------------------------------------------------------------------------------------------


class TestSeed:
    def test_clone_shares_nothing_with_the_repository(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        hook = repo / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 0\n")
        hook.chmod(0o755)
        git(repo, "config", "local.marker", "from-the-user")
        with (repo / ".git" / "info" / "exclude").open("a") as handle:
            handle.write("user-only-exclude\n")
        before = snapshot(repo)
        tree = seed(repo, tmp_path)
        gitdir = tree.path / ".git"
        assert not (gitdir / "objects" / "info" / "alternates").exists()
        linked = [
            p for p in (gitdir / "objects").rglob("*") if p.is_file() and p.stat().st_nlink > 1
        ]
        assert linked == []
        assert git(tree.path, "remote").strip() == ""
        assert not (gitdir / "hooks" / "pre-commit").exists()
        assert "from-the-user" not in (gitdir / "config").read_text()
        assert "user-only-exclude" not in (gitdir / "info" / "exclude").read_text()
        assert snapshot(repo) == before

    def test_dirty_state_is_carried_over(self, tmp_path: Path) -> None:
        repo = make_repo(
            tmp_path,
            {
                "mod.py": "m = 1\n",
                "gone.py": "g = 1\n",
                "staged.py": "s = 1\n",
                ".gitignore": "ign.txt\n",
            },
        )
        write(repo, "mod.py", "m = 2\n")
        (repo / "gone.py").unlink()
        write(repo, "staged.py", "s = 2\n")
        git(repo, "add", "staged.py")
        write(repo, "new dir/untracked file.py", "u = 1\n")
        write(repo, "ign.txt", "ignored\n")
        before = snapshot(repo)
        tree = seed(repo, tmp_path, ("mod.py",))
        assert (tree.path / "mod.py").read_text() == "m = 2\n"
        assert (tree.path / "staged.py").read_text() == "s = 2\n"
        assert (tree.path / "new dir" / "untracked file.py").read_text() == "u = 1\n"
        assert not (tree.path / "gone.py").exists()
        assert not (tree.path / "ign.txt").exists()
        assert snapshot(repo) == before

    def test_staged_rename(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path, {"old.py": "o = 1\n", "keep.py": "k = 1\n"})
        git(repo, "mv", "old.py", "new.py")
        before = snapshot(repo)
        tree = seed(repo, tmp_path, ("keep.py",))
        assert (tree.path / "new.py").read_text() == "o = 1\n"
        assert not (tree.path / "old.py").exists()
        assert "R" in git(repo, "status", "--porcelain")
        assert snapshot(repo) == before

    def test_staged_rename_whose_destination_was_then_modified(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path, {"old.py": "o = 1\n", "keep.py": "k = 1\n"})
        git(repo, "mv", "old.py", "new.py")
        write(repo, "new.py", "o = 99\n")
        before = snapshot(repo)
        tree = seed(repo, tmp_path, ("keep.py",))
        assert (tree.path / "new.py").read_text() == "o = 99\n"
        assert not (tree.path / "old.py").exists()
        assert snapshot(repo) == before

    def test_owned_files_hold_working_tree_bytes(self, tmp_path: Path) -> None:
        # A clean filter drops the trailing note, so a same-size edit of the note leaves git status
        # clean while the working-tree bytes differ from the blob (and from what a clone checks out).
        repo = tmp_path / "repo"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        git(repo, "config", "filter.strip.clean", "sed 's/  # note.*//'")
        write(repo, ".gitattributes", "a.txt filter=strip\n")
        write(repo, "a.txt", "x = 1  # note A\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "init")
        write(repo, "a.txt", "x = 1  # note B\n")
        assert git(repo, "status", "--porcelain") == ""  # so it cannot pass for the wrong reason
        assert git(repo, "cat-file", "-p", "HEAD:a.txt") == "x = 1\n"
        tree = seed(repo, tmp_path, ("a.txt",))
        assert (tree.path / "a.txt").read_bytes() == b"x = 1  # note B\n"
        assert tree.manifest["a.txt"].digest == hashlib.sha256(b"x = 1  # note B\n").hexdigest()

    def test_status_record_with_a_space_is_one_path(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path, {"with space.py": "w = 1\n", "keep.py": "k = 1\n"})
        git(repo, "mv", "with space.py", "still spaced.py")
        tree = seed(repo, tmp_path, ("keep.py",))
        assert (tree.path / "still spaced.py").exists()
        assert not (tree.path / "with space.py").exists()

    def test_symlinks_are_copied_as_symlinks(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        outside = tmp_path / "outside.txt"
        outside.write_text("secret\n")
        (repo / "link.txt").symlink_to(outside)
        tree = seed(repo, tmp_path)
        link = tree.path / "link.txt"
        assert link.is_symlink()
        assert os.readlink(link) == str(outside)
        assert tree.manifest["link.txt"].kind == "symlink"

    @pytest.mark.parametrize(
        "case", ["no_head", "submodule", "nested_repository", "limit", "dot_git"]
    )
    def test_seed_refused_before_any_worker_starts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
    ) -> None:
        owned: tuple[str, ...] = ("a.py",)
        if case == "no_head":
            repo = tmp_path / "repo"
            repo.mkdir()
            git(repo, "init", "-q", "-b", "main")
            write(repo, "a.py", "x = 1\n")
            expect = "no HEAD"
        else:
            repo = make_repo(tmp_path)
            expect = ""
        if case == "submodule":
            sub = tmp_path / "subsrc"
            sub.mkdir()
            git(sub, "init", "-q", "-b", "main")
            write(sub, "s.py", "s = 1\n")
            git(sub, "add", "-A")
            git(sub, "commit", "-qm", "s")
            git(
                repo,
                "-c",
                "protocol.file.allow=always",
                "submodule",
                "add",
                "-q",
                str(sub),
                "vendored",
            )
            git(repo, "commit", "-qm", "submodule")
            expect = "submodule"
        elif case == "nested_repository":
            nested = repo / "nested"
            nested.mkdir()
            git(nested, "init", "-q", "-b", "main")
            write(nested, "n.py", "n = 1\n")
            expect = "nested repository"
        elif case == "limit":
            monkeypatch.setattr(oc, "MAX_SEED_FILES", 1)
            expect = "limit"
        elif case == "dot_git":
            owned = (".git/config",)
            expect = "unsafe"
        before = snapshot(repo) if case != "no_head" else None
        parent = tmp_path / "attempt"
        parent.mkdir(mode=0o700)
        with pytest.raises(oc.AttemptFailedError) as caught:
            oc.seed_attempt_tree(repo, parent, owned)
        assert expect in caught.value.reason
        assert list(parent.iterdir()) == []
        if before is not None:
            assert snapshot(repo) == before

    @pytest.mark.parametrize("victim", ["file", "directory"])
    def test_removal_does_not_follow_a_symlinked_ancestor(
        self, tmp_path: Path, victim: str
    ) -> None:
        """Review finding 3: the clone holds a committed symlink ``d``; the user replaced it by a real
        directory and the unit owns the absent ``d/victim``. The removal pass must not unlink the
        outside ``victim`` that ``d`` points at."""
        external = tmp_path / "external"
        external.mkdir()
        if victim == "file":
            (external / "victim").write_text("keep me\n")
        else:
            (external / "victim").mkdir()
            (external / "victim" / "inner.txt").write_text("keep me\n")
        repo = make_repo(tmp_path)
        (repo / "d").symlink_to(external)
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "commit d as a symlink")
        (repo / "d").unlink()
        write(
            repo, "d/keep.txt", "the user's file\n"
        )  # d is now a real directory in the user's tree
        before = snapshot(repo)
        tree = seed(repo, tmp_path, ("d/victim",))
        survivor = external / "victim" if victim == "file" else external / "victim" / "inner.txt"
        assert survivor.read_text() == "keep me\n"
        assert snapshot(repo) == before
        assert (tree.path / "d").is_dir() and not (tree.path / "d").is_symlink()
        assert (tree.path / "d" / "keep.txt").read_text() == "the user's file\n"
        assert not (tree.path / "d" / "victim").exists()

    def test_copy_does_not_write_through_a_symlinked_ancestor(self, tmp_path: Path) -> None:
        """The overlay of ``d/new.txt`` replaces the clone's committed symlink ``d`` by a directory."""
        external = tmp_path / "external"
        external.mkdir()
        repo = make_repo(tmp_path)
        (repo / "d").symlink_to(external)
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "commit d as a symlink")
        (repo / "d").unlink()
        write(repo, "d/new.txt", "the user's file\n")
        tree = seed(repo, tmp_path)
        assert list(external.iterdir()) == []
        assert (tree.path / "d" / "new.txt").read_text() == "the user's file\n"
        assert not (tree.path / "d").is_symlink()

    def test_removal_of_a_directory(self, tmp_path: Path) -> None:
        """A path the unit owns that is a directory in the clone and absent in the user's tree goes."""
        repo = make_repo(
            tmp_path, {"a.py": "a = 1\n", "pkg/x.py": "x = 1\n", "pkg/sub/y.py": "y = 1\n"}
        )
        shutil.rmtree(repo / "pkg")
        tree = seed(repo, tmp_path, ("pkg",))
        assert not (tree.path / "pkg").exists()
        assert (tree.path / "a.py").exists()


# --------------------------------------------------------------------------------------------
# 2.2 delta
# --------------------------------------------------------------------------------------------


def delta(
    tree: oc.AttemptTree,
    repo: Path,
    owned: tuple[str, ...] = ("a.py",),
    hooks: Mapping[str, Callable[..., None]] | None = None,
) -> oc.Delta:
    return oc.attempt_delta(tree, repo, unit(*owned), hooks=hooks)


class TestDelta:
    def test_planted_git_configuration_is_inert(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        tree = seed(repo, tmp_path)
        marker = tmp_path / "marker"
        script = tmp_path / "plant.sh"
        script.write_text(f"#!/bin/sh\ntouch {marker}\n")
        script.chmod(0o755)
        with (tree.path / ".git" / "config").open("a") as handle:
            handle.write(f"[core]\n\tfsmonitor = {script}\n")
        hook = tree.path / ".git" / "hooks" / "pre-commit"
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
        hook.chmod(0o755)
        write(tree.path, "a.py", "x = 2\n")
        result = delta(tree, repo)
        assert [item.path for item in result.items] == ["a.py"]
        assert not marker.exists()

    def test_git_state_does_not_change_the_result(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        tree = seed(repo, tmp_path)
        write(tree.path, "a.py", "x = 2\n")
        assert [i.path for i in delta(tree, repo).items] == ["a.py"]
        git(tree.path, "add", "a.py")
        assert [i.path for i in delta(tree, repo).items] == ["a.py"]
        git(tree.path, "commit", "-qm", "worker commit", "--no-verify")
        assert [i.path for i in delta(tree, repo).items] == ["a.py"]
        write(tree.path, "a.py", "import os\nx = 1\n")  # reverted
        with pytest.raises(oc.AttemptRefusedError) as caught:
            delta(tree, repo)
        assert "no change" in caught.value.reason

    def test_a_new_owned_file_in_a_new_directory(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        tree = seed(repo, tmp_path, ("newdir/file.py",))
        write(tree.path, "newdir/file.py", "n = 1\n")
        result = delta(tree, repo, ("newdir/file.py",))
        assert [i.path for i in result.items] == ["newdir/file.py"]
        assert result.items[0].data == b"n = 1\n"

    def test_directory_and_file_swapped(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path, {"d/inner.py": "i = 1\n", "a.py": "a = 1\n"})
        tree = seed(repo, tmp_path, ("d/inner.py",))
        (tree.path / "d" / "inner.py").unlink()
        (tree.path / "d").rmdir()
        write(tree.path, "d", "now a file\n")
        with pytest.raises(oc.AttemptRefusedError) as caught:
            delta(tree, repo, ("d/inner.py",))
        assert "d" in caught.value.reason.replace("d/inner.py", "")

    def test_special_files_in_the_attempt_tree(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        tree = seed(repo, tmp_path)
        (tree.path / "a.py").unlink()
        os.mkfifo(tree.path / "a.py")
        with pytest.raises(oc.AttemptRefusedError) as caught:
            delta(tree, repo)
        assert "a.py" in caught.value.reason
        (tree.path / "a.py").unlink()
        (tree.path / "a.py").symlink_to("/etc/hostname")
        with pytest.raises(oc.AttemptRefusedError) as caught2:
            delta(tree, repo)
        assert "a.py" in caught2.value.reason

    @pytest.mark.parametrize("seam", ["before_open", "after_metadata"])
    def test_a_regular_file_replaced_by_a_fifo_during_validation(
        self, tmp_path: Path, seam: str
    ) -> None:
        repo = make_repo(tmp_path)
        tree = seed(repo, tmp_path)
        write(tree.path, "a.py", "x = 2\n")
        swapped: list[str] = []

        def swap(path: str) -> None:
            if path == "a.py" and not swapped:
                swapped.append(path)
                (tree.path / "a.py").unlink()
                os.mkfifo(tree.path / "a.py")

        class Hang(BaseException):  # not an OSError, so no handler under test can swallow it
            pass

        def hang_guard(_signum: int, _frame: Any) -> None:
            raise Hang("the executor blocked opening a FIFO")

        previous = signal.signal(signal.SIGALRM, hang_guard)
        signal.alarm(15)
        try:
            with pytest.raises(oc.AttemptFailedError) as caught:
                delta(tree, repo, hooks={seam: swap})
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)
        assert "changed during validation" in caught.value.reason

    def test_ancestor_directory_swapped_during_validation(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path, {"d/inner.py": "i = 1\n", "a.py": "a = 1\n"})
        tree = seed(repo, tmp_path, ("d/inner.py",))
        write(tree.path, "d/inner.py", "i = 2\n")
        outside = tmp_path / "outside"
        outside.mkdir()
        # Identical bytes, so only refusing to follow the link (not a digest mismatch) catches it.
        (outside / "inner.py").write_text("i = 2\n")
        calls: list[str] = []

        def swap(path: str) -> None:
            # The first call measures d/inner.py; the second is the read of the delta.
            calls.append(path)
            if path == "d/inner.py" and calls.count(path) == 2:
                (tree.path / "d").rename(tmp_path / "moved-away")
                (tree.path / "d").symlink_to(outside)

        with pytest.raises(oc.AttemptFailedError) as caught:
            delta(tree, repo, ("d/inner.py",), hooks={"before_open": swap})
        assert "changed during validation" in caught.value.reason

    def test_a_worker_cannot_hide_a_path_by_editing_gitignore(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        tree = seed(repo, tmp_path)
        write(tree.path, "a.py", "x = 2\n")
        write(tree.path, "sneaky.txt", "x\n")
        write(tree.path, ".gitignore", "sneaky.txt\n")
        with pytest.raises(oc.AttemptRefusedError) as caught:
            delta(tree, repo)
        assert "sneaky.txt" in caught.value.reason
        assert ".gitignore" in caught.value.reason

    def test_ignored_byproduct(self, tmp_path: Path) -> None:
        repo = make_repo(
            tmp_path, {"a.py": "x = 1\n", ".gitignore": "__pycache__/\n.pytest_cache/\n"}
        )
        tree = seed(repo, tmp_path)
        write(tree.path, "a.py", "x = 2\n")
        write(tree.path, "__pycache__/a.cpython-313.pyc", b"\x00\x01")
        write(tree.path, ".pytest_cache/v/cache/lastfailed", "{}")
        result = delta(tree, repo)
        assert [i.path for i in result.items] == ["a.py"]

    def test_ignored_owned_new_file(self, tmp_path: Path) -> None:
        """Carried finding 1, single file: an owned new file the ignore rules match is kept."""
        repo = make_repo(tmp_path, {"a.py": "x = 1\n", ".gitignore": "generated_*.py\n"})
        tree = seed(repo, tmp_path, ("generated_out.py",))
        write(tree.path, "generated_out.py", "g = 1\n")
        result = delta(tree, repo, ("generated_out.py",))
        assert [i.path for i in result.items] == ["generated_out.py"]

    def test_ignored_and_plain_owned_new_files(self, tmp_path: Path) -> None:
        """Carried finding 1, mixed repair: both owned new files are in the delta."""
        repo = make_repo(tmp_path, {"a.py": "x = 1\n", ".gitignore": "generated_*.py\n"})
        owned = ("generated_out.py", "plain_out.py")
        tree = seed(repo, tmp_path, owned)
        write(tree.path, "generated_out.py", "g = 1\n")
        write(tree.path, "plain_out.py", "p = 1\n")
        result = delta(tree, repo, owned)
        assert [i.path for i in result.items] == ["generated_out.py", "plain_out.py"]

    def test_ignored_path_present_at_seed_is_not_a_byproduct(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path, {"a.py": "x = 1\n", "forced.log": "one\n"})
        (repo / ".gitignore").write_text("*.log\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "ignore logs")
        tree = seed(repo, tmp_path)
        write(tree.path, "forced.log", "two\n")
        with pytest.raises(oc.AttemptRefusedError) as caught:
            delta(tree, repo)
        assert "forced.log" in caught.value.reason

    def test_owned_delete_is_in_the_delta(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        tree = seed(repo, tmp_path)
        (tree.path / "a.py").unlink()
        result = delta(tree, repo)
        assert [(i.path, i.kind) for i in result.items] == [("a.py", "delete")]

    def test_mode_only_change_is_in_the_delta(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        tree = seed(repo, tmp_path)
        (tree.path / "a.py").chmod(0o755)
        result = delta(tree, repo)
        assert [(i.path, i.executable) for i in result.items] == [("a.py", True)]

    def test_symlink_in_an_owned_path_is_rejected(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        tree = seed(repo, tmp_path)
        (tree.path / "a.py").unlink()
        (tree.path / "a.py").symlink_to("b.py")
        with pytest.raises(oc.AttemptRefusedError) as caught:
            delta(tree, repo)
        assert "a.py" in caught.value.reason
        assert "regular file" in caught.value.reason

    def test_delta_hash_mismatch_is_error(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        tree = seed(repo, tmp_path)
        write(tree.path, "a.py", "x = 2\n")
        seen: list[str] = []

        def rewrite(path: str) -> None:
            # a.py is opened once to measure and once to read: change it in between.
            seen.append(path)
            if path == "a.py" and seen.count(path) == 2:
                (tree.path / "a.py").write_text("x = 3\n")

        with pytest.raises(oc.AttemptFailedError) as caught:
            delta(tree, repo, hooks={"before_open": rewrite})
        assert "changed during validation" in caught.value.reason

    def test_file_over_the_limit_is_a_named_refusal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = make_repo(tmp_path)
        tree = seed(repo, tmp_path)
        monkeypatch.setattr(oc, "MAX_TRANSFER_FILE_BYTES", 8)
        write(tree.path, "a.py", "x = 123456789\n")
        with pytest.raises(oc.AttemptRefusedError) as caught:
            delta(tree, repo)
        assert "a.py" in caught.value.reason
        assert "limit" in caught.value.reason

    def test_tree_over_the_limit_is_a_named_refusal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = make_repo(tmp_path)
        tree = seed(repo, tmp_path)
        write(tree.path, "a.py", "x = 2\n")
        for index in range(5):
            write(tree.path, f"extra{index}.txt", "x\n")
        monkeypatch.setattr(oc, "MAX_SEED_FILES", 3)
        with pytest.raises(oc.AttemptRefusedError) as caught:
            delta(tree, repo)
        assert "limit" in caught.value.reason

    @pytest.mark.parametrize(
        "sizes",
        [
            pytest.param([1024 * 1024], id="one-huge-file"),
            pytest.param([3000, 3000, 3000], id="sum"),
        ],
    )
    def test_manifest_hashing_stops_at_the_byte_budget(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sizes: list[int]
    ) -> None:
        """Review finding 6: a worker-made file must not be read past the tree's byte budget."""
        repo = make_repo(tmp_path)
        tree = seed(repo, tmp_path)
        write(tree.path, "a.py", "x = 2\n")
        for index, size in enumerate(sizes):
            write(tree.path, f"big{index}.bin", b"x" * size)
        monkeypatch.setattr(oc, "MAX_SEED_BYTES", 4096)
        monkeypatch.setattr(oc, "_READ_CHUNK", 1024)
        read = [0]
        real_read = os.read

        def counting(fd: int, count: int) -> bytes:
            data = real_read(fd, count)
            read[0] += len(data)
            return data

        monkeypatch.setattr(os, "read", counting)
        with pytest.raises(oc.AttemptRefusedError) as caught:
            delta(tree, repo)
        monkeypatch.setattr(os, "read", real_read)
        assert "limit" in caught.value.reason and "bytes" in caught.value.reason
        assert read[0] <= 4096 + 1024  # the budget plus the one chunk that crossed it

    def test_seed_walk_stops_at_the_byte_budget(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same bound holds for the manifest taken at the end of the seed."""
        repo = make_repo(tmp_path, {"a.py": "a = 1\n", "huge.bin": b"x" * (1024 * 1024)})
        monkeypatch.setattr(oc, "MAX_SEED_BYTES", 4096)
        monkeypatch.setattr(oc, "_READ_CHUNK", 1024)
        read = [0]
        real_read = os.read

        def counting(fd: int, count: int) -> bytes:
            data = real_read(fd, count)
            read[0] += len(data)
            return data

        monkeypatch.setattr(os, "read", counting)
        with pytest.raises(oc.AttemptFailedError) as caught:
            seed(repo, tmp_path)
        monkeypatch.setattr(os, "read", real_read)
        assert "limit" in caught.value.reason and "bytes" in caught.value.reason
        assert read[0] <= 4096 + 1024

    def test_git_only_changes_to_unowned_files_are_not_a_change(self, tmp_path: Path) -> None:
        """Review finding 2 (documented limit): the delta compares working-tree contents, so a commit or
        an index-only edit that leaves an unowned file's bytes as seeded is not seen, and reaches nothing."""
        repo = make_repo(tmp_path)
        tree = seed(repo, tmp_path)
        write(tree.path, "a.py", "x = 2\n")  # the owned edit
        git(tree.path, "commit", "-q", "--allow-empty", "-m", "empty worker commit", "--no-verify")
        write(tree.path, "b.py", "staged only\n")
        git(tree.path, "add", "b.py")  # an index-only edit to an unowned file ...
        write(tree.path, "b.py", "y = 2\n")  # ... with the working tree put back
        git(
            tree.path, "commit", "-q", "-m", "unowned edit, committed", "--no-verify"
        )  # HEAD: "staged only"
        assert [item.path for item in delta(tree, repo).items] == ["a.py"]


# --------------------------------------------------------------------------------------------
# 2.3 transfer, 2.4 restore
# --------------------------------------------------------------------------------------------


def witem(path: str, text: str, *, executable: bool = False) -> oc.DeltaItem:
    data = text.encode()
    return oc.DeltaItem(path, "write", data, executable, hashlib.sha256(data).hexdigest())


def ditem(path: str) -> oc.DeltaItem:
    return oc.DeltaItem(path, "delete")


def user_manifest(repo: Path) -> dict[str, oc.Entry]:
    return oc.walk_manifest(repo, limit_error=lambda reason: AssertionError(reason))


def run_transfer(
    repo: Path,
    items: list[oc.DeltaItem],
    m0: Mapping[str, oc.Entry] | None = None,
    journal: list[oc.JournalEntry] | None = None,
    hooks: Mapping[str, Callable[..., None]] | None = None,
) -> list[oc.JournalEntry]:
    journal = [] if journal is None else journal
    oc.transfer(
        oc.Delta(tuple(items)), repo, user_manifest(repo) if m0 is None else m0, journal, hooks
    )
    return journal


class TestTransfer:
    def test_owned_file_changed_after_the_seed(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        m0 = user_manifest(repo)
        write(repo, "a.py", "user edit\n")
        before = snapshot(repo)
        with pytest.raises(oc.AttemptRefusedError) as caught:
            run_transfer(repo, [witem("a.py", "worker\n")], m0)
        assert "changed since the seed" in caught.value.reason
        assert snapshot(repo) == before

    def test_file_appeared_after_the_seed(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        m0 = user_manifest(repo)
        write(repo, "fresh.py", "user file\n")
        before = snapshot(repo)
        with pytest.raises(oc.AttemptRefusedError) as caught:
            run_transfer(repo, [witem("fresh.py", "worker\n")], m0)
        assert "appeared after the seed" in caught.value.reason
        assert snapshot(repo) == before

    @pytest.mark.parametrize("kind", ["fifo", "directory"])
    def test_non_regular_file_in_the_user_s_tree(self, tmp_path: Path, kind: str) -> None:
        repo = make_repo(tmp_path)
        m0 = user_manifest(repo)
        if kind == "fifo":
            os.mkfifo(repo / "special")
        else:
            (repo / "special").mkdir()
        before = snapshot(repo)
        with pytest.raises(oc.AttemptRefusedError) as caught:
            run_transfer(repo, [witem("special", "worker\n")], m0)
        assert "not a regular file" in caught.value.reason
        assert snapshot(repo) == before

    def test_edit_between_the_checks_and_the_first_write(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        m0 = user_manifest(repo)

        def edit() -> None:
            write(repo, "a.py", "user edit after the checks\n")

        with pytest.raises(oc.AttemptRefusedError):
            run_transfer(repo, [witem("a.py", "worker\n")], m0, hooks={"after_checks": edit})
        assert (repo / "a.py").read_text() == "user edit after the checks\n"
        assert not [p for p in repo.rglob(".verdict-transfer-*")]

    def test_only_the_mode_changed(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        m0 = user_manifest(repo)
        (repo / "a.py").chmod(0o755)
        with pytest.raises(oc.AttemptRefusedError) as caught:
            run_transfer(repo, [witem("a.py", "worker\n")], m0)
        assert "executable bit" in caught.value.reason
        assert (repo / "a.py").stat().st_mode & 0o111

    @pytest.mark.parametrize(
        "bad", ["../escape.py", "/tmp/escape-abs.py", ".git/hooks/pre-commit", "a/../b.py"]
    )
    def test_path_that_escapes_the_root_is_refused(self, tmp_path: Path, bad: str) -> None:
        repo = make_repo(tmp_path)
        before = snapshot(repo)
        with pytest.raises(oc.AttemptRefusedError) as caught:
            run_transfer(repo, [witem(bad, "evil\n")], {})
        assert "not safe" in caught.value.reason
        assert not (tmp_path / "escape.py").exists()
        assert snapshot(repo) == before

    def test_user_file_over_the_limit_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = make_repo(tmp_path)
        m0 = user_manifest(repo)
        monkeypatch.setattr(oc, "MAX_TRANSFER_FILE_BYTES", 4)
        with pytest.raises(oc.AttemptRefusedError) as caught:
            run_transfer(repo, [witem("a.py", "z\n")], m0)
        assert "limit" in caught.value.reason

    def test_symlinked_parent(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        (repo / "d").symlink_to(outside)
        with pytest.raises(oc.AttemptRefusedError):
            run_transfer(repo, [witem("d/new.py", "worker\n")], {})
        assert list(outside.iterdir()) == []

    def test_parent_swapped_for_a_symlink_during_the_transfer(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path, {"d/inner.py": "i = 1\n", "a.py": "a = 1\n"})
        m0 = user_manifest(repo)
        outside = tmp_path / "outside"
        outside.mkdir()

        def swap() -> None:
            (repo / "d").rename(tmp_path / "d-moved")
            (repo / "d").symlink_to(outside)

        with pytest.raises(oc.AttemptRefusedError):
            run_transfer(repo, [witem("d/inner.py", "worker\n")], m0, hooks={"after_checks": swap})
        assert list(outside.iterdir()) == []
        assert (tmp_path / "d-moved" / "inner.py").read_text() == "i = 1\n"

    @pytest.mark.parametrize("second", [False, True])
    def test_parent_directory_renamed_away_during_the_transfer(
        self, tmp_path: Path, second: bool
    ) -> None:
        repo = make_repo(tmp_path, {"d1/x.py": "x = 1\n", "d2/y.py": "y = 1\n", "a.py": "a = 1\n"})
        m0 = user_manifest(repo)
        items = [witem("d1/x.py", "w1\n"), witem("d2/y.py", "w2\n")]
        target = "d2/y.py" if second else "d1/x.py"

        def displace(path: str) -> None:
            if path == target:
                parent = repo / path.split("/")[0]
                parent.rename(tmp_path / "displaced")
                parent.mkdir()
                (parent / path.split("/")[1]).write_text("a different directory\n")

        journal: list[oc.JournalEntry] = []
        expected = oc.TransferFailedError if second else oc.AttemptRefusedError
        with pytest.raises(expected):
            run_transfer(repo, items, m0, journal, hooks={"before_write": displace})
        assert (tmp_path / "displaced" / ("y.py" if second else "x.py")).read_text() in (
            "y = 1\n",
            "x = 1\n",
        )
        if second:  # the first path was written and then rolled back
            assert (repo / "d1" / "x.py").read_text() == "x = 1\n"

    def test_a_refusal_on_a_later_path_leaves_no_directory_behind(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        m0 = user_manifest(repo)
        write(repo, "b.py", "user edit\n")
        before = snapshot(repo)
        with pytest.raises(oc.AttemptRefusedError):
            run_transfer(repo, [witem("adir/x.py", "w\n"), witem("b.py", "w\n")], m0)
        assert snapshot(repo) == before
        assert not (repo / "adir").exists()

    def test_late_conflict_after_earlier_paths_were_written(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path, {"a.py": "a = 1\n", "b.py": "b = 1\n", "c.py": "c = 1\n"})
        m0 = user_manifest(repo)

        def edit(path: str) -> None:
            if path == "b.py":
                write(repo, "b.py", "user edit\n")

        items = [witem("a.py", "w\n"), witem("b.py", "w\n"), witem("c.py", "w\n")]
        with pytest.raises(oc.TransferFailedError) as caught:
            run_transfer(repo, items, m0, hooks={"before_item": edit})
        assert "b.py" in caught.value.reason
        assert (repo / "a.py").read_text() == "a = 1\n"
        assert (repo / "b.py").read_text() == "user edit\n"
        assert (repo / "c.py").read_text() == "c = 1\n"

    def test_rollback_blocked_by_a_later_user_edit(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path, {"a.py": "a = 1\n", "b.py": "b = 1\n"})
        m0 = user_manifest(repo)

        def edit(path: str) -> None:
            if path == "b.py":
                write(repo, "a.py", "user edit of the written file\n")
                write(repo, "b.py", "user edit\n")

        with pytest.raises(oc.TransferFailedError) as caught:
            run_transfer(
                repo, [witem("a.py", "w\n"), witem("b.py", "w\n")], m0, hooks={"before_item": edit}
            )
        assert (repo / "a.py").read_text() == "user edit of the written file\n"
        assert "not restored" in caught.value.reason
        assert "a.py" in caught.value.reason

    def test_failure_part_way(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = make_repo(tmp_path, {"a.py": "a = 1\n", "b.py": "b = 1\n", "c.py": "c = 1\n"})
        m0 = user_manifest(repo)
        before = snapshot(repo)
        real = oc.replace_file
        calls: list[str] = []

        def flaky(fd: int, name: str, data: bytes, mode: int, **kwargs: Any) -> None:
            calls.append(name)
            if len(calls) == 2:
                raise OSError(errno.EIO, "disk")
            real(fd, name, data, mode, **kwargs)

        monkeypatch.setattr(oc, "replace_file", flaky)
        items = [witem("a.py", "w\n"), witem("b.py", "w\n"), witem("c.py", "w\n")]
        with pytest.raises(oc.TransferFailedError):
            run_transfer(repo, items, m0)
        assert snapshot(repo) == before

    @pytest.mark.parametrize("failing", ["write", "fchmod", "rename"])
    @pytest.mark.parametrize("existing", [True, False])
    def test_temporary_file_removed_after_a_failed_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: str, existing: bool
    ) -> None:
        repo = make_repo(tmp_path)
        m0 = user_manifest(repo)
        before = snapshot(repo)
        real = getattr(os, failing)

        def boom(*args: Any, **kwargs: Any) -> Any:
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(os, failing, boom)
        target = "a.py" if existing else "newdir/new.py"
        try:
            with pytest.raises(oc.AttemptFailedError) as caught:
                run_transfer(repo, [witem(target, "worker\n")], m0)
        finally:
            monkeypatch.setattr(os, failing, real)
        assert "ENOSPC" in caught.value.reason or "No space" in caught.value.reason
        assert not list(repo.rglob(".verdict-transfer-*"))
        assert snapshot(repo) == before

    def test_temporary_file_removal_fails_too(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = make_repo(tmp_path)
        m0 = user_manifest(repo)
        real_unlink = os.unlink

        def failing_rename(*args: Any, **kwargs: Any) -> Any:
            raise OSError(errno.ENOSPC, "No space left on device")

        def failing_unlink(path: Any, *args: Any, **kwargs: Any) -> Any:
            if str(path).startswith(".verdict-transfer-"):
                raise OSError(errno.EACCES, "Permission denied")
            return real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(os, "rename", failing_rename)
        monkeypatch.setattr(os, "unlink", failing_unlink)
        try:
            with pytest.raises(oc.AttemptFailedError) as caught:
                run_transfer(repo, [witem("a.py", "worker\n")], m0)
        finally:
            monkeypatch.setattr(os, "rename", os.rename)
        reason = caught.value.reason
        assert "No space left" in reason
        assert reason.index("No space left") < reason.index(".verdict-transfer-")
        assert "could not be removed" in reason


CHILD = textwrap.dedent(
    """
    import hashlib, json, sys
    from pathlib import Path
    from verdict import autodev_opencode as oc

    repo, phase, count = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
    items = []
    for path, text in json.loads(sys.argv[4]):
        data = text.encode()
        items.append(oc.DeltaItem(path, "write", data, False, hashlib.sha256(data).hexdigest()))
    m0 = oc.walk_manifest(repo, limit_error=lambda reason: RuntimeError(reason))
    seen = {"n": 0}

    def block(*_args):
        seen["n"] += 1
        if seen["n"] == count:
            print("ready", flush=True)
            sys.stdin.readline()  # blocks until the test kills this process

    oc.transfer(oc.Delta(tuple(items)), repo, m0, [], {phase: block})
    """
)


def kill_during(repo: Path, phase: str, count: int, items: list[tuple[str, str]]) -> None:
    import json
    import signal

    proc = subprocess.Popen(
        [sys.executable, "-I", "-c", CHILD, str(repo), phase, str(count), json.dumps(items)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env=os.environ,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "ready"
        proc.send_signal(signal.SIGKILL)
    finally:
        proc.wait(timeout=30)


class TestCrash:
    def test_kill_after_the_temporary_write(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        kill_during(
            repo, "after_temp_write", 1, [("a.py", "worker one\n"), ("b.py", "worker two\n")]
        )
        assert (repo / "a.py").read_text() == "import os\nx = 1\n"
        assert (repo / "b.py").read_text() == "y = 2\n"
        temps = list(repo.glob(".verdict-transfer-*"))
        assert len(temps) <= 1
        listed = git(repo, "status", "--porcelain", "--untracked-files=all").splitlines()
        assert [line for line in listed if ".verdict-transfer-" not in line] == []

    def test_kill_after_the_first_of_two_renames(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        before = snapshot(repo)
        kill_during(repo, "after_item", 1, [("a.py", "worker one\n"), ("b.py", "worker two\n")])
        assert (repo / "a.py").read_text() == "worker one\n"
        assert (repo / "b.py").read_text() == "y = 2\n"
        git(repo, "checkout", "--", "a.py", "b.py")
        for temp in repo.glob(".verdict-transfer-*"):
            temp.unlink()
        assert snapshot(repo) == before

    def test_kill_after_the_last_rename(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        before = snapshot(repo)
        kill_during(
            repo, "after_item", 2, [("a.py", "worker one\n"), ("newdir/new.py", "created\n")]
        )
        assert (repo / "a.py").read_text() == "worker one\n"
        assert (repo / "newdir" / "new.py").read_text() == "created\n"
        git(repo, "checkout", "--", "a.py")
        (repo / "newdir" / "new.py").unlink()
        (repo / "newdir").rmdir()
        assert snapshot(repo) == before


def transferred(
    repo: Path, items: list[oc.DeltaItem], hooks: Mapping[str, Callable[..., None]] | None = None
) -> list[oc.JournalEntry]:
    return run_transfer(repo, items, None, None, hooks)


def interrupt_case(which: str) -> tuple[list[oc.DeltaItem], Callable[[tuple[Any, ...]], bool]]:
    """The delta and the call to interrupt: the rename over a.py, its deletion, or mkdir newdir."""
    if which == "rename":
        return [witem("a.py", "worker\n")], lambda args: len(args) > 1 and args[1] == "a.py"
    if which == "unlink":
        return [ditem("a.py")], lambda args: bool(args) and args[0] == "a.py"
    return [witem("newdir/new.py", "created\n")], lambda args: bool(args) and args[0] == "newdir"


class TestRestore:
    def test_created_files_and_directories_are_removed(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        before = snapshot(repo)
        journal = transferred(repo, [witem("deep/er/new.py", "created\n")])
        assert (repo / "deep" / "er" / "new.py").exists()
        result = oc.restore_transfer(journal, repo)
        assert not (repo / "deep").exists()
        assert result.not_restored == []
        assert snapshot(repo) == before

    def test_created_directory_replaced(self, tmp_path: Path) -> None:
        """Carried finding 2: a directory the transfer created, removed and replaced, is not removed."""
        repo = make_repo(tmp_path)
        journal = transferred(repo, [witem("newdir/new.py", "created\n")])
        directory = repo / "newdir"
        # Swap in a replacement while the original inode is still held, so a reused inode number
        # cannot hide the swap; then remove the original.
        directory.rename(repo / "newdir-original")
        directory.mkdir()
        (repo / "newdir-original" / "new.py").unlink()
        (repo / "newdir-original").rmdir()
        result = oc.restore_transfer(journal, repo)
        assert directory.is_dir() and list(directory.iterdir()) == []
        assert ("newdir", "replaced by another directory") in result.not_restored
        assert "newdir" in result.summary()

    def test_user_edit_after_the_transfer_is_kept(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        journal = transferred(repo, [witem("a.py", "worker\n")])
        write(repo, "a.py", "user edit after the transfer\n")
        result = oc.restore_transfer(journal, repo)
        assert (repo / "a.py").read_text() == "user edit after the transfer\n"
        assert result.not_restored == [("a.py", "changed since the transfer")]

    def test_user_mode_change_after_the_transfer_is_kept(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        journal = transferred(repo, [witem("a.py", "worker\n")])
        (repo / "a.py").chmod(0o755)
        result = oc.restore_transfer(journal, repo)
        assert (repo / "a.py").stat().st_mode & stat.S_IXUSR
        assert [path for path, _ in result.not_restored] == ["a.py"]

    def test_parent_swapped_between_transfer_and_restore(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path, {"d/inner.py": "i = 1\n", "a.py": "a = 1\n"})
        journal = transferred(repo, [witem("d/inner.py", "worker\n")])
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "inner.py").write_text("outside content\n")
        (repo / "d").rename(tmp_path / "d-moved")
        (repo / "d").symlink_to(outside)
        result = oc.restore_transfer(journal, repo)
        assert (outside / "inner.py").read_text() == "outside content\n"
        assert [path for path, _ in result.not_restored] == ["d/inner.py"]

    def test_restore_twice(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        before = snapshot(repo)
        journal = transferred(repo, [witem("a.py", "worker\n")])
        first = oc.restore_transfer(journal, repo)
        second = oc.restore_transfer(journal, repo)
        assert first.restored == ["a.py"]
        assert second.restored == [] and second.not_restored == [] and second.summary() == ""
        assert snapshot(repo) == before

    def test_restore_without_a_transfer(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        before = snapshot(repo)
        result = oc.restore_transfer([], repo)
        assert result.summary() == "" and result.restored == []
        assert snapshot(repo) == before

    def test_restore_write_failure_leaves_no_temporary_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = make_repo(tmp_path)
        journal = transferred(repo, [witem("a.py", "worker one\n"), witem("b.py", "worker two\n")])
        real_rename = os.rename

        def flaky(src: str, dst: str, *args: Any, **kwargs: Any) -> None:
            if dst == "b.py":
                raise OSError(errno.ENOSPC, "No space left on device")
            real_rename(src, dst, *args, **kwargs)

        monkeypatch.setattr(os, "rename", flaky)
        result = oc.restore_transfer(journal, repo)
        monkeypatch.setattr(os, "rename", real_rename)
        assert [path for path, _ in result.not_restored] == ["b.py"]
        assert result.restored == ["a.py"]
        assert (repo / "a.py").read_text() == "import os\nx = 1\n"
        assert (repo / "b.py").read_text() == "worker two\n"
        assert not list(repo.glob(".verdict-transfer-*"))

    def test_restore_continues_after_a_state_read_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Review finding 5: a read failure on one path is that path's ``not_restored``, not the loop's end."""
        repo = make_repo(tmp_path)
        journal = transferred(repo, [witem("a.py", "worker one\n"), witem("b.py", "worker two\n")])
        real = oc._state_at

        def flaky(parent_fd: int, name: str, **kwargs: Any) -> Any:
            if name == "b.py":  # the last journal entry, so it is the first one restored
                raise PermissionError(errno.EACCES, "Permission denied")
            return real(parent_fd, name, **kwargs)

        monkeypatch.setattr(oc, "_state_at", flaky)
        result = oc.restore_transfer(journal, repo)
        monkeypatch.setattr(oc, "_state_at", real)
        assert result.restored == ["a.py"]
        assert [path for path, _ in result.not_restored] == ["b.py"]
        assert "PermissionError" in dict(result.not_restored)["b.py"]
        assert (repo / "a.py").read_text() == "import os\nx = 1\n"
        assert (repo / "b.py").read_text() == "worker two\n"  # named, not silently left
        assert journal == []
        assert "b.py" in result.summary()

    def test_rollback_continues_after_a_state_read_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Review finding 5, inside ``transfer``'s own rollback: nothing is left transferred unreported."""
        repo = make_repo(tmp_path)
        armed: list[bool] = []

        def fail_the_third(path: str) -> None:
            if path == "c.py":
                armed.append(True)
                raise OSError(errno.ENOSPC, "No space left on device")

        real = oc._state_at

        def flaky(parent_fd: int, name: str, **kwargs: Any) -> Any:
            if armed and name == "b.py":
                raise PermissionError(errno.EACCES, "Permission denied")
            return real(parent_fd, name, **kwargs)

        monkeypatch.setattr(oc, "_state_at", flaky)
        journal: list[oc.JournalEntry] = []
        items = [
            witem("a.py", "worker one\n"),
            witem("b.py", "worker two\n"),
            witem("c.py", "new\n"),
        ]
        with pytest.raises(oc.TransferFailedError) as caught:
            run_transfer(repo, items, None, journal, {"before_item": fail_the_third})
        monkeypatch.setattr(oc, "_state_at", real)
        assert "b.py" in caught.value.reason and "not restored" in caught.value.reason
        assert (repo / "a.py").read_text() == "import os\nx = 1\n"  # restored although b.py was not
        assert not (repo / "c.py").exists()
        assert journal == []

    def test_interrupted_restore_keeps_the_unprocessed_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Review finding 5: an interrupt mid-entry leaves that entry journalled, so a second restore covers it."""
        repo = make_repo(tmp_path)
        before = snapshot(repo)
        journal = transferred(repo, [witem("a.py", "worker one\n"), witem("b.py", "worker two\n")])
        real = oc._state_at

        def interrupted(parent_fd: int, name: str, **kwargs: Any) -> Any:
            if name == "b.py":
                raise KeyboardInterrupt
            return real(parent_fd, name, **kwargs)

        monkeypatch.setattr(oc, "_state_at", interrupted)
        with pytest.raises(KeyboardInterrupt):
            oc.restore_transfer(journal, repo)
        monkeypatch.setattr(oc, "_state_at", real)
        assert [entry.path for entry in journal] == ["a.py", "b.py"]
        oc.restore_transfer(journal, repo)
        assert snapshot(repo) == before

    def test_restore_of_a_modified_file_and_of_a_deleted_file(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        (repo / "b.py").chmod(0o755)
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "exec")
        before = snapshot(repo)
        journal = transferred(repo, [witem("a.py", "worker\n"), ditem("b.py")])
        assert not (repo / "b.py").exists()
        result = oc.restore_transfer(journal, repo)
        assert sorted(result.restored) == ["a.py", "b.py"]
        assert snapshot(repo) == before
        assert (repo / "b.py").stat().st_mode & stat.S_IXUSR

    def interrupt_right_after(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, which: str
    ) -> None:
        repo = make_repo(tmp_path)
        before = snapshot(repo)
        real = getattr(os, which)
        items, hit = interrupt_case(which)

        def interrupted(*args: Any, **kwargs: Any) -> Any:
            value = real(*args, **kwargs)  # the real operation happens ...
            if hit(args):
                raise KeyboardInterrupt  # ... and then the interrupt arrives
            return value

        monkeypatch.setattr(os, which, interrupted)
        journal: list[oc.JournalEntry] = []
        with pytest.raises(KeyboardInterrupt):
            run_transfer(repo, items, None, journal)
        monkeypatch.setattr(os, which, real)
        result = oc.restore_transfer(journal, repo)
        assert snapshot(repo) == before  # bytes, executable bit and the directory set
        assert result.not_restored == []

    def test_interrupt_right_after_a_rename(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.interrupt_right_after(tmp_path, monkeypatch, "rename")

    def test_interrupt_right_after_a_deletion(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.interrupt_right_after(tmp_path, monkeypatch, "unlink")

    def test_interrupt_right_after_a_directory_creation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.interrupt_right_after(tmp_path, monkeypatch, "mkdir")

    @pytest.mark.parametrize("which", ["rename", "unlink", "mkdir"])
    def test_interrupt_before_the_mutation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, which: str
    ) -> None:
        repo = make_repo(tmp_path)
        before = snapshot(repo)
        real = getattr(os, which)
        items, hit = interrupt_case(which)

        def interrupted(*args: Any, **kwargs: Any) -> Any:
            if hit(args):
                raise KeyboardInterrupt  # raised without performing the operation
            return real(*args, **kwargs)

        monkeypatch.setattr(os, which, interrupted)
        journal: list[oc.JournalEntry] = []
        with pytest.raises(KeyboardInterrupt):
            run_transfer(repo, items, None, journal)
        monkeypatch.setattr(os, which, real)
        result = oc.restore_transfer(journal, repo)
        assert result.restored == [] and result.not_restored == []
        assert snapshot(repo) == before


# --------------------------------------------------------------------------------------------
# 3.1 prompt
# --------------------------------------------------------------------------------------------


class TestPrompt:
    def test_prompt_carries_the_context_pack_unchanged(self) -> None:
        pack = "## Context\nline one  \n\n  indented `code` \u00e9\n"
        task = WorkUnit(
            unit_id="u1",
            objective="drop the unused import",
            owned_files=("a.py", "pkg/b.py"),
            verification_command=("python", "-m", "ruff", "check", "a.py"),
            context=pack,
        )
        prompt = oc.build_opencode_prompt(task)
        assert pack in prompt
        assert "drop the unused import" in prompt
        assert "- a.py" in prompt and "- pkg/b.py" in prompt
        assert "python -m ruff check a.py" in prompt
        assert "git commit" in prompt and "git push" in prompt

    def test_prompt_never_contains_the_credential(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VERDICT_WORKER_API_KEY", "SECRET-WORKER-KEY-123")
        monkeypatch.setenv("LLMGATE_AUTH_TOKEN", "SECRET-SERVE-TOKEN-456")
        prompt = oc.build_opencode_prompt(unit("a.py"))
        assert "SECRET-WORKER-KEY-123" not in prompt
        assert "SECRET-SERVE-TOKEN-456" not in prompt


# --------------------------------------------------------------------------------------------
# 3.2 executor
# --------------------------------------------------------------------------------------------


class StubRunner:
    """A stand-in for ``OpenCodeExecutor``: ``async run`` with the real signature."""

    def __init__(
        self,
        behave: Callable[[Path], None] | None = None,
        *,
        ok: bool = True,
        error: str = "",
        usage: AttemptUsage | None = None,
        raises: BaseException | None = None,
    ) -> None:
        self.behave = behave
        self.ok = ok
        self.error = error
        self.usage = usage
        self.raises = raises
        self.calls: list[dict[str, Any]] = []

    async def run(
        self,
        prompt: str,
        *,
        route_id: str,
        cwd: Path,
        timeout_seconds: float,
        run_id: str | None = None,
        node_id: str | None = None,
    ) -> WorkerTerminal:
        self.calls.append(
            {
                "prompt": prompt,
                "route_id": route_id,
                "cwd": Path(cwd),
                "timeout_seconds": timeout_seconds,
                "run_id": run_id,
                "node_id": node_id,
            }
        )
        if self.raises is not None:
            raise self.raises
        if self.behave is not None:
            self.behave(Path(cwd))
        return WorkerTerminal(
            ok=self.ok, model=route_id, error=self.error, usage=self.usage, harness="opencode"
        )


def make_executor(
    repo: Path,
    tmp_path: Path,
    runner: StubRunner,
    *,
    hooks: Mapping[str, Callable[..., None]] | None = None,
    run_id: str | None = "autodev-run-1",
) -> oc.OpenCodeUnitExecutor:
    temp_root = tmp_path / "attempts"
    temp_root.mkdir(exist_ok=True)
    executor = oc.OpenCodeUnitExecutor(
        repo,
        runner,
        model="cheap/model",
        base_url="http://127.0.0.1:8000",
        temp_root=temp_root,
        hooks=hooks,
    )
    if run_id is not None:
        executor.begin_run(run_id)
    return executor


def attempts_left(tmp_path: Path) -> list[Path]:
    return list((tmp_path / "attempts").iterdir())


def edit_a(tree: Path) -> None:
    write(tree, "a.py", "x = 2\n")


class TestExecute:
    def test_a_valid_owned_repair_is_applied(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        runner = StubRunner(edit_a)
        executor = make_executor(repo, tmp_path, runner)
        attempt = executor.execute_unit(unit("a.py"))
        assert attempt.outcome == "applied" and attempt.applied
        assert attempt.changed_files == ("a.py",)
        assert (repo / "a.py").read_text() == "x = 2\n"
        assert attempt.usage.reported is False
        call = runner.calls[0]
        assert call["route_id"] == "cheap/model"
        assert call["run_id"] == "autodev-run-1" and call["node_id"] == "u1"
        assert call["timeout_seconds"] == oc.ATTEMPT_TIMEOUT_SECONDS
        assert str(call["cwd"]).startswith(str(tmp_path / "attempts"))

    def test_usage_is_reported_when_the_worker_counts_tokens(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        runner = StubRunner(edit_a, usage=AttemptUsage(input_tokens=120, output_tokens=30))
        attempt = make_executor(repo, tmp_path, runner).execute_unit(unit("a.py"))
        assert attempt.usage.reported is True
        assert (attempt.usage.prompt_tokens, attempt.usage.completion_tokens) == (120, 30)

    @pytest.mark.parametrize(
        "error",
        [
            "timed out after 600s",
            "stalled: no output for 120s",
            "authentication failed (serve refused)",
            "exited 3",
        ],
    )
    def test_worker_failed(self, tmp_path: Path, error: str) -> None:
        repo = make_repo(tmp_path)
        before = snapshot(repo)
        runner = StubRunner(edit_a, ok=False, error=error)  # the tree holds an owned edit
        attempt = make_executor(repo, tmp_path, runner).execute_unit(unit("a.py"))
        assert attempt.outcome == "error"
        assert error in attempt.reason
        assert snapshot(repo) == before
        assert attempts_left(tmp_path) == []

    def test_worker_makes_no_change(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        before = snapshot(repo)
        attempt = make_executor(repo, tmp_path, StubRunner()).execute_unit(unit("a.py"))
        assert attempt.outcome == "rejected"
        assert attempt.reason == "worker made no change to the owned files"
        assert snapshot(repo) == before

    @pytest.mark.parametrize(
        "exit_path", ["applied", "rejected", "error", "exception", "interrupt"]
    )
    def test_attempt_tree_removed_on_every_exit_path(self, tmp_path: Path, exit_path: str) -> None:
        repo = make_repo(tmp_path)
        runner = {
            "applied": StubRunner(edit_a),
            "rejected": StubRunner(lambda tree: write(tree, "b.py", "evil\n")),
            "error": StubRunner(edit_a, ok=False, error="exited 1"),
            "exception": StubRunner(raises=RuntimeError("boom")),
            "interrupt": StubRunner(raises=KeyboardInterrupt()),
        }[exit_path]
        executor = make_executor(repo, tmp_path, runner)
        if exit_path == "exception":
            with pytest.raises(RuntimeError):
                executor.execute_unit(unit("a.py"))
        elif exit_path == "interrupt":
            with pytest.raises(KeyboardInterrupt):
                executor.execute_unit(unit("a.py"))
        else:
            assert executor.execute_unit(unit("a.py")).outcome == exit_path
        assert attempts_left(tmp_path) == []

    def test_attempt_tree_removed_when_the_seed_is_refused(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        (repo / "nested").mkdir()
        git(repo / "nested", "init", "-q", "-b", "main")
        write(repo, "nested/n.py", "n = 1\n")
        runner = StubRunner(edit_a)
        attempt = make_executor(repo, tmp_path, runner).execute_unit(unit("a.py"))
        assert attempt.outcome == "error" and "nested repository" in attempt.reason
        assert runner.calls == []
        assert attempts_left(tmp_path) == []

    def test_tree_is_removed_before_the_transfer(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        seen: list[list[Path]] = []

        def look() -> None:
            seen.append(attempts_left(tmp_path))

        executor = make_executor(repo, tmp_path, StubRunner(edit_a), hooks={"after_checks": look})
        assert executor.execute_unit(unit("a.py")).applied
        assert seen == [[]]

    def test_running_event_loop_returns_error(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        runner = StubRunner(edit_a)
        executor = make_executor(repo, tmp_path, runner)

        async def inside() -> Any:
            return executor.execute_unit(unit("a.py"))

        attempt = asyncio.run(inside())
        assert attempt.outcome == "error" and "running event loop" in attempt.reason
        assert runner.calls == [] and attempts_left(tmp_path) == []

    def test_no_run_id(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        runner = StubRunner(edit_a)
        executor = make_executor(repo, tmp_path, runner, run_id=None)
        attempt = executor.execute_unit(unit("a.py"))
        assert attempt.outcome == "error" and "run id" in attempt.reason
        assert runner.calls == [] and attempts_left(tmp_path) == []

    @pytest.mark.parametrize(
        ("timeout", "accepted"), [(119.0, False), (120.0, True), (1740.0, True), (1741.0, False)]
    )
    def test_attempt_timeout_bounds(self, tmp_path: Path, timeout: float, accepted: bool) -> None:
        repo = make_repo(tmp_path)
        if accepted:
            oc.OpenCodeUnitExecutor(repo, StubRunner(), model="m", timeout_seconds=timeout)
        else:
            with pytest.raises(ValueError, match="attempt timeout"):
                oc.OpenCodeUnitExecutor(repo, StubRunner(), model="m", timeout_seconds=timeout)

    def test_stub_matches_the_real_run_signature(self) -> None:
        real = inspect.signature(OpenCodeExecutor.run)
        stub = inspect.signature(StubRunner.run)
        assert list(stub.parameters) == list(real.parameters)
        for name, parameter in real.parameters.items():
            assert stub.parameters[name].kind == parameter.kind
            assert stub.parameters[name].default == parameter.default

    def test_journal_is_emptied_at_the_start_of_each_attempt(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        runner = StubRunner(edit_a)
        executor = make_executor(repo, tmp_path, runner)
        assert executor.execute_unit(unit("a.py")).applied  # unit A, verified by the caller
        runner.behave = None  # unit B: the worker changes nothing and is rejected
        assert executor.execute_unit(unit("b.py")).outcome == "rejected"
        assert executor.restore() == ""  # nothing of unit B to undo ...
        assert (repo / "a.py").read_text() == "x = 2\n"  # ... and unit A's file stays

    def test_no_served_identity_is_claimed(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        executor = make_executor(repo, tmp_path, StubRunner(edit_a))
        attempt = executor.execute_unit(unit("a.py"))
        assert attempt.resolved_model is None
        assert executor.response_headers == {}
        assert executor.config.model == "cheap/model"
        assert executor.config.temperature is None and executor.config.max_tokens is None
        assert executor.isolated is True

    def test_credential_never_appears_in_a_record(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VERDICT_WORKER_API_KEY", "SECRET-WORKER-KEY-123")
        repo = make_repo(tmp_path)
        runner = StubRunner(ok=False, error="serve refused the route; key [REDACTED]")
        attempt = make_executor(repo, tmp_path, runner).execute_unit(unit("a.py"))
        assert "[REDACTED]" in attempt.reason
        assert "SECRET-WORKER-KEY-123" not in repr(attempt.to_dict())
        assert "SECRET-WORKER-KEY-123" not in runner.calls[0]["prompt"]


def no_follow_chmod_works(tmp_path: Path) -> bool:
    """Whether this Python and kernel can chmod a directory by name relative to a descriptor, no-follow."""
    probe = tmp_path / "chmod-probe"
    (probe / "child").mkdir(parents=True)
    fd = os.open(probe, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.chmod("child", 0o700, dir_fd=fd, follow_symlinks=False)
        return True
    except (OSError, NotImplementedError, ValueError):
        return False
    finally:
        os.close(fd)


def open_descriptors() -> int:
    return len(os.listdir("/proc/self/fd"))


class TestAttemptDirectory:
    """Review finding 4: the attempt directory is worker-controlled, so its cleanup follows no path."""

    def test_cleanup_does_not_follow_a_replaced_attempt_directory(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        external = tmp_path / "external"
        (external / "sub").mkdir(parents=True)
        (external / "sub" / "f.txt").write_text("outside\n")
        external.chmod(0o755)
        (external / "sub").chmod(0o755)
        moved = tmp_path / "moved"

        def hostile(tree: Path) -> None:
            edit_a(tree)
            attempt_dir = tree.parent
            attempt_dir.rename(moved)  # the worker moves its own attempt directory ...
            attempt_dir.symlink_to(
                external
            )  # ... and leaves a link to an outside directory in its place

        attempt = make_executor(repo, tmp_path, StubRunner(hostile)).execute_unit(unit("a.py"))
        assert attempt.outcome == "error"
        assert stat.S_IMODE(external.stat().st_mode) == 0o755
        assert stat.S_IMODE((external / "sub").stat().st_mode) == 0o755
        assert (external / "sub" / "f.txt").read_text() == "outside\n"
        assert list(moved.iterdir()) == []  # the directory Verdict made was still emptied

    def test_cleanup_leaves_a_directory_that_replaced_the_attempt_directory(
        self, tmp_path: Path
    ) -> None:
        """Only the entry that still is the directory Verdict made is removed by name."""
        repo = make_repo(tmp_path)
        moved = tmp_path / "moved"
        replaced: list[Path] = []

        def hostile(tree: Path) -> None:
            edit_a(tree)
            tree.parent.rename(moved)
            tree.parent.mkdir()  # an empty directory of the worker's, at the name Verdict made
            replaced.append(tree.parent)

        attempt = make_executor(repo, tmp_path, StubRunner(hostile)).execute_unit(unit("a.py"))
        assert attempt.outcome == "error"
        assert replaced[0].is_dir() and list(replaced[0].iterdir()) == []
        assert list(moved.iterdir()) == []  # the original was emptied through its descriptor

    def test_a_tree_reached_through_a_replaced_attempt_directory_is_not_read(
        self, tmp_path: Path
    ) -> None:
        """The walk and the reads open the tree once and accept only the directory that was seeded."""
        repo = make_repo(tmp_path)
        before = snapshot(repo)
        forged = tmp_path / "forged"
        forged.mkdir()

        def hostile(tree: Path) -> None:
            shutil.copytree(tree, forged / "tree", symlinks=True)  # a complete copy ...
            write(forged / "tree", "a.py", "x = 2\n")  # ... holding an owned edit
            tree.parent.rename(tmp_path / "moved")
            tree.parent.symlink_to(forged)  # the executor's path to the tree now leads here

        attempt = make_executor(repo, tmp_path, StubRunner(hostile)).execute_unit(unit("a.py"))
        assert attempt.outcome == "error"
        assert "changed during validation" in attempt.reason
        assert snapshot(repo) == before

    def test_cleanup_does_not_follow_a_link_inside_the_tree(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path, {"a.py": "a = 1\n", ".gitignore": "cache/\n"})
        external = tmp_path / "external"
        external.mkdir()
        (external / "f.txt").write_text("outside\n")
        external.chmod(0o755)

        def hostile(tree: Path) -> None:
            edit_a(tree)
            (tree / "cache").mkdir()  # ignored, so a byproduct
            (tree / "cache" / "link").symlink_to(external)
            (tree / "cache").chmod(0o500)

        attempt = make_executor(repo, tmp_path, StubRunner(hostile)).execute_unit(unit("a.py"))
        assert attempt.outcome == "applied"
        assert attempts_left(tmp_path) == []
        assert stat.S_IMODE(external.stat().st_mode) == 0o755
        assert (external / "f.txt").read_text() == "outside\n"

    @pytest.mark.parametrize(("mode", "outcome"), [(0o555, "applied"), (0o000, "error")])
    def test_cleanup_removes_directories_the_worker_made_unusable(
        self, tmp_path: Path, mode: int, outcome: str
    ) -> None:
        if mode == 0 and not no_follow_chmod_works(tmp_path):
            pytest.skip("this host cannot chmod a directory by name without following links")
        repo = make_repo(tmp_path, {"a.py": "a = 1\n", ".gitignore": "cache/\n"})

        def hostile(tree: Path) -> None:
            edit_a(tree)
            write(tree, "cache/deep/f.txt", "x\n")  # ignored, so the 0o555 case is applied
            (tree / "cache" / "deep").chmod(mode)
            (tree / "cache").chmod(mode)

        attempt = make_executor(repo, tmp_path, StubRunner(hostile)).execute_unit(unit("a.py"))
        assert attempt.outcome == outcome
        assert attempts_left(tmp_path) == []

    @pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs /proc/self/fd")
    @pytest.mark.parametrize(
        "exit_path", ["applied", "rejected", "error", "exception", "interrupt"]
    )
    def test_no_descriptor_is_left_open(self, tmp_path: Path, exit_path: str) -> None:
        repo = make_repo(tmp_path)
        runner = {
            "applied": StubRunner(edit_a),
            "rejected": StubRunner(lambda tree: write(tree, "b.py", "evil\n")),
            "error": StubRunner(edit_a, ok=False, error="exited 1"),
            "exception": StubRunner(raises=RuntimeError("boom")),
            "interrupt": StubRunner(raises=KeyboardInterrupt()),
        }[exit_path]
        executor = make_executor(repo, tmp_path, runner)
        before = open_descriptors()
        with contextlib.suppress(RuntimeError, KeyboardInterrupt):
            executor.execute_unit(unit("a.py"))
        assert open_descriptors() == before


# --------------------------------------------------------------------------------------------
# 3.3 preflight
# --------------------------------------------------------------------------------------------


def preflight_executor(
    repo: Path, tmp_path: Path, git_runner: Callable[..., Any] | None = None
) -> oc.OpenCodeUnitExecutor:
    kwargs: dict[str, Any] = {} if git_runner is None else {"git_runner": git_runner}
    temp_root = tmp_path / "attempts"
    temp_root.mkdir(exist_ok=True)
    return oc.OpenCodeUnitExecutor(
        repo, StubRunner(), model="cheap/model", temp_root=temp_root, **kwargs
    )


def refused(repo: Path, tmp_path: Path, owned: tuple[str, ...], path: str) -> str:
    before = snapshot(repo)
    reason = preflight_executor(repo, tmp_path).preflight(unit(*owned))
    assert reason is not None
    assert path in reason
    assert "commit or stash" in reason or "ignore rule" in reason
    assert snapshot(repo) == before
    assert attempts_left(tmp_path) == []
    return reason


class TestPreflight:
    def test_tracked_and_unmodified_owned_file(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        assert preflight_executor(repo, tmp_path).preflight(unit("a.py", "b.py")) is None

    def test_modified_owned_file_refused(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        write(repo, "a.py", "x = 99\n")
        refused(repo, tmp_path, ("a.py",), "a.py")

    @pytest.mark.parametrize("how", ["staged", "deleted"])
    def test_staged_or_deleted_owned_file_refused(self, tmp_path: Path, how: str) -> None:
        repo = make_repo(tmp_path)
        if how == "staged":
            write(repo, "a.py", "x = 99\n")
            git(repo, "add", "a.py")  # staged, and the working tree equals the index
        else:
            (repo / "a.py").unlink()
        refused(repo, tmp_path, ("a.py",), "a.py")

    @pytest.mark.parametrize("how", ["untracked", "ignored"])
    def test_untracked_or_ignored_owned_file_refused(self, tmp_path: Path, how: str) -> None:
        repo = make_repo(tmp_path)
        if how == "untracked":
            write(repo, "fresh.py", "f = 1\n")
        else:
            write(repo, ".gitignore", "fresh.py\n")
            git(repo, "add", ".gitignore")
            git(repo, "commit", "-qm", "ignore")
            write(repo, "fresh.py", "f = 1\n")
        refused(repo, tmp_path, ("fresh.py",), "fresh.py")

    def test_assume_unchanged_owned_file_refused(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        git(repo, "update-index", "--assume-unchanged", "a.py")
        write(repo, "a.py", "x = 99\n")
        assert git(repo, "status", "--porcelain") == ""  # the edit is invisible to git status
        reason = refused(repo, tmp_path, ("a.py",), "a.py")
        assert "'h'" in reason

    @pytest.mark.parametrize("state", ["edited", "absent"])
    def test_skip_worktree_owned_file_refused(self, tmp_path: Path, state: str) -> None:
        repo = make_repo(tmp_path)
        git(repo, "update-index", "--skip-worktree", "a.py")
        if state == "edited":
            write(repo, "a.py", "x = 99\n")
        else:
            (repo / "a.py").unlink()
        assert git(repo, "status", "--porcelain") == ""
        reason = refused(repo, tmp_path, ("a.py",), "a.py")
        assert "'S'" in reason

    @pytest.mark.parametrize("attribute", ["filter", "ident"])
    def test_owned_file_with_a_filter_or_ident_attribute_refused(
        self, tmp_path: Path, attribute: str
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        if attribute == "filter":
            git(repo, "config", "filter.strip.clean", "sed 's/  # note.*//'")
            write(repo, ".gitattributes", "a.txt filter=strip\n")
            write(repo, "a.txt", "x = 1  # note A\n")
            edited = "x = 1  # note B\n"
        else:
            write(repo, ".gitattributes", "a.txt ident\n")
            write(repo, "a.txt", "$Id: " + "1" * 40 + " $\n")
            edited = "$Id: " + "2" * 40 + " $\n"
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "init")
        write(repo, "a.txt", edited)
        assert git(repo, "status", "--porcelain") == ""  # same-size edit the attribute hides
        reason = refused(repo, tmp_path, ("a.txt",), "a.txt")
        assert attribute in reason

    @pytest.mark.parametrize("with_attribute", [False, True])
    def test_absent_owned_file_accepted(self, tmp_path: Path, with_attribute: bool) -> None:
        repo = make_repo(tmp_path)
        if with_attribute:
            write(repo, ".gitattributes", "new_file.py filter=anything\n")
            git(repo, "add", ".gitattributes")
            git(repo, "commit", "-qm", "attr")
        assert preflight_executor(repo, tmp_path).preflight(unit("new_file.py")) is None

    def test_ignored_absent_owned_file_refused(self, tmp_path: Path) -> None:
        """Carried finding 1: a unit that would create an ignored file is refused."""
        repo = make_repo(tmp_path, {"a.py": "x = 1\n", ".gitignore": "generated_*.py\n"})
        reason = refused(repo, tmp_path, ("generated_out.py",), "generated_out.py")
        assert "ignore rule" in reason

    def test_ignored_and_plain_absent_owned_files_refused(self, tmp_path: Path) -> None:
        """Carried finding 1, mixed: the whole unit is refused, so nothing is half-transferred."""
        repo = make_repo(tmp_path, {"a.py": "x = 1\n", ".gitignore": "generated_*.py\n"})
        before = snapshot(repo)
        reason = preflight_executor(repo, tmp_path).preflight(
            unit("generated_out.py", "plain_out.py")
        )
        assert reason is not None and "generated_out.py" in reason
        assert snapshot(repo) == before
        assert not (repo / "plain_out.py").exists()

    def test_unowned_dirty_file_does_not_refuse(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        write(repo, "b.py", "dirty = 1\n")
        write(repo, "untracked.py", "u = 1\n")
        assert preflight_executor(repo, tmp_path).preflight(unit("a.py")) is None
        tree = seed(repo, tmp_path, ("a.py",))  # ... and the dirty unowned files go into the seed
        assert (tree.path / "b.py").read_text() == "dirty = 1\n"
        assert (tree.path / "untracked.py").read_text() == "u = 1\n"

    @pytest.mark.parametrize("read", ["status", "ls-files", "check-attr", "check-ignore"])
    def test_git_read_fails_closed(self, tmp_path: Path, read: str) -> None:
        repo = make_repo(tmp_path)
        owned = ("a.py", "gen.py") if read == "check-ignore" else ("a.py",)

        def failing(argv: list[str], **kwargs: Any) -> Any:
            if read in argv:
                # check-ignore exits 1 for "nothing ignored"; any status other than 0 and 1 is a failure
                return subprocess.CompletedProcess(argv, 128, b"", b"fatal: simulated")
            return subprocess.run(argv, **kwargs)

        before = snapshot(repo)
        reason = preflight_executor(repo, tmp_path, failing).preflight(unit(*owned))
        assert reason is not None and read.replace("-", " ") in reason.replace("-", " ")
        assert "simulated" in reason
        assert snapshot(repo) == before

    def test_check_ignore_exit_one_is_not_a_failure(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)

        def clean(argv: list[str], **kwargs: Any) -> Any:
            if "check-ignore" in argv:
                return subprocess.CompletedProcess(argv, 1, b"", b"")
            return subprocess.run(argv, **kwargs)

        assert preflight_executor(repo, tmp_path, clean).preflight(unit("gen.py")) is None


# --------------------------------------------------------------------------------------------
# 3.4 isolation matrix
# --------------------------------------------------------------------------------------------


class TestMatrix:
    @pytest.mark.parametrize(
        "variant",
        ["plain", "staged", "commit", "unowned_new_file", "symlink_swap", "push", "planted_config"],
    )
    def test_out_of_bounds_edits_are_rejected(self, tmp_path: Path, variant: str) -> None:
        repo = make_repo(tmp_path)
        before = snapshot(repo)
        marker = tmp_path / "marker"

        def hostile(tree: Path) -> None:
            if variant == "plain":
                write(tree, "b.py", "evil\n")
            elif variant == "staged":
                write(tree, "b.py", "evil\n")
                git(tree, "add", "b.py")
            elif variant == "commit":
                write(tree, "b.py", "evil\n")
                git(tree, "add", "b.py")
                git(tree, "commit", "-qm", "evil", "--no-verify")
            elif variant == "unowned_new_file":
                write(tree, "newfile.py", "evil\n")
            elif variant == "symlink_swap":
                (tree / "a.py").unlink()
                (tree / "a.py").symlink_to(repo / "b.py")
            elif variant == "push":
                write(tree, "b.py", "evil\n")
                done = subprocess.run(
                    ["git", "push", "origin"], cwd=tree, capture_output=True, text=True, check=False
                )
                assert done.returncode != 0  # the clone has no origin to push to
            else:
                script = tmp_path / "plant.sh"
                script.write_text(f"#!/bin/sh\ntouch {marker}\n")
                script.chmod(0o755)
                with (tree / ".git" / "config").open("a") as handle:
                    handle.write(f"[core]\n\tfsmonitor = {script}\n")
                hook = tree / ".git" / "hooks" / "pre-commit"
                hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
                hook.chmod(0o755)
                write(tree, "b.py", "evil\n")

        executor = make_executor(repo, tmp_path, StubRunner(hostile))
        attempt = executor.execute_unit(unit("a.py"))
        assert attempt.outcome == "rejected"
        assert snapshot(repo) == before
        assert attempts_left(tmp_path) == []
        assert not marker.exists()

    def test_no_context_file_at_all(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        attempt = make_executor(repo, tmp_path, StubRunner(edit_a)).execute_unit(unit("a.py"))
        assert attempt.outcome == "applied" and attempt.changed_files == ("a.py",)
        assert git(repo, "status", "--porcelain", "--untracked-files=all") == " M a.py\n"

    @pytest.mark.xfail(
        strict=True,
        reason="ADR-053 Decision 4: a worker shell can write an absolute path in the user's tree",
    )
    def test_absolute_path_write_is_an_accepted_risk(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        before = snapshot(repo)

        def absolute(tree: Path) -> None:
            edit_a(tree)
            (repo / "written-by-absolute-path.txt").write_text("a shell can do this\n")

        make_executor(repo, tmp_path, StubRunner(absolute)).execute_unit(unit("a.py"))
        assert snapshot(repo) == before
