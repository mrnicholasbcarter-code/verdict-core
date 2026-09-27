#!/usr/bin/env python3
"""Opt-in LIVE smoke: one real Verdict orchestration through OmniRoute + Prime (BOD-268).

Spends real subscription capacity, so it never runs by default or in CI. It:

1. creates a throwaway git repo with a one-node WorkGraph (write smoke.txt);
2. runs ``verdict orchestrate --graph`` scoped to ``--scope`` routes, with a
   synthetic route-scoped 5xx fault injected on the node's FIRST attempt, so
   the run must classify it, cool only that route in a per-run state file
   (real capacity is never cooled) and reassign the SAME node to another
   healthy route. (A provider-scoped fault with a single-provider scope
   correctly fails closed: no alternate provider exists.);
3. verifies the run receipt digest and prints the evidence chain:
   selection -> dispatch -> failure -> cooldown -> reassign -> terminal ->
   verify -> review -> outcome.

Usage::

    VERDICT_LIVE_SMOKE=1 python scripts/live_failover_smoke.py --scope kr/ --out /tmp/smoke-run

Exit 0 only when the run is COMPLETE, the receipt verifies, and the fault
really caused a reassignment.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

GRAPH = {
    "goal": "live failover smoke: write smoke.txt",
    "max_parallel": 1,
    "nodes": [
        {
            "node_id": "smoke",
            "objective": "Create the file smoke.txt containing exactly the line: ok",
            "owned_files": ["smoke.txt"],
            "verification_command": ["sh", "-c", "grep -qx ok smoke.txt"],
            "risk": "low",
        }
    ],
}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _events(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--scope", default="kr/", help="Route prefixes allowed (default kr/)")
    parser.add_argument("--out", type=Path, help="Copy the finished run directory here")
    parser.add_argument("--attempt-timeout", type=float, default=600)
    args = parser.parse_args(argv)
    if os.environ.get("VERDICT_LIVE_SMOKE") != "1":
        print("refusing: set VERDICT_LIVE_SMOKE=1 (this spends real capacity)", file=sys.stderr)
        return 2

    work = Path(tempfile.mkdtemp(prefix="verdict-live-smoke-"))
    repo, runs = work / "repo", work / "runs"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "smoke@localhost")
    _git(repo, "config", "user.name", "smoke")
    (repo / "README.md").write_text("live smoke\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    graph_path = work / "graph.json"
    graph_path.write_text(json.dumps(GRAPH))
    state = work / "smoke-health.json"

    cmd = [
        sys.executable, "-m", "verdict.cli", "orchestrate",
        "--repo", str(repo), "--graph", str(graph_path), "--runs-dir", str(runs),
        "--scope", args.scope, "--max-parallel", "1",
        "--attempt-timeout", str(args.attempt_timeout),
        "--state-file", str(state),
        "--inject", "@smoke=server",
        "--plain",
    ]  # fmt: skip
    print("$", " ".join(cmd), flush=True)
    code = subprocess.run(cmd, cwd=Path(__file__).resolve().parent.parent).returncode

    run_dirs = sorted(p for p in runs.iterdir() if p.is_dir()) if runs.exists() else []
    if not run_dirs:
        print("FAIL: no run directory was created", file=sys.stderr)
        return 1
    run_dir = run_dirs[-1]
    events = _events(run_dir)
    chain = [
        e for e in events
        if e["type"] in {"selection", "dispatch", "failure", "cooldown", "reassign",
                         "terminal", "verify", "review", "review_attempt", "run_finished"}
    ]  # fmt: skip
    print("\nevidence chain:")
    for e in chain:
        data = {k: v for k, v in e.get("data", {}).items() if k in {
            "route_id", "category", "scope", "from_route", "to_route", "ok", "status",
            "reviewer", "outcome", "reason", "attempt", "key", "fault_injected"}}  # fmt: skip
        print(f"  {e['type']:<15} {e.get('node_id') or '-':<8} {data}")

    verify = subprocess.run(
        [sys.executable, "-m", "verdict.cli", "run-receipt", str(run_dir.name),
         "--runs-dir", str(runs)],
        cwd=Path(__file__).resolve().parent.parent, capture_output=True, text=True,
    )  # fmt: skip
    print("\n" + verify.stdout.strip())
    if args.out:
        shutil.rmtree(args.out, ignore_errors=True)
        shutil.copytree(run_dir, args.out)
        print(f"run copied to {args.out}")

    reassigned = any(e["type"] == "reassign" for e in events)
    finished = [e for e in events if e["type"] == "run_finished"]
    outcome = finished[-1]["data"].get("outcome") if finished else None
    ok = code == 0 and outcome == "COMPLETE" and verify.returncode == 0 and reassigned
    print(f"\nresult: {'PASS' if ok else 'FAIL'} (exit {code}, outcome {outcome}, "
          f"receipt {'ok' if verify.returncode == 0 else 'FAILED'}, reassigned {reassigned})")  # fmt: skip
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
