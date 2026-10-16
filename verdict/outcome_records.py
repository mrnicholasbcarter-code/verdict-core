"""Outcome records derived from orchestration run receipts.

SONA is the learning loop these records feed.  Each node in a completed
run produces one ``OutcomeRecord``.  Cancelled,
timed-out and fail-closed nodes produce **negative** records (outcome !=
``accepted``) so the learning pipeline never silently drops failures.

``build_outcome_records`` is called once by ``write_run_receipt`` and persisted as
``outcome-records.jsonl`` beside the run's event log.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

OUTCOME_RECORDS_FILE = "outcome-records.jsonl"


@dataclass(frozen=True)
class OutcomeRecord:
    """One node's learning signal extracted from a run receipt (SONA loop)."""

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


def build_outcome_records(receipt: dict[str, Any]) -> list[OutcomeRecord]:
    """Build one ``OutcomeRecord`` per node from a run receipt.

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

    records: list[OutcomeRecord] = []
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
            OutcomeRecord(
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


def write_outcome_records(run_dir: Path, receipt: dict[str, Any]) -> Path:
    """Write ``outcome-records.jsonl`` beside the run's event log."""
    records = build_outcome_records(receipt)
    target = run_dir / OUTCOME_RECORDS_FILE
    lines = [json.dumps(r.to_dict(), sort_keys=True, separators=(",", ":")) for r in records]
    target.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# BOD-353: cross-surface attempt records (ts, task_kind, model, outcome,
# classified failure). Distinct from the per-node SONA records above: an
# ``AttemptRecord`` is one model attempt by any harness (autodev, route,
# orchestrate, or the Prime controller's own children), not one orchestration
# node. Written as a flat, append-only JSONL store under VERDICT_HOME.
# ---------------------------------------------------------------------------

ATTEMPT_RECORDS_FILE = "outcome-attempts.jsonl"

#: Fixed outcome taxonomy. No caller may introduce a new value.
ATTEMPT_OUTCOMES: tuple[str, ...] = ("accepted", "failed", "partial", "abandoned")

#: Fixed failure-class taxonomy (BOD-353). ``success`` is the only class used
#: with outcome ``accepted``; every other class implies a non-accepted outcome.
ATTEMPT_FAILURE_CLASSES: tuple[str, ...] = (
    "success",
    "test_failed",
    "review_rejected",
    "quota_429",
    "model_cooldown",
    "stream_drop",
    "timeout",
    "tool_error",
    "spec_violation",
    "false_report",
    "provider_5xx",
    "auth_error",
    "other",
)


@dataclass(frozen=True)
class AttemptRecord:
    """One model attempt, classified against the fixed BOD-353 taxonomy."""

    ts: str
    task_kind: str
    harness: str
    model: str  # canonical provider/model id (no "omniroute/" prefix)
    outcome: str  # one of ATTEMPT_OUTCOMES
    failure_class: str  # one of ATTEMPT_FAILURE_CLASSES
    source: str
    effort: str | None = None
    duration_s: float | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    story_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _require_text(value: str, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} must not be blank")
    return text


def attempt_records_path(store_dir: Path | str) -> Path:
    """``<store_dir>/outcome-attempts.jsonl``."""
    return Path(store_dir) / ATTEMPT_RECORDS_FILE


def build_attempt_record(
    *,
    task_kind: str,
    harness: str,
    model: str,
    outcome: str,
    failure_class: str,
    source: str,
    effort: str | None = None,
    duration_s: float | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    cost_usd: float | None = None,
    story_id: str | None = None,
    ts: str | None = None,
) -> AttemptRecord:
    """Validate and canonicalize one attempt into an :class:`AttemptRecord`.

    ``model`` is canonicalized with :func:`verdict.admission.canonical_route_id`
    so ``omniroute/kiro/claude-haiku-4.5`` and ``kiro/claude-haiku-4.5`` record
    the same model id. ``outcome`` and ``failure_class`` must be members of the
    fixed BOD-353 taxonomy; any other value is a programmer error, not data to
    silently coerce.
    """
    from verdict.admission import canonical_route_id

    task_kind = _require_text(task_kind, "task_kind")
    harness = _require_text(harness, "harness")
    model = canonical_route_id(_require_text(model, "model"))
    source = _require_text(source, "source")

    outcome = _require_text(outcome, "outcome")
    if outcome not in ATTEMPT_OUTCOMES:
        raise ValueError(f"outcome must be one of {ATTEMPT_OUTCOMES}, got {outcome!r}")

    failure_class = _require_text(failure_class, "failure_class")
    if failure_class not in ATTEMPT_FAILURE_CLASSES:
        raise ValueError(
            f"failure_class must be one of {ATTEMPT_FAILURE_CLASSES}, got {failure_class!r}"
        )

    story_id = story_id.strip() if isinstance(story_id, str) and story_id.strip() else None
    effort = effort.strip() if isinstance(effort, str) and effort.strip() else None

    return AttemptRecord(
        ts=ts or datetime.now(timezone.utc).isoformat(),
        task_kind=task_kind,
        harness=harness,
        model=model,
        outcome=outcome,
        failure_class=failure_class,
        source=source,
        effort=effort,
        duration_s=duration_s,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost_usd,
        story_id=story_id,
    )


def write_attempt_record(record: AttemptRecord, *, store_dir: Path | str) -> Path:
    """Append one attempt record as a JSONL line under ``store_dir``."""
    target = attempt_records_path(store_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record.to_dict(), sort_keys=True, ensure_ascii=False) + "\n")
    return target


_STORY_ID_RE = re.compile(r"\b([A-Z]{2,}-\d+)\b")

_LEGACY_TASK_KIND_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("review", "review"),
    ("verify", "verify"),
    ("implement", "implement"),
    ("research", "research"),
    ("synthesis", "synthesis"),
    ("debug", "debug"),
    ("crawl", "crawl"),
    ("plan", "plan"),
)


def _legacy_task_kind(task_text: str) -> str:
    lowered = task_text.lower()
    for keyword, kind in _LEGACY_TASK_KIND_KEYWORDS:
        if keyword in lowered:
            return kind
    return "other"


def _classify_legacy_outcome(raw_outcome: str) -> tuple[str, str]:
    """Best-effort map of a free-text legacy outcome to (outcome, failure_class)."""
    text = (raw_outcome or "").strip().lower()
    if not text:
        return "abandoned", "other"
    if text.startswith("pending") or text.startswith("spawned") or text.startswith("planned"):
        return "abandoned", "other"
    if text.startswith("partial") or text.startswith("mixed"):
        return "partial", "other"
    if text.startswith("accepted"):
        return "accepted", "success"
    if text.startswith("failed") or text.startswith("parked"):
        if "429" in text or "quota" in text:
            return "failed", "quota_429"
        if "502" in text or "5xx" in text or "500" in text or "503" in text:
            return "failed", "provider_5xx"
        if "stream_drop" in text or "stream drop" in text:
            return "failed", "stream_drop"
        if "stall" in text or "exited" in text or "no output" in text:
            return "failed", "stream_drop"
        if "timeout" in text or "timed out" in text:
            return "failed", "timeout"
        if "cooldown" in text or "cooling" in text:
            return "failed", "model_cooldown"
        if "unauthorized" in text or "auth" in text:
            return "failed", "auth_error"
        if "no tests" in text or ("claimed" in text and "do not exist" in text):
            return "failed", "false_report"
        return "failed", "other"
    return "abandoned", "other"


def import_decision_jsonl(
    path: Path | str, *, store_dir: Path | str, source: str = "model-decisions-import"
) -> int:
    """Best-effort import of the controller's hand-written ``model-decisions.jsonl``.

    Each legacy row has a loose, free-text schema (``model`` spelled multiple
    ways, ``outcome`` as free text, sometimes absent entirely). This maps each
    row onto the fixed BOD-353 taxonomy on a best-effort basis so past attempts
    are not lost, and appends one :class:`AttemptRecord` per row. Malformed
    lines are skipped, not fatal: a historical import must not abort on one
    bad row.
    """
    text = Path(path).read_text(encoding="utf-8")
    count = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        model_raw = str(row.get("model") or "").strip()
        if not model_raw:
            continue
        task_text = str(row.get("task") or "")
        outcome, failure_class = _classify_legacy_outcome(str(row.get("outcome") or ""))
        story_match = _STORY_ID_RE.search(task_text)
        record = build_attempt_record(
            task_kind=_legacy_task_kind(task_text),
            harness="prime",
            model=model_raw,
            outcome=outcome,
            failure_class=failure_class,
            source=source,
            story_id=story_match.group(1) if story_match else None,
            ts=str(row.get("ts") or "") or None,
        )
        write_attempt_record(record, store_dir=store_dir)
        count += 1
    return count
