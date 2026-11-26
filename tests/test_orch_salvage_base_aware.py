"""Salvage judges only the lines an attempt added, against the base version."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import verdict.orchestration.salvage as salvage_module
from verdict.orchestration.salvage import (
    _has_credential_content,
    restore_salvaged_files,
    salvage_owned_files,
)
from verdict.security import redact_text

REPO_ROOT = Path(__file__).resolve().parent.parent
BASE_LINE = "def check(token: str) -> None:"
BASE_TEXT = BASE_LINE + "\n    pass\n"
CLEAN_LINE = "    return None"
SECRET_LINE = 'API_KEY = "sk-' + "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bC1dE3fG5hJ7k" + '"'
# Use the PATH this test run inherited so git is found wherever it is installed.
PATH = os.environ.get("PATH") or "/usr/bin:/bin:/usr/local/bin"


def _git(repo: Path, *args: str) -> str:
    env = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "PATH": PATH,
        "HOME": str(repo.parent),
    }
    done = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, env=env
    )
    return done.stdout.strip()


def _make_repo(tmp_path: Path, base_text: str = BASE_TEXT) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "mod.py").write_text(base_text, encoding="utf-8")
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
        env={"GIT_CONFIG_GLOBAL": "/dev/null", "PATH": PATH},
    )
    return fresh


def test_clean_added_line_on_base_token_file_is_salvaged_and_restored(tmp_path: Path) -> None:
    repo, base = _make_repo(tmp_path)
    _append(repo, "mod.py", CLEAN_LINE)
    salvage_dir = tmp_path / "salvage"

    saved = salvage_owned_files(
        repo, ("mod.py",), salvage_dir, changed_files=["mod.py"], base_sha=base, repo_root=repo
    )
    assert saved.saved == ("mod.py",)
    assert saved.skipped == ()

    fresh = _fresh_checkout(tmp_path, repo)
    (fresh / "mod.py").write_text(BASE_TEXT, encoding="utf-8")
    restored = restore_salvaged_files(salvage_dir, fresh, ("mod.py",))
    assert restored.restored == ("mod.py",)
    assert CLEAN_LINE in (fresh / "mod.py").read_text(encoding="utf-8")


def test_added_credential_line_is_skipped_at_capture(tmp_path: Path) -> None:
    repo, base = _make_repo(tmp_path)
    _append(repo, "mod.py", SECRET_LINE)
    salvage_dir = tmp_path / "salvage"

    result = salvage_owned_files(
        repo, ("mod.py",), salvage_dir, changed_files=["mod.py"], base_sha=base, repo_root=repo
    )
    assert result.saved == ()
    assert result.skipped == ("mod.py",)
    assert SECRET_LINE not in repr(result)


def test_credential_bearing_salvage_copy_is_skipped_at_restore(tmp_path: Path) -> None:
    repo, _base = _make_repo(tmp_path)
    salvage_dir = tmp_path / "salvage"
    salvage_dir.mkdir()
    (salvage_dir / "mod.py").write_text(BASE_TEXT + SECRET_LINE + "\n", encoding="utf-8")
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
        repo_root=repo,
    )
    assert result.saved == ("clean.py",)
    assert result.skipped == ("secret.py",)


# ---------------------------------------------------------------- C1: duplicates


def test_duplicating_a_base_credential_line_is_skipped_at_capture(tmp_path: Path) -> None:
    """A credential-shaped line already in the base is tolerated once per base copy."""
    repo, base = _make_repo(tmp_path, BASE_TEXT + SECRET_LINE + "\n")

    # Control: leaving the base's credential line alone and adding a clean line is saved.
    _append(repo, "mod.py", CLEAN_LINE)
    kept = salvage_owned_files(
        repo,
        ("mod.py",),
        tmp_path / "salvage-kept",
        changed_files=["mod.py"],
        base_sha=base,
        repo_root=repo,
    )
    assert kept.saved == ("mod.py",)

    # The attempt adds a second copy of the same line: that copy is an added credential.
    (repo / "mod.py").write_text(BASE_TEXT + SECRET_LINE + "\n" + SECRET_LINE + "\n")
    duplicated = salvage_owned_files(
        repo,
        ("mod.py",),
        tmp_path / "salvage-dup",
        changed_files=["mod.py"],
        base_sha=base,
        repo_root=repo,
    )
    assert duplicated.saved == ()
    assert duplicated.skipped == ("mod.py",)
    assert SECRET_LINE not in repr(duplicated)
    assert not (tmp_path / "salvage-dup" / "mod.py").exists()


def test_capture_tolerates_exactly_as_many_credential_copies_as_the_base_has(
    tmp_path: Path,
) -> None:
    repo, base = _make_repo(tmp_path, BASE_TEXT + SECRET_LINE + "\n" + SECRET_LINE + "\n")

    (repo / "mod.py").write_text(BASE_TEXT + SECRET_LINE + "\n" + SECRET_LINE + "\n" + CLEAN_LINE)
    same = salvage_owned_files(
        repo,
        ("mod.py",),
        tmp_path / "salvage-same",
        changed_files=["mod.py"],
        base_sha=base,
        repo_root=repo,
    )
    assert same.saved == ("mod.py",)

    (repo / "mod.py").write_text(BASE_TEXT + (SECRET_LINE + "\n") * 3)
    more = salvage_owned_files(
        repo,
        ("mod.py",),
        tmp_path / "salvage-more",
        changed_files=["mod.py"],
        base_sha=base,
        repo_root=repo,
    )
    assert more.saved == ()
    assert more.skipped == ("mod.py",)


def test_duplicating_a_base_credential_line_is_skipped_at_restore(tmp_path: Path) -> None:
    repo, _base = _make_repo(tmp_path, BASE_TEXT + SECRET_LINE + "\n")

    kept_dir = tmp_path / "salvage-kept"
    kept_dir.mkdir()
    (kept_dir / "mod.py").write_text(BASE_TEXT + SECRET_LINE + "\n" + CLEAN_LINE + "\n")
    fresh_kept = _fresh_checkout(tmp_path, repo)
    kept = restore_salvaged_files(kept_dir, fresh_kept, ("mod.py",))
    assert kept.restored == ("mod.py",)

    dup_dir = tmp_path / "salvage-dup"
    dup_dir.mkdir()
    (dup_dir / "mod.py").write_text(BASE_TEXT + SECRET_LINE + "\n" + SECRET_LINE + "\n")
    fresh_dup = tmp_path / "fresh-dup"
    fresh_dup.mkdir()
    (fresh_dup / "mod.py").write_text(BASE_TEXT + SECRET_LINE + "\n", encoding="utf-8")
    duplicated = restore_salvaged_files(dup_dir, fresh_dup, ("mod.py",))
    assert duplicated.restored == ()
    assert duplicated.skipped == ("mod.py",)
    assert (fresh_dup / "mod.py").read_text(encoding="utf-8").count(SECRET_LINE) == 1


# ------------------------------------------------------------ R1: base fallback


@pytest.mark.parametrize("bad_sha", ["", "not-hex-at-all", "abc12", "zzzzzzz", "HEAD", "0" * 40])
def test_missing_or_invalid_base_judges_the_whole_file(tmp_path: Path, bad_sha: str) -> None:
    repo, base = _make_repo(tmp_path)
    # Precondition: the base's own signature line already matches the redaction rules, so
    # only the base comparison can let a clean addition through.
    assert redact_text(BASE_TEXT) != BASE_TEXT
    _append(repo, "mod.py", CLEAN_LINE)

    fallback = salvage_owned_files(
        repo,
        ("mod.py",),
        tmp_path / "salvage-fallback",
        changed_files=["mod.py"],
        base_sha=bad_sha,
        repo_root=repo,
    )
    assert fallback.saved == ()
    assert fallback.skipped == ("mod.py",)

    valid = salvage_owned_files(
        repo,
        ("mod.py",),
        tmp_path / "salvage-valid",
        changed_files=["mod.py"],
        base_sha=base,
        repo_root=repo,
    )
    assert valid.saved == ("mod.py",)
    assert valid.skipped == ()


def test_no_repo_root_judges_the_whole_file(tmp_path: Path) -> None:
    repo, base = _make_repo(tmp_path)
    _append(repo, "mod.py", CLEAN_LINE)

    result = salvage_owned_files(
        repo, ("mod.py",), tmp_path / "salvage", changed_files=["mod.py"], base_sha=base
    )
    assert result.saved == ()
    assert result.skipped == ("mod.py",)


def test_whole_file_rule_applies_without_base_text(tmp_path: Path) -> None:
    plain = tmp_path / "plain.py"
    plain.write_text("token = 'abcdefgh'\n", encoding="utf-8")
    assert _has_credential_content(plain) is True
    assert _has_credential_content(plain, lambda: None) is True


# ------------------------------------------------- R3: base from trusted state


def test_attempt_controlled_replace_ref_cannot_fabricate_the_base(tmp_path: Path) -> None:
    repo, base = _make_repo(tmp_path)
    attempt = tmp_path / "attempt"
    _git(repo, "worktree", "add", "-q", "-b", "attempt", str(attempt), base)
    (attempt / "mod.py").write_text(BASE_TEXT + SECRET_LINE + "\n", encoding="utf-8")
    # The attempt makes git report a base that already contains its credential line.
    base_blob = _git(attempt, "rev-parse", f"{base}:mod.py")
    fabricated = _git(attempt, "hash-object", "-w", "mod.py")
    _git(attempt, "replace", base_blob, fabricated)
    assert SECRET_LINE in _git(attempt, "show", f"{base}:mod.py")

    result = salvage_owned_files(
        attempt,
        ("mod.py",),
        tmp_path / "salvage",
        changed_files=["mod.py"],
        base_sha=base,
        repo_root=repo,
    )
    assert result.saved == ()
    assert result.skipped == ("mod.py",)
    assert SECRET_LINE not in repr(result)


# ------------------------------------------------ R4: lazy and capped base read


def test_base_is_read_only_when_redaction_changes_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base = _make_repo(tmp_path)
    reads: list[str] = []

    def fake_read(*args: object) -> str | None:
        reads.append("read")
        return None

    monkeypatch.setattr(salvage_module, "_read_base_text", fake_read)
    monkeypatch.setattr(salvage_module, "_read_worktree_base_text", fake_read)

    (repo / "clean.py").write_text("x = 1\n", encoding="utf-8")
    captured = salvage_owned_files(
        repo,
        ("clean.py",),
        tmp_path / "salvage",
        changed_files=["clean.py"],
        base_sha=base,
        repo_root=repo,
    )
    assert captured.saved == ("clean.py",)
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    restored = restore_salvaged_files(tmp_path / "salvage", fresh, ("clean.py",))
    assert restored.restored == ("clean.py",)
    assert reads == []

    _append(repo, "mod.py", CLEAN_LINE)
    salvage_owned_files(
        repo,
        ("mod.py",),
        tmp_path / "salvage-mod",
        changed_files=["mod.py"],
        base_sha=base,
        repo_root=repo,
    )
    assert reads == ["read"]


def test_base_larger_than_the_file_cap_is_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base = _make_repo(tmp_path, BASE_TEXT + "x = 1\n" * 60)
    (repo / "mod.py").write_text(BASE_TEXT + CLEAN_LINE + "\n", encoding="utf-8")

    assert salvage_module._read_base_text(repo, base, "mod.py") is not None
    kept = salvage_owned_files(
        repo,
        ("mod.py",),
        tmp_path / "salvage-kept",
        changed_files=["mod.py"],
        base_sha=base,
        repo_root=repo,
    )
    assert kept.saved == ("mod.py",)

    with monkeypatch.context() as patched:
        patched.setattr(salvage_module, "_MAX_FILE_BYTES", 200)
        assert salvage_module._read_base_text(repo, base, "mod.py") is None
        capped = salvage_owned_files(
            repo,
            ("mod.py",),
            tmp_path / "salvage-capped",
            changed_files=["mod.py"],
            base_sha=base,
            repo_root=repo,
        )
    assert capped.saved == ()
    assert capped.skipped == ("mod.py",)


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
        assert _has_credential_content(path, lambda text=base_text: text) is False, rel
