#!/usr/bin/env python3
"""Fail if obsolete Ruflo/swarm/hivemind architecture reappears under verdict/.

BOD-17 deletes these modules from Core (not relocate). BOD-131 tracks broader
canonical docs hygiene. This structural gate must fail closed on reintroduction.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERDICT = ROOT / "verdict"

# Exact filenames / prefixes that must not exist under verdict/.
FORBIDDEN_EXACT = frozenset(
    {
        "hivemind.py",
        "hive_workspace.py",
        "sona.py",
        "neural.py",
        "swarm.py",
        "lifecycle_controller.py",
        "workflow_compiler.py",
    }
)
FORBIDDEN_PREFIXES = ("ruflo_", "swarm_", "ruvector_")
FORBIDDEN_DIRS = frozenset({"experimental"})


def find_violations(verdict_root: Path = VERDICT) -> list[str]:
    violations: list[str] = []
    if not verdict_root.is_dir():
        return [f"missing verdict package root: {verdict_root}"]

    experimental = verdict_root / "experimental"
    if experimental.exists():
        violations.append(str(experimental.relative_to(ROOT)))

    for path in sorted(verdict_root.rglob("*")):
        rel = path.relative_to(ROOT)
        parts = rel.parts
        if any(part in FORBIDDEN_DIRS for part in parts):
            violations.append(str(rel))
            continue
        if not path.is_file() or path.suffix != ".py":
            continue
        name = path.name
        if name in FORBIDDEN_EXACT:
            violations.append(str(rel))
            continue
        if any(name.startswith(prefix) for prefix in FORBIDDEN_PREFIXES):
            violations.append(str(rel))
    return sorted(set(violations))


def main(argv: list[str] | None = None) -> int:
    _ = argv
    violations = find_violations()
    if violations:
        print("Obsolete architecture paths must not exist under verdict/:", file=sys.stderr)
        for item in violations:
            print(f"  - {item}", file=sys.stderr)
        return 1
    print("OK: no obsolete Ruflo/swarm/hivemind architecture under verdict/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
