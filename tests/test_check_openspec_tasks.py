"""scripts/check_openspec_tasks.py fails on open tasks in touched OpenSpec changes."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load() -> ModuleType:
    path = ROOT / "scripts" / "check_openspec_tasks.py"
    spec = importlib.util.spec_from_file_location("check_openspec_tasks", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tasks_check = _load()


def _commit(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    for args in (["add", "-A"], ["commit", "-qm", "x"]):
        subprocess.run(
            ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", *args],
            cwd=root,
            check=True,
        )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    _commit(tmp_path, {"openspec/changes/old/tasks.md": "- [ ] untouched debt\n"})
    subprocess.run(["git", "branch", "base"], cwd=tmp_path, check=True)
    return tmp_path


def _main(root: Path, *extra: str) -> int:
    return tasks_check.main(["--base", "base", "--root", str(root), *extra])


def test_touched_change_with_open_task_fails(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _commit(repo, {"openspec/changes/new/tasks.md": "- [x] done\n- [ ] still open\n"})
    assert _main(repo) == 1
    assert "openspec change new: 1 open task(s)" in capsys.readouterr().out
    assert _main(repo, "--wip") == 0


def test_ticked_change_and_untouched_debt_pass(repo: Path) -> None:
    _commit(repo, {"openspec/changes/new/tasks.md": "- [x] done\n"})
    assert _main(repo) == 0


def test_touched_change_without_tasks_fails(repo: Path) -> None:
    _commit(repo, {"openspec/changes/new/proposal.md": "# New\n"})
    assert _main(repo) == 1


def test_archived_changes_are_not_checked(repo: Path) -> None:
    _commit(repo, {"openspec/changes/archive/2026-10-13-new/tasks.md": "- [ ] open\n"})
    assert _main(repo) == 0


def test_repository_changes_validate_strictly() -> None:
    """The pinned CLI is the CI authority; locally it runs when installed."""
    if shutil.which("openspec") is None:
        pytest.skip("openspec CLI not installed")
    result = subprocess.run(
        ["openspec", "validate", "--all", "--strict", "--no-interactive"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={"OPENSPEC_TELEMETRY": "0", "PATH": os.environ["PATH"]},
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
