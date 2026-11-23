#!/usr/bin/env python3
"""Vendored OpenSpec validator: run validate_openspec_change on every change touched by this branch.

Usage:
    scripts/check_openspec_vendor.py --base <ref> [--root <path>]

``--root`` overrides the repository root used to discover openspec/changes and to
run ``git diff``.  The vendored validator is always taken from the script's own
repository root so the validator itself is never substituted.

Exit codes:
    0: every touched change passed (or no changes were touched)
    1: at least one change failed
    2: usage error or git error
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path, PurePosixPath

# The script's own repository root — used to locate the vendored validator.
_SCRIPT_ROOT = Path(__file__).resolve().parents[1]
_VALIDATOR = _SCRIPT_ROOT / "verdict" / "openspec_vendor" / "validate_openspec_change.py"


def _touched_changes(root: Path, base: str) -> list[str]:
    """Return sorted active (non-archived) change ids touched since ``base``."""
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...HEAD", "--", "openspec/changes"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    ids: set[str] = set()
    for name in result.stdout.split():
        parts = PurePosixPath(name).parts
        # parts: ('openspec', 'changes', '<id>', ...)
        if len(parts) > 3 and parts[2] != "archive":
            ids.add(parts[2])
    return sorted(change for change in ids if (root / "openspec" / "changes" / change).is_dir())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--base", required=True, help="git ref the branch is compared with")
    parser.add_argument(
        "--root",
        type=Path,
        default=_SCRIPT_ROOT,
        help="repository root (default: this script's root)",
    )
    args = parser.parse_args(argv)

    root = args.root.resolve()
    changes = _touched_changes(root, args.base)
    failed = False

    for change in changes:
        change_dir = root / "openspec" / "changes" / change
        result = subprocess.run(
            [sys.executable, str(_VALIDATOR), str(change_dir)], capture_output=True, text=True
        )
        if result.returncode != 0:
            failed = True
            print(f"vendored validator FAIL: {change}")
            # Errors go to stderr in the vendored validator.
            for line in (result.stderr or result.stdout).splitlines():
                print(f"  {line}")
        else:
            print(f"vendored validator ok: {change}")

    n = len(changes)
    print(f"check_openspec_vendor: {n} touched change(s)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
