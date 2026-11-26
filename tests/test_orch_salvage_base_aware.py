"""Salvage judges only the lines an attempt added, against the base version."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from verdict.orchestration.salvage import (
    _has_credential_content,
    restore_salvaged_files,
    salvage_owned_files,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
BASE_LINE = "def check(token: str) -> None:"
CLEAN_LINE = "    return None"
SECRET_LINE = 'API_KEY = "sk-' + "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bC1dE3fG5hJ7k" + '"'


def _git(repo: Path, *args: str) -> str:
    env = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(repo.parent),
    }
    done = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, env=env
    )
    return done.stdout.strip()


def _make_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "mod.py").write_text(BASE_LINE + "\n    pass\n", encoding="utf-8")
    _git(repo, "add", "mod.py")
    _git(repo, "commit", "-q", "-m", "base")
    return repo, _git(repo, "rev-parse", "HEAD")


def _append(repo: Path, name: str, line: str) -> None:
    path = repo / name
    path.write_text(path.read_text(encoding="utf-8") + line + "\n", encoding="utf-8")


def _fresh_checkout(tmp_path: Path, repo: Path) -> Path:
    fresh = tmp_path / "fresh"
    subprocess.run(
        ["git", "clone", "-q", str(repo), str(fresh)],
        check=True,
        capture_output=True,
        env={"GIT_CONFIG_GLOBAL": "/dev/null", "PATH": "/usr/bin:/bin:/usr/local/bin"},
    )
    return fresh


def test_clean_added_line_on_base_token_file_is_salvaged_and_restored(tmp_path: Path) -> None:
    repo, base = _make_repo(tmp_path)
    _append(repo, "mod.py", CLEAN_LINE)
    salvage_dir = tmp_path / "salvage"

    saved = salvage_owned_files(
        repo, ("mod.py",), salvage_dir, changed_files=["mod.py"], base_sha=base
    )
    assert saved.saved == ("mod.py",)
    assert saved.skipped == ()

    fresh = _fresh_checkout(tmp_path, repo)
    (fresh / "mod.py").write_text(BASE_LINE + "\n    pass\n", encoding="utf-8")
    restored = restore_salvaged_files(salvage_dir, fresh, ("mod.py",))
    assert restored.restored == ("mod.py",)
    assert CLEAN_LINE in (fresh / "mod.py").read_text(encoding="utf-8")


def test_added_credential_line_is_skipped_at_capture(tmp_path: Path) -> None:
    repo, base = _make_repo(tmp_path)
    _append(repo, "mod.py", SECRET_LINE)
    salvage_dir = tmp_path / "salvage"

    result = salvage_owned_files(
        repo, ("mod.py",), salvage_dir, changed_files=["mod.py"], base_sha=base
    )
    assert result.saved == ()
    assert result.skipped == ("mod.py",)
    assert SECRET_LINE not in repr(result)


def test_credential_bearing_salvage_copy_is_skipped_at_restore(tmp_path: Path) -> None:
    repo, _base = _make_repo(tmp_path)
    salvage_dir = tmp_path / "salvage"
    salvage_dir.mkdir()
    (salvage_dir / "mod.py").write_text(
        BASE_LINE + "\n    pass\n" + SECRET_LINE + "\n", encoding="utf-8"
    )
    fresh = _fresh_checkout(tmp_path, repo)

    result = restore_salvaged_files(salvage_dir, fresh, ("mod.py",))
    assert result.restored == ()
    assert result.skipped == ("mod.py",)
    assert SECRET_LINE not in (fresh / "mod.py").read_text(encoding="utf-8")


def test_new_clean_file_is_salvaged_and_new_credential_file_is_skipped(tmp_path: Path) -> None:
    repo, base = _make_repo(tmp_path)
    (repo / "clean.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "secret.py").write_text(SECRET_LINE + "\n", encoding="utf-8")
    salvage_dir = tmp_path / "salvage"

    result = salvage_owned_files(
        repo,
        ("clean.py", "secret.py"),
        salvage_dir,
        changed_files=["clean.py", "secret.py"],
        base_sha=base,
    )
    assert result.saved == ("clean.py",)
    assert result.skipped == ("secret.py",)


@pytest.mark.parametrize("bad_sha", ["", "not-hex-at-all", "abc12", "zzzzzzz", "HEAD"])
def test_missing_or_invalid_base_sha_judges_whole_file(tmp_path: Path, bad_sha: str) -> None:
    repo, _base = _make_repo(tmp_path)
    (repo / "mod.py").write_text(
        BASE_LINE + "\n    pass\nPASSWORD = 'hunter2hunter2'\n", encoding="utf-8"
    )
    salvage_dir = tmp_path / "salvage"

    result = salvage_owned_files(
        repo, ("mod.py",), salvage_dir, changed_files=["mod.py"], base_sha=bad_sha
    )
    assert result.saved == ()
    assert result.skipped == ("mod.py",)

    # The whole-file rule also rejects the base version's own token line when the
    # base cannot be read, exactly as before.
    plain = tmp_path / "plain.py"
    plain.write_text("token = 'abcdefgh'\n", encoding="utf-8")
    assert _has_credential_content(plain) is True


def test_unchanged_repo_python_files_are_not_rejected_against_themselves() -> None:
    listed = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "verdict/*.py"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert listed
    for rel in listed:
        path = REPO_ROOT / rel
        base_text = path.read_text(encoding="utf-8", errors="replace")
        assert _has_credential_content(path, base_text) is False, rel
