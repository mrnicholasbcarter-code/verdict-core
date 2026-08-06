"""Every shipped module must be in git.

An unanchored ``patch_*.py`` ignore rule once excluded
``verdict/patch_executor.py``, so the branch committed the executor's tests
without the executor and a fresh clone could not import ``verdict``.  The
working tree passed every check; only a clone failed.  This test closes that
gap by asking git directly.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _git(*args: str) -> str:
    result = subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        pytest.skip(f"git unavailable or not a repository: {result.stderr.strip()}")
    return result.stdout


def test_every_verdict_module_is_tracked() -> None:
    tracked = set(_git("ls-files", "verdict").splitlines())
    on_disk = {
        str(path.relative_to(REPO))
        for path in (REPO / "verdict").rglob("*.py")
        if "__pycache__" not in path.parts
    }

    untracked = sorted(on_disk - tracked)
    assert not untracked, (
        f"these modules ship in the package but are not in git: {untracked}. "
        "Check .gitignore for an unanchored pattern."
    )
