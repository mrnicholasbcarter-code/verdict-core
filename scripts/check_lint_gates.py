#!/usr/bin/env python3
"""Run the checks of CI's required ``lint`` job locally, before a push.

The ``lint`` job in ``.github/workflows/ci.yml`` runs more than ruff and mypy:
it also runs the reachability ratchet, the definition-of-done evidence check,
the proof-path trailer check, ``openspec validate`` and the open-task check
for touched OpenSpec changes. Those repo gates are not part of the operator's
pre-push gate, so a branch can pass every local check and still fail ``lint``
once a pull request opens. This script runs the same commands, in the same
order, against the same kind of base (the merge base with ``origin/main`` by
default, the pull request's base in CI).

``tests/test_check_lint_gates.py`` fails when ``ci.yml`` gains or drops a
check that this list does not mirror, so the two cannot drift apart silently.

The CI job also autofixes ruff findings on same-repository pull requests; this
script only checks, so run ``ruff check --fix . && ruff format .`` first if you
want the fixes.

Usage:
    scripts/check_lint_gates.py [--base REF] [--wip] [--keep-going]

``--wip`` mirrors the ``wip`` pull request label: open tasks in a touched
OpenSpec change are then allowed. Only use it when the PR carries that label.

Exit codes:
    0: every gate passed
    1: at least one gate failed (the summary names each one)
    2: usage error, or the base could not be resolved
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OPENSPEC = "@fission-ai/openspec@1.13.2"


@dataclass(frozen=True)
class Gate:
    name: str
    argv: tuple[str, ...]


def gates(base: str, *, wip: bool) -> list[Gate]:
    """Return the lint job's checks, in CI order, for one base commit."""
    py = sys.executable
    tasks = [py, "scripts/check_openspec_tasks.py", "--base", base]
    if wip:
        tasks.append("--wip")
    return [
        Gate("ruff check", (py, "-m", "ruff", "check", ".")),
        Gate("ruff format --check", (py, "-m", "ruff", "format", "--check", ".")),
        Gate("mypy verdict --strict", (py, "-m", "mypy", "verdict", "--strict")),
        Gate("reachability ratchet", (py, "scripts/check_reachability.py", "--ratchet-base", base)),
        Gate("definition of done", (py, "scripts/check_dod.py", "--all")),
        Gate("proof-path trailers", (py, "scripts/check_proof_paths.py", "--base", base)),
        Gate(
            "openspec validate",
            ("npx", "--yes", OPENSPEC, "validate", "--all", "--strict", "--no-interactive"),
        ),
        Gate("open OpenSpec tasks", tuple(tasks)),
    ]


def resolve_base(ref: str | None) -> str:
    """Return the base commit: ``ref`` as given, else the merge base with origin/main."""
    if ref:
        argv = ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"]
    else:
        argv = ["git", "merge-base", "HEAD", "origin/main"]
    result = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        hint = ref or "origin/main (run `git fetch origin main`)"
        raise SystemExit(f"check_lint_gates: cannot resolve base {hint}: {result.stderr.strip()}")
    return result.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", help="base commit (default: merge base with origin/main)")
    parser.add_argument("--wip", action="store_true", help="the PR carries the wip label")
    parser.add_argument(
        "--keep-going", action="store_true", help="run every gate even after one fails"
    )
    args = parser.parse_args(argv)

    try:
        base = resolve_base(args.base)
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 2

    failed: list[str] = []
    for gate in gates(base, wip=args.wip):
        if gate.argv[0] == "npx" and shutil.which("npx") is None:
            print(f"== {gate.name}: npx not found; install Node 22 to run it", file=sys.stderr)
            failed.append(f"{gate.name} (npx not found)")
            if not args.keep_going:
                break
            continue
        print(f"== {gate.name}", flush=True)
        if subprocess.run(gate.argv, cwd=ROOT).returncode != 0:
            failed.append(gate.name)
            if not args.keep_going:
                break

    if failed:
        print(f"check_lint_gates: FAILED against base {base[:12]}: {', '.join(failed)}")
        return 1
    print(f"check_lint_gates: all lint gates passed against base {base[:12]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
