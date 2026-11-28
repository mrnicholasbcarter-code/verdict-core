"""Verified-outcome labels for OpenJev calibration records (OJ1, #1220).

Every label is read from facts a run recorded: the receipt's node and attempt
rows, and the failure classes the runtime assigned to each attempt. Nothing here
reads a prompt, a topology or an OpenJev signal, and nothing guesses: a fact the
events do not show is ``None`` (unknown), never ``False`` or a default.

Measurement only. No route is admitted, ranked or selected from these labels.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from verdict.orchestration.contracts import RunEvent

# Failure classes the runtime assigns that say nothing about whether the work was
# right: the verification runner could not start, no route was available, the
# served model was not the routed one, serve denied the route, or the provider
# could not or would not serve. A run that ends on one of these has no task label.
# Every other class (verification_failed, ownership_violation, diff_rejected,
# timeout, empty_output, ...) is a judged or ambiguous attempt and stays a task
# failure.
NON_TASK_FAILURES = frozenset(
    {
        "environment_error",
        "pool_exhausted",
        "waiting_for_capacity",
        "model_mismatch",
        "route_denied",
        "harness_configuration",
        "rate_limited",
        "quota_exhausted",
        "payment_required",
        "authentication",
        "permission",
        "model_unavailable",
        "gone",
        "unsupported",
        "gateway_busy",
        "upstream_temporary",
        "transport_temporary",
    }
)

# The lifts the learning-loop plan names. ``template`` and ``node_split`` have no
# event today, so they are always unknown.
LIFTS = ("context_pack", "template", "lint_autofix", "node_split")

AttemptFailures = Mapping[tuple[str, int], str]


def attempt_failures(events: Iterable[RunEvent]) -> dict[tuple[str, int], str]:
    """The failure class of each attempt, from the events that name their attempt.

    The receipt keeps one category per attempt row, and a node-level
    ``pool_exhausted`` failure (which names no attempt) overwrites the last
    attempt's category. The events keep the class the attempt really failed with.
    """
    out: dict[tuple[str, int], str] = {}
    for event in events:
        attempt = event.data.get("attempt")
        category = event.data.get("category")
        if (
            event.type == "failure"
            and isinstance(attempt, int)
            and not isinstance(attempt, bool)
            and isinstance(category, str)
        ):
            out[(event.node_id, attempt)] = category
    return out


def _rows(node: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [a for a in node.get("attempts") or [] if isinstance(a, Mapping)]


def _is_model_row(row: Mapping[str, Any]) -> bool:
    route = str(row.get("route_id") or "")
    return (
        bool(route) and not route.startswith("local/") and row.get("route_identity") != "mechanical"
    )


def _in_play(row: Mapping[str, Any]) -> bool:
    """A model attempt, or a node-level failure that never reached a route.

    The runtime's no-eligible-model path records ``pool_exhausted`` with an empty
    route, before any dispatch. That row says why the node did not run.
    """
    return _is_model_row(row) or (
        not row.get("route_id") and row.get("failure_category") in NON_TASK_FAILURES
    )


def _category(node: Mapping[str, Any], row: Mapping[str, Any], failures: AttemptFailures) -> str:
    attempt = row.get("attempt")
    if isinstance(attempt, int):
        found = failures.get((str(node.get("node_id") or ""), attempt))
        if found is not None:
            return found
    return str(row.get("failure_category") or "")


def _judged(node: Mapping[str, Any], row: Mapping[str, Any], failures: AttemptFailures) -> bool:
    """True when the attempt's work was judged: it succeeded, or failed on the work itself."""
    outcome = row.get("outcome")
    if outcome == "success":
        return True
    return outcome == "failure" and _category(node, row, failures) not in NON_TASK_FAILURES


def _cause(
    node: Mapping[str, Any], row: Mapping[str, Any], failures: AttemptFailures
) -> str | None:
    """Why a node's last attempt says nothing about the work, or None for a task failure."""
    if row.get("outcome") == "abandoned":
        return "abandoned"
    category = _category(node, row, failures)
    return category if category in NON_TASK_FAILURES else None


def _node_cause(
    node: Mapping[str, Any], rows: list[Mapping[str, Any]], failures: AttemptFailures
) -> str | None:
    """Why a node that did not pass says nothing about the work, or None for a task failure.

    One attempt that failed on the work makes it a task failure, whatever failed after it.
    """
    for row in rows:
        if (
            row.get("outcome") == "failure"
            and _category(node, row, failures) not in NON_TASK_FAILURES
        ):
            return None
    return _cause(node, rows[-1], failures)


def _fold(values: list[bool | None]) -> bool | None:
    if any(v is True for v in values):
        return True
    return False if values and all(v is False for v in values) else None


def _context_pack(row: Mapping[str, Any]) -> bool | None:
    pack = row.get("context_pack")
    size = pack.get("context_bytes") if isinstance(pack, Mapping) else None
    return size > 0 if isinstance(size, int) and not isinstance(size, bool) else None


def _lint_autofix(row: Mapping[str, Any]) -> bool | None:
    fix = row.get("lint_autofix")
    changed = fix.get("files_changed") if isinstance(fix, Mapping) else None
    return len(changed) > 0 if isinstance(changed, list) else None


def _agreed(values: list[Any]) -> Any:
    """The one value every passing attempt shares, or None when they differ or are unknown."""
    return values[0] if values and None not in values and len(set(values)) == 1 else None


def label_run(
    receipt: Mapping[str, Any], failures: AttemptFailures | None = None
) -> dict[str, Any]:
    """Labels for one run, from its receipt (and its per-attempt failure classes).

    ``outcome`` is ``accepted`` or ``not_accepted``, or None with an
    ``excluded_reason`` when the run ended on a failure that says nothing about
    the work. ``capacity_class`` and ``capability_tier`` describe the route that
    passed, and are None unless every passing node agrees. ``attempts`` counts
    the model attempts whose work was judged. ``lifts`` is per lift
    ``True`` / ``False`` / None (unknown).
    """
    known = failures or {}
    nodes = [n for n in receipt.get("nodes") or [] if isinstance(n, Mapping)]
    model_nodes = [(n, [r for r in _rows(n) if _in_play(r)]) for n in nodes]
    model_nodes = [(n, rows) for n, rows in model_nodes if rows]

    outcome: str | None = "not_accepted"
    excluded: str | None = None
    claimed = receipt.get("outcome")
    if claimed == "COMPLETE":
        outcome = "accepted"
    elif claimed == "CANCELLED":
        outcome, excluded = None, "cancelled"
    else:
        causes = [
            _node_cause(n, rows, known)
            for n, rows in model_nodes
            if n.get("final_state") != "VALIDATED"
        ]
        if causes and None not in causes:
            outcome, excluded = None, str(causes[0])

    passing: list[Mapping[str, Any]] = []
    deciding: list[Mapping[str, Any]] = []
    for node, rows in model_nodes:
        won = [r for r in rows if r.get("outcome") == "success"]
        if node.get("final_state") == "VALIDATED" and won:
            passing.append(won[-1])
            deciding.append(won[-1])
        else:
            deciding.append(rows[-1])

    capacity = [str(r.get("capacity_class") or "unknown").lower() for r in passing]
    tiers = [r.get("capability_tier") for r in passing]
    return {
        "outcome": outcome,
        "excluded_reason": excluded,
        "capacity_class": _agreed([None if c == "unknown" else c for c in capacity]),
        "capability_tier": _agreed(tiers),
        "attempts": sum(_judged(n, r, known) for n, rows in model_nodes for r in rows),
        "lifts": {
            "context_pack": _fold([_context_pack(r) for r in deciding]),
            "template": None,
            "lint_autofix": _fold([_lint_autofix(r) for r in deciding]),
            "node_split": None,
        },
    }
