"""Tests for scripts/check_openspec_vendor.py."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_openspec_vendor.py"


def _run(base: str, root: Path | None = None) -> tuple[int, str]:
    cmd = [sys.executable, str(SCRIPT), "--base", base]
    if root is not None:
        cmd += ["--root", str(root)]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
    return result.returncode, result.stdout + result.stderr


def test_script_exists() -> None:
    assert SCRIPT.exists(), f"script not found: {SCRIPT}"


def test_no_touched_changes_passes() -> None:
    """With base=HEAD no changes are touched; script must exit 0."""
    code, out = _run("HEAD")
    assert code == 0
    assert "0 touched change(s)" in out


def test_missing_design_md_fails(tmp_path: Path) -> None:
    """A touched change that is missing design.md must cause exit 1."""
    # Minimal git repo so git-diff can compute touched changes.
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "T"], cwd=tmp_path, check=True, capture_output=True
    )

    change_dir = tmp_path / "openspec" / "changes" / "my-change"
    change_dir.mkdir(parents=True)

    proposal = textwrap.dedent(
        """\
        # my-change

        ## Why

        Test reason.

        ## What Changes

        Test description.

        ## Capabilities

        ### New Capabilities

        - **foo**: added.

        ## Impact

        None.

        ## Evidence boundary

        KNOWN: tests pass.

        ## Acceptance criteria

        - Works.
        """
    )
    tasks = textwrap.dedent(
        """\
        # Tasks

        ## 1. Implementation

        - [x] 1.1 Write tests first. Test added.
        """
    )

    (change_dir / "proposal.md").write_text(proposal)
    (change_dir / "tasks.md").write_text(tasks)
    # Intentionally omit design.md

    # Initial commit (base)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=tmp_path, check=True, capture_output=True)
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout.strip()

    # Touch the change so it shows up in `git diff base...HEAD`
    (change_dir / "proposal.md").write_text(proposal + "\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "touch"], cwd=tmp_path, check=True, capture_output=True)

    code, out = _run(base_sha, root=tmp_path)
    assert code == 1, f"expected exit 1 for missing design.md; got {code}; output:\n{out}"
    assert "design.md" in out, f"expected 'design.md' in output; got:\n{out}"


def test_valid_change_passes(tmp_path: Path) -> None:
    """A touched change with all required artifacts and correct content must pass."""
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "T"], cwd=tmp_path, check=True, capture_output=True
    )

    change_dir = tmp_path / "openspec" / "changes" / "my-change"
    change_dir.mkdir(parents=True)

    proposal = textwrap.dedent(
        """\
        # my-change

        ## Why

        Test reason.

        ## What Changes

        Test description.

        ## Capabilities

        ### New Capabilities

        - **foo**: added.

        ## Impact

        None.

        ## Evidence boundary

        KNOWN: tests pass.

        ## Acceptance criteria

        - Works.
        """
    )
    design = textwrap.dedent(
        """\
        # my-change design

        ## Context

        Test context.

        ## Goals / Non-Goals

        Goal: work. Non-goal: not scope.

        ## Decisions

        Use existing approach.

        ## Risks / Trade-offs

        No significant risk.

        ## Security / trust implications

        None beyond existing.

        ## Routing / context / memory implications

        No impact.

        ## Migration / rollback

        No migration required.

        ## ADR impact

        No ADR changes required.
        """
    )
    tasks = textwrap.dedent(
        """\
        # Tasks

        ## 1. Implementation

        - [x] 1.1 Write tests first. Test added and verified.
        """
    )

    (change_dir / "proposal.md").write_text(proposal)
    (change_dir / "design.md").write_text(design)
    (change_dir / "tasks.md").write_text(tasks)
    # No spec files in this minimal test; skip spec validation.
    (change_dir / ".openspec.yaml").write_text("skip_specs: true\n")

    # Initial commit (base)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=tmp_path, check=True, capture_output=True)
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout.strip()

    # Touch the change
    (change_dir / "proposal.md").write_text(proposal + "\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "touch"], cwd=tmp_path, check=True, capture_output=True)

    code, out = _run(base_sha, root=tmp_path)
    assert code == 0, f"expected exit 0 for valid change; got {code}; output:\n{out}"
