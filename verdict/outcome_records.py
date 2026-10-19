"""Outcome records derived from orchestration run receipts.

SONA is the learning loop these records feed.  Each node in a completed
run produces one ``OutcomeRecord``.  Cancelled,
timed-out and fail-closed nodes produce **negative** records (outcome !=
``accepted``) so the learning pipeline never silently drops failures.

``build_outcome_records`` is called once by ``write_run_receipt`` and persisted as
``outcome-records.jsonl`` beside the run's event log.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
from collections.abc import Iterable, Mapping
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

#: BOD-437: ``terminal`` is one execution attempt; ``checker_result`` is a later,
#: append-only independent check of that same attempt key. Legacy rows carry
#: neither and stay unscored.
ATTEMPT_RECORD_KINDS: tuple[str, ...] = ("terminal", "checker_result")

#: Failure classes an independent checker can assign. Every other class is
#: operational (quota, auth, transport, infrastructure, cancel, incomplete)
#: and never carries task-quality credit.
CHECKED_QUALITY_CLASSES: frozenset[str] = frozenset(
    {"success", "test_failed", "review_rejected", "spec_violation", "false_report"}
)

_CREDIT_OUTCOME: Mapping[float, str] = {1.0: "accepted", 0.5: "partial", 0.0: "failed"}


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
    request_id: str | None = None
    decision_id: str | None = None
    run_id: str | None = None
    node_id: str | None = None
    attempt_id: str | None = None
    task_family: str | None = None
    profile_digest: str | None = None
    record_kind: str | None = None  # one of ATTEMPT_RECORD_KINDS, None for legacy rows
    checker_digest: str | None = None
    quality_credit: float | None = None  # 0/.5/1 only on a checker_result with evidence
    effective_parameters: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def key(self) -> tuple[str | None, ...]:
        """The append-only identity: one terminal and one checker row per digest."""
        return (
            self.run_id,
            self.node_id,
            self.attempt_id,
            self.request_id,
            self.record_kind,
            self.checker_digest,
        )


def _require_text(value: str, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} must not be blank")
    return text


def _optional_text(value: str | None) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _validate_quality(
    *,
    record_kind: str | None,
    checker_digest: str | None,
    quality_credit: float | None,
    outcome: str,
    failure_class: str,
) -> None:
    """Quality credit exists only on an independent checker result with evidence."""
    if record_kind is not None and record_kind not in ATTEMPT_RECORD_KINDS:
        raise ValueError(f"record_kind must be one of {ATTEMPT_RECORD_KINDS}, got {record_kind!r}")
    if record_kind == "checker_result" and checker_digest is None:
        raise ValueError("checker_result requires checker_digest evidence")
    if quality_credit is None:
        return
    if record_kind != "checker_result":
        raise ValueError("quality_credit is only valid on a checker_result record")
    if isinstance(quality_credit, bool) or quality_credit not in _CREDIT_OUTCOME:
        raise ValueError(f"quality_credit must be 0, 0.5 or 1, got {quality_credit!r}")
    if failure_class not in CHECKED_QUALITY_CLASSES:
        raise ValueError(f"operational failure_class {failure_class!r} cannot carry quality_credit")
    if outcome != _CREDIT_OUTCOME[quality_credit]:
        raise ValueError(f"quality_credit {quality_credit} contradicts outcome {outcome!r}")


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
    request_id: str | None = None,
    decision_id: str | None = None,
    run_id: str | None = None,
    node_id: str | None = None,
    attempt_id: str | None = None,
    task_family: str | None = None,
    profile_digest: str | None = None,
    record_kind: str | None = None,
    checker_digest: str | None = None,
    quality_credit: float | None = None,
    effective_parameters: Mapping[str, Any] | None = None,
) -> AttemptRecord:
    """Validate and canonicalize one attempt into an :class:`AttemptRecord`.

    ``model`` is canonicalized with :func:`verdict.admission.canonical_route_id`
    so ``omniroute/kiro/claude-haiku-4.5`` and ``kiro/claude-haiku-4.5`` record
    the same model id. ``outcome`` and ``failure_class`` must be members of the
    fixed BOD-353 taxonomy; any other value is a programmer error, not data to
    silently coerce.

    BOD-437 lineage fields are optional; missing usage or settings stay
    ``None``. ``quality_credit`` is accepted only on a ``checker_result`` that
    names its ``checker_digest`` evidence and a checker-assignable class.
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

    story_id = _optional_text(story_id)
    effort = _optional_text(effort)
    record_kind = _optional_text(record_kind)
    checker_digest = _optional_text(checker_digest)
    _validate_quality(
        record_kind=record_kind,
        checker_digest=checker_digest,
        quality_credit=quality_credit,
        outcome=outcome,
        failure_class=failure_class,
    )

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
        request_id=_optional_text(request_id),
        decision_id=_optional_text(decision_id),
        run_id=_optional_text(run_id),
        node_id=_optional_text(node_id),
        attempt_id=_optional_text(attempt_id),
        task_family=_optional_text(task_family),
        profile_digest=_optional_text(profile_digest),
        record_kind=record_kind,
        checker_digest=checker_digest,
        quality_credit=None if quality_credit is None else float(quality_credit),
        effective_parameters=dict(effective_parameters) if effective_parameters else None,
    )


def _append_lines(target: Path, records: list[AttemptRecord], *, skip_existing: bool) -> int:
    """Append complete JSONL rows under an exclusive file lock, then fsync.

    One ``write`` of whole lines while holding ``flock`` keeps concurrent
    writers from interleaving rows; it does not make a crash mid-write atomic.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if skip_existing:
            with open(fd, encoding="utf-8", closefd=False) as existing:
                seen = {key for key in _row_keys(existing)}
            records = [r for r in records if r.key() not in seen]
        if not records:
            return 0
        payload = "".join(
            json.dumps(r.to_dict(), sort_keys=True, ensure_ascii=False) + "\n" for r in records
        ).encode("utf-8")
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError(f"attempt ledger write made no progress: {target}")
            view = view[written:]
        os.fsync(fd)
        return len(records)
    finally:
        os.close(fd)


def _row_keys(lines: Iterable[str]) -> Iterable[tuple[Any, ...]]:
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("record_kind"):
            yield tuple(
                row.get(k)
                for k in (
                    "run_id",
                    "node_id",
                    "attempt_id",
                    "request_id",
                    "record_kind",
                    "checker_digest",
                )
            )


def write_attempt_record(record: AttemptRecord, *, store_dir: Path | str) -> Path:
    """Append one attempt record as a JSONL line under ``store_dir``."""
    target = attempt_records_path(store_dir)
    _append_lines(target, [record], skip_existing=False)
    return target


def append_new_attempt_records(records: Iterable[AttemptRecord], *, store_dir: Path | str) -> int:
    """Append only records whose :meth:`AttemptRecord.key` is not yet stored.

    Re-recording the same run is therefore idempotent and never rewrites an
    earlier row. Returns the number of rows appended.
    """
    return _append_lines(attempt_records_path(store_dir), list(records), skip_existing=True)


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
