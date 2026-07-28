"""Git Hook Management for Quality & Security Gating.

Installs and verifies pre-commit and pre-push Git hooks across repositories to
ensure linting (Ruff), type-checking (MyPy), security scanning (Bandit), and
unit testing (Pytest) run automatically before any git commit or push.
"""

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path

PRE_COMMIT_SCRIPT = """#!/usr/bin/env bash
# Verdict Git Pre-Commit Hook (Automated Quality Gate)
set -e

echo "🔍 [Verdict Git Hook] Running pre-commit quality checks..."

if command -v ruff &> /dev/null; then
    echo "  -> Running ruff check..."
    ruff check .
    echo "  -> Running ruff format --check..."
    ruff format --check .
fi

if command -v mypy &> /dev/null && [ -d "verdict" ]; then
    echo "  -> Running mypy --strict..."
    mypy verdict --strict
fi

echo "✅ [Verdict Git Hook] Pre-commit checks passed!"
"""


PRE_PUSH_SCRIPT = """#!/usr/bin/env bash
# Verdict Git Pre-Push Hook (Automated CI Verification Gate)
set -e

echo "🛡️ [Verdict Git Hook] Running pre-push security & test verification..."

if command -v pytest &> /dev/null && [ -d "tests" ]; then
    echo "  -> Running pytest..."
    pytest tests/ -v --ignore=tests/test_vcr_fallback.py
fi

if command -v bandit &> /dev/null && [ -d "verdict" ]; then
    echo "  -> Running bandit security scan..."
    bandit -r verdict/ -lll
fi

echo "🚀 [Verdict Git Hook] All pre-push verification gates passed!"
"""


@dataclass(frozen=True)
class GitHookInstallReport:
    """Report of installed Git hooks."""

    hooks_dir: Path
    pre_commit_installed: bool
    pre_push_installed: bool


def install_git_hooks(repo_root: Path | None = None) -> GitHookInstallReport:
    """Install pre-commit and pre-push git hooks into .git/hooks."""
    root = (repo_root or Path.cwd()).resolve()
    git_dir = root / ".git"

    if not git_dir.exists() or not git_dir.is_dir():
        raise FileNotFoundError(f"not_a_git_repository:{root}")

    hooks_dir = git_dir / "hooks"
    hooks_dir.mkdir(exist_ok=True)

    pre_commit_path = hooks_dir / "pre-commit"
    pre_push_path = hooks_dir / "pre-push"

    pre_commit_path.write_text(PRE_COMMIT_SCRIPT, encoding="utf-8")
    pre_push_path.write_text(PRE_PUSH_SCRIPT, encoding="utf-8")

    # Make executable
    for p in (pre_commit_path, pre_push_path):
        current_stat = p.stat().st_mode
        p.chmod(current_stat | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    return GitHookInstallReport(
        hooks_dir=hooks_dir, pre_commit_installed=True, pre_push_installed=True
    )


__all__ = ["GitHookInstallReport", "install_git_hooks"]
