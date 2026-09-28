"""SONA outcome records derived from orchestration receipts.

Each node in a completed run produces one ``SONAOutcomeRecord``.  Cancelled,
timed-out and fail-closed nodes produce **negative** records (outcome !=
``accepted``) so the learning pipeline never silently drops failures.

``build_sona_records`` is called once by ``write_run_receipt`` and persisted as
``sona-outcomes.jsonl`` beside the run's event log.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

SONA_FILE = "sona-outcomes.jsonl"


@dataclass(frozen=True)
class SONAOutcomeRecord:
    """One node's learning signal extracted from a run receipt."""

    run_id: str
    node_id: str
    role: str  # node kind: implement / research / integrate / review
    outcome: str  # accepted / rejected / cancelled / timeout

    # Task classification (may be absent)
    task_class: str | None = None

    # Context strategy
    context_strategy: str | None = None  # pack_state if recorded
    budget_tier: str | None = None

    # Per-attempt model choices
    chosen_models: tuple[str, ...] = ()

    # Tools / skills (may be absent)
    tools_used: tuple[str, ...] | None = None

    # Proof / verification
    proof_pass: bool | None = None
    verified_commands: tuple[str, ...] = ()

    # Repair metrics
    repairs: int = 0
    retries: int = 0
    reroutes: int = 0

    # CI failures (may be absent)
    ci_failures: int | None = None

    # Timing
    latency_seconds: float | None = None

    # Token usage (sum across all attempts)
    input_tokens: int = 0
    output_tokens: int = 0

    # Cost (None unless every attempt reported a real cost)
    cost_usd: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe dict; tuple fields become lists."""
        raw = asdict(self)
        for key in ("chosen_models", "tools_used", "verified_commands"):
            val = raw.get(key)
            if isinstance(val, tuple):
                raw[key] = list(val)
        return raw


def _node_outcome(final_state: str, claimed_outcome: str) -> str:
    """Map receipt final_state to a SONA outcome label."""
    accepted_states = {"VALIDATED", "TERMINAL_SUCCESS"}
    rejected_states = {"TERMINAL_FAILURE", "REJECTED", "BLOCKED"}
    if final_state in accepted_states:
        return "accepted"
    if final_state in rejected_states:
        return "rejected"
    if final_state == "PLANNED":
        # Node was never dispatched — treat as cancelled
        return "cancelled"
    if final_state == "RUNNING":
        # Still running when receipt was built → timeout
        return "timeout"
    # ADMITTED / DISPATCHED without terminal → cancelled
    return "cancelled"


def _sum_usage(attempts: list[dict[str, Any]]) -> tuple[int, int, float | None]:
    """Sum token usage and cost across attempts.

    Returns (input_tokens, output_tokens, cost_usd).
    cost_usd is None unless *every* attempt reported a numeric cost and at
    least one attempt exists.  A reported cost of 0.0 from a provider is
    accepted; ``None`` (missing) is not.
    """
    inp = 0
    out = 0
    cost: float = 0.0
    all_have_cost = bool(attempts)
    for att in attempts:
        usage = att.get("usage")
        if isinstance(usage, dict):
            inp += int(usage.get("input_tokens") or 0)
            out += int(usage.get("output_tokens") or 0)
            raw_cost = usage.get("cost_usd")
            if raw_cost is None or not isinstance(raw_cost, int | float):
                all_have_cost = False
            else:
                cost += float(raw_cost)
        else:
            all_have_cost = False
    # Never return 0.0 for unknown cost
    final_cost: float | None = cost if all_have_cost else None
    if final_cost is not None and final_cost == 0.0 and not all_have_cost:
        final_cost = None  # pragma: no cover — belt-and-suspenders
    return inp, out, final_cost


def build_sona_records(receipt: dict[str, Any]) -> list[SONAOutcomeRecord]:
    """Build one ``SONAOutcomeRecord`` per node from a run receipt.

    Cancelled, timed-out and fail-closed nodes produce **negative** records.
    """
    run_id: str = str(receipt.get("run_id", ""))
    claimed_outcome: str = str(receipt.get("claimed_outcome", ""))
    reassignments = receipt.get("reassignments") or []

    # Count reroutes per node
    reroutes_by_node: dict[str, int] = {}
    for r in reassignments:
        nid = str(r.get("node_id", ""))
        if nid:
            reroutes_by_node[nid] = reroutes_by_node.get(nid, 0) + 1

    records: list[SONAOutcomeRecord] = []
    for node in receipt.get("nodes") or []:
        node_id: str = str(node.get("node_id", ""))
        kind: str = str(node.get("kind", ""))
        final_state: str = str(node.get("final_state", ""))
        attempts: list[dict[str, Any]] = node.get("attempts") or []

        outcome = _node_outcome(final_state, claimed_outcome)

        # Chosen models per attempt
        chosen_models = tuple(str(a.get("route_id", "")) for a in attempts if a.get("route_id"))

        # Proof / verification
        validated_by = node.get("validated_by") or []
        proof_pass: bool | None = None
        verified_cmds: tuple[str, ...] = ()
        if validated_by:
            proof_pass = True
            verified_cmds = tuple(
                str(v.get("command", "")) for v in validated_by if v.get("command")
            )
        elif final_state in {"TERMINAL_FAILURE", "REJECTED", "BLOCKED"}:
            proof_pass = False

        # Repairs = attempts with outcome=failure that were followed by another attempt
        failures = sum(1 for a in attempts if a.get("outcome") == "failure")
        repairs = failures  # each failure that led to a retry is a repair

        retries = max(0, len(attempts) - 1)
        reroutes = reroutes_by_node.get(node_id, 0)

        # Latency: sum of attempt durations
        latency: float | None = None
        durations = [
            float(a["duration_seconds"])
            for a in attempts
            if isinstance(a.get("duration_seconds"), int | float)
        ]
        if durations:
            latency = sum(durations)

        # Token usage
        inp, out, cost = _sum_usage(attempts)

        records.append(
            SONAOutcomeRecord(
                run_id=run_id,
                node_id=node_id,
                role=kind,
                outcome=outcome,
                task_class=None,  # not yet in receipt
                context_strategy=None,  # not yet in receipt
                budget_tier=None,  # not yet in receipt
                chosen_models=chosen_models,
                tools_used=None,  # not yet in receipt
                proof_pass=proof_pass,
                verified_commands=verified_cmds,
                repairs=repairs,
                retries=retries,
                reroutes=reroutes,
                ci_failures=None,  # not yet in receipt
                latency_seconds=latency,
                input_tokens=inp,
                output_tokens=out,
                cost_usd=cost,
            )
        )

    return records


def write_sona_outcomes(run_dir: Path, receipt: dict[str, Any]) -> Path:
    """Write ``sona-outcomes.jsonl`` beside the run's event log."""
    records = build_sona_records(receipt)
    target = run_dir / SONA_FILE
    lines = [json.dumps(r.to_dict(), sort_keys=True, separators=(",", ":")) for r in records]
    target.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return target
