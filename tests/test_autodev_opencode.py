"""OC2a (#1301): the isolated OpenCode executor for `verdict autodev` units.

Task groups 2 and 3 of ``openspec/changes/autodev-opencode-isolated-executor``. Nothing here calls a
model, a probe or the real ``opencode``; every test runs in a temporary home with git's own global and
system configuration switched off.
"""

from __future__ import annotations

import errno
import hashlib
import os
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
        result = oc.restore(journal, repo)
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
        result = oc.restore(journal, repo)
        assert directory.is_dir() and list(directory.iterdir()) == []
        assert ("newdir", "replaced by another directory") in result.not_restored
        assert "newdir" in result.summary()

    def test_user_edit_after_the_transfer_is_kept(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        journal = transferred(repo, [witem("a.py", "worker\n")])
        write(repo, "a.py", "user edit after the transfer\n")
        result = oc.restore(journal, repo)
        assert (repo / "a.py").read_text() == "user edit after the transfer\n"
        assert result.not_restored == [("a.py", "changed since the transfer")]

    def test_user_mode_change_after_the_transfer_is_kept(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        journal = transferred(repo, [witem("a.py", "worker\n")])
        (repo / "a.py").chmod(0o755)
        result = oc.restore(journal, repo)
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
        result = oc.restore(journal, repo)
        assert (outside / "inner.py").read_text() == "outside content\n"
        assert [path for path, _ in result.not_restored] == ["d/inner.py"]

    def test_restore_twice(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        before = snapshot(repo)
        journal = transferred(repo, [witem("a.py", "worker\n")])
        first = oc.restore(journal, repo)
        second = oc.restore(journal, repo)
        assert first.restored == ["a.py"]
        assert second.restored == [] and second.not_restored == [] and second.summary() == ""
        assert snapshot(repo) == before

    def test_restore_without_a_transfer(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        before = snapshot(repo)
        result = oc.restore([], repo)
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
        result = oc.restore(journal, repo)
        monkeypatch.setattr(os, "rename", real_rename)
        assert [path for path, _ in result.not_restored] == ["b.py"]
        assert result.restored == ["a.py"]
        assert (repo / "a.py").read_text() == "import os\nx = 1\n"
        assert (repo / "b.py").read_text() == "worker two\n"
        assert not list(repo.glob(".verdict-transfer-*"))

    def test_restore_of_a_modified_file_and_of_a_deleted_file(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        (repo / "b.py").chmod(0o755)
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "exec")
        before = snapshot(repo)
        journal = transferred(repo, [witem("a.py", "worker\n"), ditem("b.py")])
        assert not (repo / "b.py").exists()
        result = oc.restore(journal, repo)
        assert sorted(result.restored) == ["a.py", "b.py"]
        assert snapshot(repo) == before
        assert (repo / "b.py").stat().st_mode & stat.S_IXUSR

    @pytest.mark.parametrize("which", ["rename", "unlink", "mkdir"])
    def test_interrupt_right_after_a_mutation(
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
        result = oc.restore(journal, repo)
        assert snapshot(repo) == before
        assert result.not_restored == []

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
        result = oc.restore(journal, repo)
        assert result.restored == [] and result.not_restored == []
        assert snapshot(repo) == before
