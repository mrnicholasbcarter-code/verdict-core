#!/usr/bin/env python3
"""Gate drills: prove that each "stop false done" gate still fails on its fault.

For every drill, a scratch worktree of HEAD gets one fault and one commit, and
the gate that must catch it runs there with HEAD as its base. A drill passes
only when the gate exits non-zero and prints the expected reason. A control
run on the unchanged tree must pass every gate. The scheduled ``gate-drills``
workflow runs this on main, so a gate that silently stops working turns red.

Usage:
    scripts/drill_gates.py [--openspec "npx --yes @fission-ai/openspec@1.13.2"]
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GIT_ID = ["-c", "user.email=drill@example.invalid", "-c", "user.name=gate-drill"]


@dataclass(frozen=True)
class Drill:
    name: str
    gate: str  # key into the gate commands
    expect: str
    fault: Callable[[Path], str | None]  # returns a commit-message suffix, or None to skip


def _active_changes(root: Path) -> list[Path]:
    changes = root / "openspec" / "changes"
    return sorted(p for p in changes.iterdir() if p.is_dir() and p.name != "archive")


def _orphan(root: Path) -> str:
    (root / "verdict" / "drill_orphan.py").write_text("VALUE = 1\n", encoding="utf-8")
    return ""


def _growing_baseline(root: Path) -> str:
    main = root / "verdict" / "__main__.py"
    main.write_text(
        main.read_text(encoding="utf-8") + "\n\ndef drill_unused() -> None:\n    pass\n"
    )
    path = root / "reachability-baseline.json"
    baseline = json.loads(path.read_text(encoding="utf-8"))
    baseline.append({"qualified_name": "verdict.__main__.drill_unused", "reason": "WIRE: drill"})
    path.write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8")
    return ""


def _untriaged_touched(root: Path) -> str | None:
    for name in ("reachability-baseline.json", "reachability-modules-baseline.json"):
        for entry in json.loads((root / name).read_text(encoding="utf-8")):
            if entry["reason"].startswith("UNTRIAGED"):
                module = entry["qualified_name"]
                if name == "reachability-baseline.json":
                    module = module.rpartition(".")[0]
                for path in (
                    module.replace(".", "/") + ".py",
                    module.replace(".", "/") + "/__init__.py",
                ):
                    if (root / path).is_file():
                        with (root / path).open("a", encoding="utf-8") as handle:
                            handle.write("\n# drill: touch a module with UNTRIAGED entries\n")
                        return ""
    return None


def _forbidden(root: Path) -> str | None:
    rules = json.loads((root / "reachability-forbidden.json").read_text(encoding="utf-8"))
    if not rules:
        return None
    module, _, symbol = rules[0]["symbol"].rpartition(".")
    main = root / "verdict" / "__main__.py"
    main.write_text(
        main.read_text(encoding="utf-8") + f"\nfrom {module} import {symbol}\n\nDRILL = {symbol}\n"
    )
    return ""


def _openspec_invalid(root: Path) -> str | None:
    for change in _active_changes(root):
        marker = change / ".openspec.yaml"
        skips = marker.is_file() and "skip_specs" in marker.read_text(encoding="utf-8")
        if (change / "specs").is_dir() and not skips:
            shutil.rmtree(change / "specs")
            return ""
    return None


def _open_task(root: Path) -> str | None:
    for change in _active_changes(root):
        tasks = change / "tasks.md"
        if tasks.is_file():
            tasks.write_text(tasks.read_text(encoding="utf-8") + "- [ ] drill: open task\n")
            return ""
    return None


def _archive_without_evidence(root: Path) -> str | None:
    changes = _active_changes(root)
    if not changes:
        return None
    target = root / "openspec/changes/archive" / f"{date.today().isoformat()}-{changes[0].name}"
    shutil.move(str(changes[0]), str(target))
    return ""


def _proof_without_trailer(root: Path) -> str:
    index = root / "docs" / "proof" / "EVIDENCE_INDEX.md"
    index.write_text(index.read_text(encoding="utf-8") + "\n<!-- drill -->\n", encoding="utf-8")
    return ""


def _proof_tampered(root: Path) -> str:
    events = root / "docs" / "proof" / "demo-run" / "events.jsonl"
    text = events.read_text(encoding="utf-8")
    events.write_text(text.replace("quota_exhausted", "rate_limited", 1), encoding="utf-8")
    return "\n\nProof: docs/proof/demo-run"


DRILLS = (
    Drill("orphan-module", "reachability", "[NEW] orphan_module: verdict.drill_orphan", _orphan),
    Drill(
        "growing-baseline",
        "reachability",
        "new entry verdict.__main__.drill_unused",
        _growing_baseline,
    ),
    Drill(
        "untriaged-touched",
        "reachability",
        "UNTRIAGED in a module this branch changed",
        _untriaged_touched,
    ),
    Drill("forbidden-symbol", "reachability", "[FORBIDDEN]", _forbidden),
    Drill("openspec-invalid", "openspec", "1 failed", _openspec_invalid),
    Drill("openspec-open-task", "tasks", "- [ ] drill: open task", _open_task),
    Drill(
        "archive-no-evidence", "dod", "was archived without DoD evidence", _archive_without_evidence
    ),
    Drill("proof-no-trailer", "proof", "without a 'Proof: <path>' line", _proof_without_trailer),
    Drill("proof-tampered", "proof", "receipt does not verify", _proof_tampered),
)


def _gates(openspec: str, base: str) -> dict[str, list[str]]:
    python = sys.executable
    return {
        "reachability": [
            python,
            "scripts/check_reachability.py",
            "--skip-vulture",
            "--ratchet-base",
            base,
        ],
        "openspec": [*shlex.split(openspec), "validate", "--all", "--strict", "--no-interactive"],
        "tasks": [python, "scripts/check_openspec_tasks.py", "--base", base],
        "dod": [python, "scripts/check_dod.py", "--all"],
        "proof": [python, "scripts/check_proof_paths.py", "--base", base],
    }


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *GIT_ID, *args], cwd=cwd, check=True, capture_output=True)


def _run(cwd: Path, argv: list[str]) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "OPENSPEC_TELEMETRY": "0"}
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, env=env, check=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--openspec", default="openspec", help="command that runs the OpenSpec CLI")
    args = parser.parse_args(argv)
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()
    gates = _gates(args.openspec, base)
    failures = 0
    with tempfile.TemporaryDirectory(prefix="gate-drill-") as scratch:
        control = Path(scratch) / "control"
        _git(ROOT, "worktree", "add", "-q", "--detach", str(control), base)
        try:
            for gate, command in gates.items():
                result = _run(control, command)
                ok = result.returncode == 0
                failures += not ok
                print(f"control {gate}: {'pass' if ok else 'FAILED on the unchanged tree'}")
                if not ok:
                    print((result.stdout + result.stderr)[-2000:])
            for drill in DRILLS:
                work = Path(scratch) / drill.name
                _git(ROOT, "worktree", "add", "-q", "--detach", str(work), base)
                try:
                    suffix = drill.fault(work)
                    if suffix is None:
                        print(f"drill {drill.name}: skipped (nothing left to break)")
                        continue
                    _git(work, "add", "-A")
                    _git(work, "commit", "-q", "-m", f"drill: {drill.name}{suffix}")
                    result = _run(work, gates[drill.gate])
                    output = result.stdout + result.stderr
                    caught = result.returncode != 0 and drill.expect in output
                    failures += not caught
                    line = next((ln for ln in output.splitlines() if drill.expect in ln), "")
                    verdict = "FAILED as intended" if caught else "NOT CAUGHT"
                    print(
                        f"drill {drill.name}: {verdict} ({drill.gate} exit {result.returncode}) {line.strip()}"
                    )
                finally:
                    _git(ROOT, "worktree", "remove", "--force", str(work))
        finally:
            _git(ROOT, "worktree", "remove", "--force", str(control))
    print(
        f"gate drills: {'all drills failed as intended' if not failures else f'{failures} problem(s)'}"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
