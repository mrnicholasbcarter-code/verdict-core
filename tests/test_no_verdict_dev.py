"""Guard against reintroducing the retired product domain (BOD-254).

The banned string is assembled at runtime so this file never contains it
literally; the CI grep and this test can then scan the whole repository with
no exclusions.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

BANNED = "verdict" + "." + "dev"
PATTERN = "verdict" + r"\." + "dev"
REPO = Path(__file__).resolve().parent.parent


def _grep(cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "grep", "-n", "-E", PATTERN], cwd=cwd, capture_output=True, text=True
    )


def test_no_retired_domain_in_repository() -> None:
    result = _grep(REPO)
    assert result.returncode == 1, (
        f"Found {BANNED!r} references (BOD-254); use "
        "github.com/mrnicholasbcarter-code/verdict-core instead:\n" + result.stdout
    )


def test_guard_catches_a_reintroduced_reference(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "note.txt").write_text(f"Visit https://{BANNED} for more info\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    result = _grep(tmp_path)
    assert result.returncode == 0
    assert BANNED in result.stdout


def test_ci_step_scans_without_exclusions() -> None:
    lint = (REPO / ".github" / "workflows" / "lint.yml").read_text()
    assert "git grep -n -E" in lint
    assert ":!" not in lint.split("retired-domain references")[1].split("fi")[0]
