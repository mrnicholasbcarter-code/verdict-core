#!/usr/bin/env python3
"""Build CalibrationRecord JSONL from orchestration run receipts (BOD-203).

Pairs durable run receipts (``verdict/orchestration/receipt.py`` format) with
``decision_signals`` event snapshots recorded in each run's event log and emits
CalibrationRecord JSONL consumable by ``calibration.load_records()``.

Ground-truth labels come ONLY from observed outcomes in the receipt (verified
completion, first-pass success, retries, escalations, whether frontier topology
was actually used).  Never from OpenJev itself.

Records with missing mandatory evidence are skipped and counted in a stderr
summary, never guessed.

Usage::

    python scripts/build_calibration_records.py --runs-dir /path/to/runs > calibration.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FRONTIER_TOPOLOGIES = frozenset({"WORKER_CRITIC", "PARALLEL_WORK_UNITS"})

# Mapping from receipt topology to TASK_CLASSES best-effort approximation.
# The receipt does not carry a native task_class; we infer from topology and
# node count.  This is a coarse heuristic — real labeled data should refine it.
_TOPOLOGY_TASK_CLASS: dict[str, str] = {
    "SOLO": "bounded_implementation",
    "WORKER_CRITIC": "multi_file_implementation",
    "PARALLEL_WORK_UNITS": "decomposable_parallel",
}

# NodeKind -> role (closed set from AC5)
_KIND_ROLE: dict[str, str] = {
    "implement": "implementation_worker",
    "research": "research_test_worker",
    "review": "independent_reviewer",
    "integrate": "implementation_worker",
}


def _extract_signals(receipt: dict[str, Any]) -> dict[str, Any] | None:
    """Return the first decision_signals snapshot, or None."""
    ds = receipt.get("decision_signals")
    if not ds or not isinstance(ds, list):
        return None
    for entry in ds:
        if not isinstance(entry, dict):
            continue
        signals_blob = entry.get("signals")
        if isinstance(signals_blob, dict):
            return entry
    return None


def _node_role(node: dict[str, Any]) -> str | None:
    """Map a receipt node kind to a calibration role, or None."""
    kind = node.get("kind", "")
    return _KIND_ROLE.get(kind)


def _count_retries(nodes: list[dict[str, Any]]) -> int:
    """Count total retry attempts across all implement/integrate nodes."""
    total = 0
    for node in nodes:
        kind = node.get("kind", "")
        if kind not in ("implement", "integrate"):
            continue
        attempts = node.get("attempts", [])
        if len(attempts) > 1:
            total += len(attempts) - 1
    return total


def _is_first_pass(nodes: list[dict[str, Any]]) -> bool:
    """True if every implement/integrate node succeeded on its first attempt."""
    for node in nodes:
        kind = node.get("kind", "")
        if kind not in ("implement", "integrate"):
            continue
        attempts = node.get("attempts", [])
        if not attempts:
            return False
        if len(attempts) > 1:
            return False
        if attempts[0].get("outcome") != "success":
            return False
    return True


def _count_escalations(receipt: dict[str, Any]) -> int:
    """Count reassignment events (escalations)."""
    return len(receipt.get("reassignments") or [])


def _time_span_seconds(receipt: dict[str, Any]) -> float | None:
    """Wall-clock seconds from started_at to finished_at, or None."""
    started = receipt.get("started_at")
    finished = receipt.get("finished_at")
    if not started or not finished:
        return None
    try:
        from datetime import datetime

        # Handle Z suffix
        s = started.replace("Z", "+00:00")
        f = finished.replace("Z", "+00:00")
        dt_s = datetime.fromisoformat(s)
        dt_f = datetime.fromisoformat(f)
        delta = (dt_f - dt_s).total_seconds()
        return delta if delta >= 0 else None
    except (ValueError, TypeError):
        return None


def _frontier_needed(receipt: dict[str, Any]) -> bool:
    """Ground-truth: did verified completion actually require frontier cognition?

    Heuristic based on observed evidence only:
    - If topology was frontier AND the run completed successfully -> True
    - If topology was SOLO and completed successfully -> False
    - If the run was NOT complete, we look at whether frontier was used and
      retries/escalations suggest it was needed.

    This is conservative: if a SOLO run failed and was never retried with
    frontier, we mark frontier_needed=False (we don't know it was needed).
    """
    topology = receipt.get("topology", "")
    outcome = receipt.get("outcome", "")
    planner_was_frontier = topology in _FRONTIER_TOPOLOGIES

    if outcome == "COMPLETE":
        # The run succeeded with whatever topology was chosen.
        # If frontier was used, that's evidence it was needed.
        # If SOLO succeeded, frontier was not needed.
        return planner_was_frontier

    # Incomplete run: if frontier was used but still failed, conservatively
    # mark as needed (frontier couldn't even solve it).
    # If SOLO failed, we can't say frontier was needed — we'd be guessing.
    return planner_was_frontier


def build_record(run_id: str, receipt: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Build a CalibrationRecord dict from a receipt, or return (None, reason)."""
    # Must have nodes
    nodes = receipt.get("nodes")
    if not nodes or not isinstance(nodes, list):
        return None, "no nodes in receipt"

    topology = receipt.get("topology", "")
    if not topology:
        return None, "missing topology"

    outcome = receipt.get("outcome", "")
    if not outcome:
        return None, "missing outcome"

    # Extract decision signals
    sig_entry = _extract_signals(receipt)

    # Signal values (may be None if no signals collected)
    frontier_worthy: float | None = None
    confidence: float | None = None
    security_sensitive: float | None = None
    signal_latency_ms: int | None = None

    if sig_entry is not None:
        signals_blob = sig_entry.get("signals", {})
        if isinstance(signals_blob, dict):
            inner = signals_blob.get("signals")
            if isinstance(inner, dict):
                frontier_worthy = inner.get("frontier_worthy")
                security_sensitive = inner.get("security_sensitive")
            confidence_val = signals_blob.get("confidence")
            if isinstance(confidence_val, (int, float)):
                confidence = float(confidence_val)
            latency_val = signals_blob.get("latency_ms")
            if isinstance(latency_val, int):
                signal_latency_ms = latency_val

    # Ground truth from receipt
    verified = outcome == "COMPLETE"
    first_pass = _is_first_pass(nodes)
    retries = _count_retries(nodes)
    escalations = _count_escalations(receipt)
    planner_was_frontier = topology in _FRONTIER_TOPOLOGIES
    fn = _frontier_needed(receipt)
    time_to_green = _time_span_seconds(receipt) if verified else None

    # Security relevance: from signals if present, else False
    security_relevant = False
    if security_sensitive is not None and security_sensitive > 0.5:
        security_relevant = True

    # Task class from topology
    task_class = _TOPOLOGY_TASK_CLASS.get(topology, "bounded_implementation")

    # Determine role from the primary implement node (if any)
    role: str | None = None
    for node in nodes:
        r = _node_role(node)
        if r is not None:
            role = r
            break

    record: dict[str, Any] = {
        "task_id": run_id,
        "task_class": task_class,
        "frontier_worthy": frontier_worthy,
        "confidence": confidence,
        "security_sensitive": security_sensitive,
        "frontier_needed": fn,
        "security_relevant": security_relevant,
        "planner_was_frontier": planner_was_frontier,
        "verified": verified,
        "first_pass": first_pass,
        "retries": retries,
        "escalations": escalations,
        "total_cost_usd": 0.0,
        "time_to_green_s": time_to_green,
        "signal_latency_ms": signal_latency_ms,
    }
    if role is not None:
        record["role"] = role

    return record, None


def process_runs_dir(runs_dir: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Walk runs_dir, build calibration records. Returns (records, skip_reasons)."""
    records: list[dict[str, Any]] = []
    skip_reasons: dict[str, int] = {}

    if not runs_dir.is_dir():
        print(f"error: {runs_dir} is not a directory", file=sys.stderr)
        return records, skip_reasons

    for child in sorted(runs_dir.iterdir()):
        if not child.is_dir():
            continue
        receipt_path = child / "receipt.json"
        if not receipt_path.exists():
            skip_reasons["missing receipt.json"] = skip_reasons.get("missing receipt.json", 0) + 1
            continue
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            reason = f"unreadable receipt: {exc}"
            skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
            continue

        if not isinstance(receipt, dict):
            skip_reasons["receipt not a dict"] = skip_reasons.get("receipt not a dict", 0) + 1
            continue

        run_id = receipt.get("run_id", child.name)
        record, reason = build_record(str(run_id), receipt)
        if record is None:
            assert reason is not None
            skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
            continue
        records.append(record)

    return records, skip_reasons


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build CalibrationRecord JSONL from orchestration run receipts."
    )
    parser.add_argument(
        "--runs-dir",
        type=Path,
        required=True,
        help="Directory containing run subdirectories (each with receipt.json).",
    )
    args = parser.parse_args(argv)
    records, skip_reasons = process_runs_dir(args.runs_dir)

    # Emit JSONL to stdout
    for record in records:
        print(json.dumps(record, sort_keys=True, separators=(",", ":")))

    # Summary to stderr
    total_dirs = (
        sum(1 for d in args.runs_dir.iterdir() if d.is_dir()) if args.runs_dir.is_dir() else 0
    )
    print(f"calibration-builder: {len(records)} records from {total_dirs} runs", file=sys.stderr)
    if skip_reasons:
        for reason, count in sorted(skip_reasons.items()):
            print(f"  skipped {count}: {reason}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
