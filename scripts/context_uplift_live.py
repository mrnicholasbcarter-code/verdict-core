#!/usr/bin/env python3
"""Opt-in LIVE context-uplift A/B on subscription capacity (BOD-272).

Why this shape:

* The dollar-savings bench cannot measure anything on subscription capacity: the
  frontier baseline costs $0/call and no free route is active.
* Cross-model token comparison on this gateway is invalid. Each model carries a
  large fixed gateway overhead and at least one counter is non-proportional
  (a 1-char prompt reports ~1347 prompt tokens on one model and ~4125 on
  another). So both arms here run on the SAME model.

The measurement is therefore a within-model A/B on the same tasks:

* ``nopack``: the task only;
* ``pack``: the task plus the hydrated context pack Verdict builds for it.

Quality is each task's own ``checks.must_contain`` against the real output. The
fixture's required phrases are project-local tokens that appear only in the
fixture workspace docs, so the ``nopack`` arm cannot satisfy them by guessing.
That is what makes the comparison discriminating rather than decorative.

This never claims dollar savings. Token deltas are only compared within a model.

Usage::

    VERDICT_LIVE_SMOKE=1 python scripts/context_uplift_live.py \
        --model kr/claude-haiku-4.5 --out /tmp/uplift.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from verdict.fixture_paths import resolve_fixture_path, resolve_fixture_workspace
from verdict.savings_bench import _service
from verdict.savings_execution import (
    ArmRequest,
    canonical_input_hash,
    evaluate_output_against_checks,
)
from verdict.savings_live import omniroute_arm_executor

DEFAULT_UPLIFT_FIXTURE = "benchmarks/fixtures/uplift_tasks.json"


def _run_arm(execute: Any, request: ArmRequest, task: dict[str, Any], model: str) -> dict[str, Any]:
    """Execute one arm live and report observed evidence only."""
    started = time.monotonic()
    execution = execute(request)
    elapsed = round(time.monotonic() - started, 2)
    usage = execution.receipt.get("usage") or {}
    quality = evaluate_output_against_checks(task, execution)
    return {
        "arm": request.arm,
        "requested": model,
        "served_by": execution.completed_with or None,
        "status": execution.status_code,
        "passed": quality.passed,
        "misses": list(quality.misses),
        "tokens_in": usage.get("prompt_tokens"),
        "tokens_out": usage.get("completion_tokens"),
        "seconds": elapsed,
        "context_chars": len(request.context_envelope or ""),
    }


def _build_request(
    arm: str,
    model: str,
    task_id: str,
    prompt: str,
    criteria: tuple[str, ...],
    input_hash: str,
    envelope: str | None,
) -> ArmRequest:
    return ArmRequest(
        arm=arm,
        task_id=task_id,
        prompt=prompt,
        acceptance_criteria=criteria,
        input_hash=input_hash,
        model=model,
        context_envelope=envelope,
    )


def _delta(pack: dict[str, Any], nopack: dict[str, Any], key: str) -> int | None:
    """Within-model token delta; None when either side did not report usage."""
    left, right = pack.get(key), nopack.get(key)
    if isinstance(left, int) and isinstance(right, int):
        return left - right
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--model", required=True, help="One model, used for BOTH arms (within-model A/B)"
    )
    parser.add_argument("--fixture", default=DEFAULT_UPLIFT_FIXTURE)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    if os.environ.get("VERDICT_LIVE_SMOKE") != "1":
        print("refusing: set VERDICT_LIVE_SMOKE=1 (this spends real capacity)", file=sys.stderr)
        return 2

    path = resolve_fixture_path(args.fixture)
    fixture = json.loads(path.read_text())
    workspace = resolve_fixture_workspace(path, str(fixture["workspace"]))
    packer = _service(workspace)  # offline chooser: used only to BUILD the pack
    base = os.environ.get("OMNIROUTE_BASE_URL") or "http://127.0.0.1:20128"
    execute = omniroute_arm_executor(base, os.environ.get("OMNIROUTE_API_KEY") or None)

    rows: list[dict[str, Any]] = []
    for task in fixture["tasks"]:
        task_id = str(task["id"])
        prompt = str(task["prompt"])
        criteria = tuple(str(c) for c in task["acceptance_criteria"])
        input_hash = canonical_input_hash(prompt, criteria)
        decision = asyncio.run(packer.route(prompt, criticality="low"))
        receipt = decision.admit_receipt if isinstance(decision.admit_receipt, dict) else {}

        nopack = _run_arm(
            execute,
            _build_request("nopack", args.model, task_id, prompt, criteria, input_hash, None),
            task,
            args.model,
        )
        pack = _run_arm(
            execute,
            _build_request(
                "pack",
                args.model,
                task_id,
                prompt,
                criteria,
                input_hash,
                decision.context_pack_prompt,
            ),
            task,
            args.model,
        )
        pack["pack_state"] = receipt.get("pack_state")
        pack["pack_sources"] = [
            s.get("source_uri") for s in (receipt.get("included_sources") or [])
        ]
        rows.append(
            {
                "task_id": task_id,
                "nopack": nopack,
                "pack": pack,
                "tokens_in_delta": _delta(pack, nopack, "tokens_in"),
                "tokens_out_delta": _delta(pack, nopack, "tokens_out"),
            }
        )
        print(
            f"{task_id:<18} nopack {'PASS' if nopack['passed'] else 'FAIL'}"
            f" {nopack['tokens_in']}/{nopack['tokens_out']} tok"
            f" | pack {'PASS' if pack['passed'] else 'FAIL'}"
            f" {pack['tokens_in']}/{pack['tokens_out']} tok"
            f" (in {rows[-1]['tokens_in_delta']:+} ) state={pack['pack_state']}",
            flush=True,
        )

    nopack_passed = sum(1 for r in rows if r["nopack"]["passed"])
    pack_passed = sum(1 for r in rows if r["pack"]["passed"])
    deltas = [r["tokens_in_delta"] for r in rows if isinstance(r["tokens_in_delta"], int)]
    summary = {
        "schema": "verdict.context-uplift-live/v2",
        "design": "within-model A/B (same model both arms); cross-model token counts on this "
        "gateway are not comparable, so they are never compared here",
        "note": "live subscription capacity; quality from each task's own checks; no dollar claim. "
        f"n={len(rows)}: a pipeline + discrimination check, not a statistical result.",
        "model": args.model,
        "fixture": str(path),
        "tasks": len(rows),
        "nopack_passed": nopack_passed,
        "pack_passed": pack_passed,
        "quality_uplift": pack_passed - nopack_passed,
        "median_tokens_in_delta": sorted(deltas)[len(deltas) // 2] if deltas else None,
        "rows": rows,
    }
    print(
        f"\nmodel {args.model}: nopack {nopack_passed}/{len(rows)} passed; "
        f"pack {pack_passed}/{len(rows)} passed; "
        f"quality uplift {summary['quality_uplift']:+} task(s); "
        f"median prompt-token cost of the pack {summary['median_tokens_in_delta']}"
    )
    if args.out:
        args.out.write_text(json.dumps(summary, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
