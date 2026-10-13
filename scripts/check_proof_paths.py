#!/usr/bin/env python3
"""Proof-path guard: committed evidence under docs/proof/ changes only on purpose.

Every non-merge commit in ``--base..HEAD`` that changes a file under
``docs/proof/`` must name the changed bundle in a ``Proof: <path>`` line of its
message (one line per bundle, for example ``Proof: docs/proof/demo-run``). A
changed file outside every named path fails. Each named bundle that holds a
``receipt.json`` must still verify (``verdict run-receipt`` integrity).

Regenerate a bundle with its generator, for example
``python scripts/demo_orchestrate.py --out docs/proof/demo-run --update-proof``,
then commit with the trailer.

Usage:
    scripts/check_proof_paths.py --base origin/main
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PROOF_DIR = "docs/proof/"
TRAILER = re.compile(r"^Proof:\s*(docs/proof/\S+?)/?\s*$", re.M)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout


def _bundles(root: Path, paths: set[str]) -> set[str]:
    """The run bundles (directories with ``receipt.json``) that contain ``paths``."""
    bundles = set()
    for path in paths:
        parent = PurePosixPath(path).parent
        while str(parent).startswith(PROOF_DIR):
            if (root / parent / "receipt.json").is_file():
                bundles.add(str(parent))
                break
            parent = parent.parent
    return bundles


def check(root: Path, base: str) -> list[str]:
    """Return every proof change in ``base..HEAD`` that is not declared or does not verify."""
    from verdict.orchestration.receipt import verify_run_receipt

    errors: list[str] = []
    changed_proof: set[str] = set()
    for sha in _git(root, "rev-list", "--no-merges", f"{base}..HEAD").split():
        changed = _git(root, "diff-tree", "--no-commit-id", "--name-only", "-r", sha).split()
        proof = [path for path in changed if path.startswith(PROOF_DIR)]
        if not proof:
            continue
        changed_proof.update(proof)
        named = TRAILER.findall(_git(root, "log", "-1", "--format=%B", sha))
        if not named:
            errors.append(
                f"{sha[:12]} changes {len(proof)} file(s) under {PROOF_DIR} without a "
                f"'Proof: <path>' line, e.g. {proof[0]}"
            )
            continue
        outside = [p for p in proof if not any(p == n or p.startswith(n + "/") for n in named)]
        if outside:
            errors.append(f"{sha[:12]}: not under a named Proof: path: {', '.join(outside[:5])}")
    # Every bundle that holds a changed file must still verify, whatever the trailer names.
    for bundle in sorted(_bundles(root, changed_proof)):
        problems = verify_run_receipt(root / bundle)
        if problems:
            errors.append(f"{bundle}: receipt does not verify: {'; '.join(problems)}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--base", required=True, help="git ref the branch is compared with")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    errors = check(args.root.resolve(), args.base)
    for error in errors:
        print(f"proof-paths: {error}")
    print(f"check_proof_paths: {'FAIL' if errors else 'ok'} ({len(errors)} problem(s))")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
