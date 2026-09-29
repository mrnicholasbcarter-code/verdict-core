"""Evidence-derived claims engine (BOD-279).

``derive_claims(events, receipt)`` inspects *only* the recorded event log and
receipt and returns a list of ``Claim`` objects.  A behaviour that was not
demonstrated in the run gets ``NOT_OBSERVED``, never ``VERIFIED``; evidence
that shows the opposite gets ``CONTRADICTED``.  Every predicate reads the
observed (executed) identity and seq chronology, never intended routes alone.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from verdict.orchestration.contracts import route_family
from verdict.orchestration.eligibility import _OPAQUE_PREFIXES
from verdict.orchestration.receipt import EVENTS_FILE, RECEIPT_FILE, verify_run_receipt

# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------

CLAIM_STATUS_VERIFIED = "VERIFIED"
CLAIM_STATUS_NOT_OBSERVED = "NOT_OBSERVED"
CLAIM_STATUS_CONTRADICTED = "CONTRADICTED"


@dataclass(frozen=True)
class Evidence:
    """One piece of supporting evidence for a claim."""

    source: str  # "event:<seq>" or "receipt:<field>"
    value: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "value": self.value}


@dataclass(frozen=True)
class Claim:
    """One evidence-derived claim about a run."""

    id: str
    text: str
    status: str  # VERIFIED | NOT_OBSERVED | CONTRADICTED
    evidence: tuple[Evidence, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "status": self.status,
            "evidence": [e.to_dict() for e in self.evidence],
        }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _data(event: Mapping[str, Any]) -> Mapping[str, Any]:
    return event.get("data") or {}


def _seq(event: Mapping[str, Any]) -> int:
    value = event.get("seq")
    return value if isinstance(value, int) and not isinstance(value, bool) else -1


def _int(value: Any) -> int | None:
    """Strict integer coercion: bools and strings are rejected."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _ordered(events: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return sorted(events, key=_seq)


def _claim(cid: str, text: str, status: str, evidence: Sequence[Evidence] = ()) -> Claim:
    return Claim(id=cid, text=text, status=status, evidence=tuple(evidence))


def _ev(event: Mapping[str, Any], value: Any) -> Evidence:
    return Evidence(source=f"event:{_seq(event)}", value=value)


def _is_opaque(route: str) -> bool:
    return route.lower().startswith(_OPAQUE_PREFIXES)


# Capability-stage rejection: only TASK_ELIGIBLE failures filter on capability.
_CAPABILITY_STAGE = "TASK_ELIGIBLE"
_HEALTHY_OR_LATER = frozenset({"HEALTHY", "AVAILABLE", "TASK_ELIGIBLE", "SELECTED"})
_NO_CHECK_PREFIX = "(none"
_MECHANICAL_MODEL = "(mechanical merge)"


def _preceding_eligibility(
    events: Sequence[Mapping[str, Any]], selection: Mapping[str, Any]
) -> Mapping[str, Any] | None:
    """The latest eligibility event for the same node recorded before ``selection``."""
    best: Mapping[str, Any] | None = None
    for e in events:
        if e.get("type") != "eligibility" or e.get("node_id") != selection.get("node_id"):
            continue
        if _seq(e) < _seq(selection) and (best is None or _seq(e) > _seq(best)):
            best = e
    return best


def _candidates(data: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    cands = data.get("candidates")
    if not isinstance(cands, list):
        return []
    return [c for c in cands if isinstance(c, Mapping)]


# ---------------------------------------------------------------------------
# Individual claim predicates
# ---------------------------------------------------------------------------


def _claim_task_aware_selection(
    events: Sequence[Mapping[str, Any]], receipt: Mapping[str, Any]
) -> Claim:
    """A selection ranked >1 candidates against a recorded task profile."""
    cid, text = "task_aware_selection", "Task-aware model selection"
    understand = [e for e in events if e.get("type") == "understand" and _data(e)]
    plan_nodes: dict[str, Mapping[str, Any]] = {}
    for p in events:
        if p.get("type") != "plan_ready":
            continue
        nodes = _data(p).get("nodes")
        for n in nodes if isinstance(nodes, list) else []:
            if isinstance(n, Mapping) and isinstance(n.get("node_id"), str):
                plan_nodes[n["node_id"]] = n
    evidence: list[Evidence] = []
    for s in events:
        if s.get("type") != "selection":
            continue
        node = _str(s.get("node_id"))
        route = _str(_data(s).get("route_id"))
        profile_ev = [u for u in understand if _seq(u) < _seq(s)]
        requirements = plan_nodes.get(node, {})
        has_requirements = bool(
            requirements.get("required_capabilities")
            or _int(requirements.get("min_context_tokens"))
        )
        elig = _preceding_eligibility(events, s)
        if not (route and profile_ev and has_requirements and elig is not None):
            continue
        ranked = [c for c in _candidates(_data(elig)) if _int(c.get("rank")) is not None]
        chosen = [c for c in ranked if c.get("route_id") == route]
        if len(ranked) > 1 and chosen:
            evidence.append(
                _ev(
                    s,
                    {
                        "route_id": route,
                        "rank": chosen[0].get("rank"),
                        "ranked_candidates": len(ranked),
                        "profile_event": _seq(profile_ev[-1]),
                        "eligibility_event": _seq(elig),
                        "required_capabilities": list(
                            requirements.get("required_capabilities") or []
                        ),
                    },
                )
            )
    if evidence:
        return _claim(cid, text, CLAIM_STATUS_VERIFIED, evidence)
    return _claim(cid, text, CLAIM_STATUS_NOT_OBSERVED)


def _capability_rejections(data: Mapping[str, Any]) -> dict[str, int]:
    """Reason -> count for rejections recorded at the capability stage."""
    found: dict[str, int] = {}
    rej = data.get("rejections")
    if isinstance(rej, Mapping):
        bucket = rej.get(_CAPABILITY_STAGE)
        if isinstance(bucket, Mapping):
            for reason, count in bucket.items():
                n = _int(count)
                if n and n > 0:
                    found[str(reason)] = found.get(str(reason), 0) + n
    elif isinstance(rej, list):
        for r in rej:
            if isinstance(r, Mapping) and r.get("stage") == _CAPABILITY_STAGE:
                reason = _str(r.get("reason")) or "unknown"
                found[reason] = found.get(reason, 0) + 1
    for c in _candidates(data):
        if c.get("failed_stage") == _CAPABILITY_STAGE and not isinstance(rej, (Mapping, list)):
            reason = _str(c.get("reason")) or "unknown"
            found[reason] = found.get(reason, 0) + 1
    return found


def _claim_capability_filtering(
    events: Sequence[Mapping[str, Any]], receipt: Mapping[str, Any]
) -> Claim:
    """Capability filtering: eligibility rejected routes at the TASK_ELIGIBLE stage."""
    cid, text = "capability_filtering", "Capability filtering reduced candidate pool"
    evidence: list[Evidence] = []
    for e in events:
        if e.get("type") != "eligibility":
            continue
        found = _capability_rejections(_data(e))
        if found:
            evidence.append(_ev(e, {"stage": _CAPABILITY_STAGE, "reasons": found}))
    if evidence:
        return _claim(cid, text, CLAIM_STATUS_VERIFIED, evidence)
    return _claim(cid, text, CLAIM_STATUS_NOT_OBSERVED)


def _claim_health_considered(
    events: Sequence[Mapping[str, Any]], receipt: Mapping[str, Any]
) -> Claim:
    """Every selected route was reached HEALTHY+ in an eligibility pass that probed."""
    cid, text = "health_considered", "Provider/model health considered in routing"
    selections = [s for s in events if s.get("type") == "selection"]
    if not selections:
        return _claim(cid, text, CLAIM_STATUS_NOT_OBSERVED)
    evidence: list[Evidence] = []
    for s in selections:
        route = _str(_data(s).get("route_id"))
        probes = [
            p
            for p in events
            if p.get("type") == "probe"
            and _seq(p) < _seq(s)
            and _str(_data(p).get("route_id")) == route
            and _data(p).get("ok") is True
        ]
        # An earlier eligibility pass in this run that actually probed and
        # recorded this route at HEALTHY or later (a probe result may be reused).
        probed_healthy: list[tuple[Mapping[str, Any], int, str]] = []
        for e in events:
            if e.get("type") != "eligibility" or _seq(e) >= _seq(s):
                continue
            probed = _int(_data(e).get("probed"))
            if not probed or probed <= 0:
                continue
            for c in _candidates(_data(e)):
                reached = _str(c.get("reached"))
                if c.get("route_id") == route and reached in _HEALTHY_OR_LATER:
                    probed_healthy.append((e, probed, reached))
        if probes:
            evidence.append(_ev(probes[-1], {"route_id": route, "probe": "ok"}))
        elif probed_healthy:
            e, probed, reached = probed_healthy[-1]
            evidence.append(_ev(e, {"route_id": route, "probed": probed, "reached": reached}))
        else:
            # One selected route without an observed health result: not demonstrated.
            return _claim(cid, text, CLAIM_STATUS_NOT_OBSERVED, evidence)
    return _claim(cid, text, CLAIM_STATUS_VERIFIED, evidence)


def _claim_explicit_assignment(
    events: Sequence[Mapping[str, Any]], receipt: Mapping[str, Any]
) -> Claim:
    """Every dispatch named a concrete route (no production opaque prefix)."""
    cid, text = "explicit_assignment", "Explicit concrete worker assignment"
    evidence: list[Evidence] = []
    opaque: list[Evidence] = []
    for d_ev in events:
        if d_ev.get("type") != "dispatch":
            continue
        route = _str(_data(d_ev).get("route_id"))
        if not route:
            continue
        (opaque if _is_opaque(route) else evidence).append(_ev(d_ev, {"route_id": route}))
    if opaque:
        return _claim(cid, text, CLAIM_STATUS_CONTRADICTED, opaque)
    if evidence:
        return _claim(cid, text, CLAIM_STATUS_VERIFIED, evidence)
    return _claim(cid, text, CLAIM_STATUS_NOT_OBSERVED)


def _claim_failure_isolated(
    events: Sequence[Mapping[str, Any]], receipt: Mapping[str, Any]
) -> Claim:
    """A worker failure was followed in seq by more work and then run_finished."""
    cid, text = "failure_isolated", "Worker failure isolated from controller"
    for f in events:
        if f.get("type") != "failure":
            continue
        node = _str(f.get("node_id"))
        category = _str(_data(f).get("category"))
        if not node or category.startswith("controller"):
            continue  # controller failures are not worker failures
        later_work = [
            e for e in events if e.get("type") in ("dispatch", "terminal") and _seq(e) > _seq(f)
        ]
        finished = [e for e in events if e.get("type") == "run_finished" and _seq(e) > _seq(f)]
        if later_work and finished:
            return _claim(
                cid,
                text,
                CLAIM_STATUS_VERIFIED,
                (
                    _ev(f, {"category": category, "node_id": node}),
                    _ev(later_work[0], {"type": later_work[0].get("type")}),
                    _ev(finished[-1], {"outcome": _data(finished[-1]).get("outcome")}),
                ),
            )
    return _claim(cid, text, CLAIM_STATUS_NOT_OBSERVED)


def _recorded_attempt_budget(
    events: Sequence[Mapping[str, Any]], receipt: Mapping[str, Any]
) -> tuple[int | None, str]:
    for e in events:
        if e.get("type") == "run_started":
            budget = _data(e).get("retry_budget")
            if isinstance(budget, Mapping):
                value = _int(budget.get("max_attempts_per_node"))
                if value is not None:
                    return value, f"event:{_seq(e)}"
    return None, ""


def _claim_automatic_failover(
    events: Sequence[Mapping[str, Any]], receipt: Mapping[str, Any]
) -> Claim:
    """A failed node was reassigned within the recorded budget and then succeeded."""
    cid, text = "automatic_failover", "Automatic bounded failover"
    reassigns = [r for r in events if r.get("type") == "reassign"]
    if not reassigns:
        return _claim(cid, text, CLAIM_STATUS_NOT_OBSERVED)
    budget, budget_src = _recorded_attempt_budget(events, receipt)
    if budget is None:
        return _claim(cid, text, CLAIM_STATUS_NOT_OBSERVED)
    evidence: list[Evidence] = []
    over: list[Evidence] = []
    for r in reassigns:
        d = _data(r)
        node = r.get("node_id")
        attempt = _int(d.get("attempt"))
        failed_before = [
            f
            for f in events
            if f.get("type") == "failure" and f.get("node_id") == node and _seq(f) < _seq(r)
        ]
        if attempt is None or not failed_before:
            continue
        if attempt > budget:
            over.append(_ev(r, {"attempt": attempt, "max_attempts_per_node": budget}))
            continue
        ok_terminal = [
            t
            for t in events
            if t.get("type") == "terminal"
            and t.get("node_id") == node
            and _seq(t) > _seq(r)
            and _int(_data(t).get("attempt")) == attempt
            and _data(t).get("ok") is True
        ]
        if ok_terminal:
            evidence.append(
                _ev(
                    r,
                    {
                        "from_route": d.get("from_route"),
                        "to_route": d.get("to_route"),
                        "reason": d.get("reason"),
                        "attempt": attempt,
                        "max_attempts_per_node": budget,
                        "budget_source": budget_src,
                        "failure_event": _seq(failed_before[-1]),
                        "terminal_event": _seq(ok_terminal[0]),
                    },
                )
            )
    if over:
        return _claim(cid, text, CLAIM_STATUS_CONTRADICTED, over)
    if evidence:
        return _claim(cid, text, CLAIM_STATUS_VERIFIED, evidence)
    return _claim(cid, text, CLAIM_STATUS_NOT_OBSERVED)


def _claim_cooldown_recorded(
    events: Sequence[Mapping[str, Any]], receipt: Mapping[str, Any]
) -> Claim:
    """A cooldown with key, scope and an 'until' later than the event time."""
    cid, text = "cooldown_recorded", "Cooldown recorded"
    evidence: list[Evidence] = []
    for c in events:
        if c.get("type") != "cooldown":
            continue
        d = _data(c)
        key, scope = _str(d.get("key")), _str(d.get("scope"))
        until, at = _parse_ts(d.get("until")), _parse_ts(c.get("at"))
        if key and scope and until is not None and at is not None and until > at:
            evidence.append(_ev(c, {"key": key, "scope": scope, "until": d.get("until")}))
    if evidence:
        return _claim(cid, text, CLAIM_STATUS_VERIFIED, evidence)
    return _claim(cid, text, CLAIM_STATUS_NOT_OBSERVED)


def _claim_context_within_budget(
    events: Sequence[Mapping[str, Any]], receipt: Mapping[str, Any]
) -> Claim:
    """Every hydration recorded integer bytes within its integer budget."""
    cid, text = "context_within_budget", "Context assembled within budget"
    recorded: list[Evidence] = []
    missing: list[Evidence] = []
    for h in events:
        if h.get("type") not in ("hydrate", "rehydrate"):
            continue
        d = _data(h)
        budget, prompt = _int(d.get("budget_bytes")), _int(d.get("prompt_bytes"))
        if budget is None or prompt is None:
            missing.append(_ev(h, {"node_id": h.get("node_id"), "recorded": False}))
            continue
        recorded.append(
            _ev(
                h,
                {"budget_bytes": budget, "prompt_bytes": prompt, "within_budget": prompt <= budget},
            )
        )
    if any(not e.value["within_budget"] for e in recorded):
        return _claim(
            cid,
            text,
            CLAIM_STATUS_CONTRADICTED,
            [e for e in recorded if not e.value["within_budget"]],
        )
    if recorded and not missing:
        return _claim(cid, text, CLAIM_STATUS_VERIFIED, recorded)
    if recorded:
        # Partial coverage: only the recorded hydrations are within budget.
        return _claim(
            cid,
            f"{text} (only {len(recorded)} of {len(recorded) + len(missing)} hydrations recorded)",
            CLAIM_STATUS_NOT_OBSERVED,
            [*recorded, *missing],
        )
    return _claim(cid, text, CLAIM_STATUS_NOT_OBSERVED, missing)


def _executed_identity(terminal: Mapping[str, Any]) -> str:
    model = _str(_data(terminal).get("reported_model"))
    return "" if model == _MECHANICAL_MODEL else model


def _claim_replacement_completed(
    events: Sequence[Mapping[str, Any]], receipt: Mapping[str, Any]
) -> Claim:
    """A failed attempt was followed by a later attempt that executed elsewhere and validated."""
    cid, text = "replacement_completed", "Replacement completed successfully on alternate route"
    evidence: list[Evidence] = []
    nodes = {_str(e.get("node_id")) for e in events if e.get("type") == "failure"}
    for node in sorted(n for n in nodes if n):
        node_events = [e for e in events if e.get("node_id") == node]
        for f in node_events:
            if f.get("type") != "failure":
                continue
            f_attempt = _int(_data(f).get("attempt"))
            if f_attempt is None:
                continue
            failed_ids = {_str(_data(f).get("route_id"))}
            for t in node_events:
                if t.get("type") == "terminal" and _int(_data(t).get("attempt")) == f_attempt:
                    failed_ids.add(_executed_identity(t))
            failed_ids.discard("")
            for t in node_events:
                t_attempt = _int(_data(t).get("attempt"))
                if (
                    t.get("type") != "terminal"
                    or _seq(t) <= _seq(f)
                    or t_attempt is None
                    or t_attempt <= f_attempt
                    or _data(t).get("ok") is not True
                ):
                    continue
                executed = _executed_identity(t)
                if not executed or not failed_ids or executed in failed_ids:
                    continue
                validated = [
                    v
                    for v in node_events
                    if v.get("type") == "verify"
                    and _seq(v) > _seq(t)
                    and _data(v).get("ok") is True
                    and _real_command(_data(v).get("command"))
                ]
                if validated:
                    evidence.append(
                        Evidence(
                            source=f"event:{_seq(t)}",
                            value={
                                "node_id": node,
                                "failed_routes": sorted(failed_ids),
                                "success_route": executed,
                                "failed_attempt": f_attempt,
                                "success_attempt": t_attempt,
                                "verify_event": _seq(validated[0]),
                                "final_state": "VALIDATED",
                            },
                        )
                    )
                    break
    if evidence:
        return _claim(cid, text, CLAIM_STATUS_VERIFIED, evidence)
    return _claim(cid, text, CLAIM_STATUS_NOT_OBSERVED)


def _real_command(command: Any) -> bool:
    if isinstance(command, list):
        command = " ".join(str(c) for c in command)
    return (
        isinstance(command, str)
        and bool(command.strip())
        and not command.strip().startswith(_NO_CHECK_PREFIX)
    )


def _claim_validation_passed(
    events: Sequence[Mapping[str, Any]], receipt: Mapping[str, Any]
) -> Claim:
    """An executed check passed and no later verify failed for that node."""
    cid, text = "validation_passed", "Validation passed"
    last: dict[str, Mapping[str, Any]] = {}
    for v in events:
        if v.get("type") == "verify":
            last[_str(v.get("node_id"))] = v  # events are seq-ordered
    failed = [v for v in last.values() if _data(v).get("ok") is not True]
    if failed:
        return _claim(
            cid,
            text,
            CLAIM_STATUS_CONTRADICTED,
            [_ev(v, {"node_id": v.get("node_id"), "ok": _data(v).get("ok")}) for v in failed],
        )
    evidence = [
        _ev(v, {"node_id": v.get("node_id"), "command": _data(v).get("command")})
        for v in last.values()
        if _real_command(_data(v).get("command"))
    ]
    if evidence:
        return _claim(cid, text, CLAIM_STATUS_VERIFIED, evidence)
    return _claim(cid, text, CLAIM_STATUS_NOT_OBSERVED)


def _claim_independent_review(
    events: Sequence[Mapping[str, Any]], receipt: Mapping[str, Any]
) -> Claim:
    """Review PASS, zero blocking, by an executed identity no worker attempt used."""
    cid, text = "independent_review", "Independent review passed"
    reviews = [e for e in events if e.get("type") == "review"]
    if not reviews:
        return _claim(cid, text, CLAIM_STATUS_NOT_OBSERVED)
    review = reviews[-1]
    rd = _data(review)
    reviewer = _str(rd.get("route_id"))
    blocking = _int(rd.get("blocking"))
    if rd.get("status") != "PASS" or not reviewer:
        return _claim(cid, text, CLAIM_STATUS_NOT_OBSERVED)
    if blocking is None:
        return _claim(cid, text, CLAIM_STATUS_NOT_OBSERVED)
    if blocking > 0:
        return _claim(cid, text, CLAIM_STATUS_CONTRADICTED, [_ev(review, {"blocking": blocking})])
    reviewer_ids = {reviewer} | {
        _str(_data(a).get("route_id"))
        for a in events
        if a.get("type") == "review_attempt" and _str(_data(a).get("route_id"))
    }
    # Worker identities: every dispatched attempt's intended route AND executed model.
    worker_ids: set[str] = set()
    for d_ev in events:
        if d_ev.get("type") != "dispatch":
            continue
        node, attempt = d_ev.get("node_id"), _int(_data(d_ev).get("attempt"))
        terminals = [
            t
            for t in events
            if t.get("type") == "terminal"
            and t.get("node_id") == node
            and _int(_data(t).get("attempt")) == attempt
        ]
        executed = [_executed_identity(t) for t in terminals if _executed_identity(t)]
        if not executed:
            return _claim(cid, text, CLAIM_STATUS_NOT_OBSERVED)  # identity not observed
        worker_ids.update(executed)
        worker_ids.add(_str(_data(d_ev).get("route_id")))
    for node in receipt.get("nodes") or []:
        for att in node.get("attempts", []) if isinstance(node, Mapping) else []:
            if isinstance(att, Mapping):
                worker_ids.add(_str(att.get("route_id")))
                worker_ids.add(_str(att.get("executed_model")))
    worker_ids.discard("")
    if not worker_ids:
        return _claim(cid, text, CLAIM_STATUS_NOT_OBSERVED)
    # Chronology: the review must come after every worker terminal it could have judged.
    review_seq = _int(review.get("seq"))
    terminal_seqs = [
        s for t in events if t.get("type") == "terminal" and (s := _int(t.get("seq"))) is not None
    ]
    if review_seq is None or (terminal_seqs and review_seq <= max(terminal_seqs)):
        return _claim(
            cid, text, CLAIM_STATUS_NOT_OBSERVED, [_ev(review, {"review_seq": review_seq})]
        )
    shared = sorted(reviewer_ids & worker_ids)
    shared_families = sorted(
        {route_family(r) for r in reviewer_ids} & {route_family(r) for r in worker_ids}
    )
    value = {
        "reviewer_routes": sorted(reviewer_ids),
        "worker_identities": sorted(worker_ids),
        "shared_identities": shared,
        "shared_families": shared_families,
    }
    if shared:
        # The reviewer executed as one of the workers: review was not independent.
        return _claim(cid, text, CLAIM_STATUS_CONTRADICTED, [_ev(review, value)])
    if shared_families:
        # Distinct routes but the same model family: independence not demonstrated.
        return _claim(cid, text, CLAIM_STATUS_NOT_OBSERVED, [_ev(review, value)])
    return _claim(cid, text, CLAIM_STATUS_VERIFIED, [_ev(review, value)])


def _claim_receipt_integrity(
    events: Sequence[Mapping[str, Any]], receipt: Mapping[str, Any], run_dir: Path | None = None
) -> Claim:
    """The SUPPLIED events/receipt are the run_dir's, and that receipt verifies."""
    cid, text = "receipt_integrity", "Receipt integrity verified"
    if run_dir is None:
        return _claim(cid, text, CLAIM_STATUS_NOT_OBSERVED)
    run_dir = Path(run_dir)
    problems: list[str] = []
    try:
        stored_receipt = json.loads((run_dir / RECEIPT_FILE).read_text())
        stored_events = [
            json.loads(line)
            for line in (run_dir / EVENTS_FILE).read_text().splitlines()
            if line.strip()
        ]
    except (OSError, ValueError) as exc:
        problems.append(f"cannot read run_dir evidence: {exc}")
    else:
        if dict(receipt) != stored_receipt:
            problems.append("supplied receipt differs from run_dir receipt.json")
        if [dict(e) for e in events] != stored_events:
            problems.append("supplied events differ from run_dir events.jsonl")
    problems.extend(verify_run_receipt(run_dir))
    if not problems:
        return _claim(
            cid,
            text,
            CLAIM_STATUS_VERIFIED,
            (Evidence(source="receipt:verify", value={"problems": []}),),
        )
    return _claim(
        cid,
        text,
        CLAIM_STATUS_CONTRADICTED,
        (Evidence(source="receipt:verify", value={"problems": problems}),),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# All claim predicates in canonical order.
_CLAIM_PREDICATES = (
    _claim_task_aware_selection,
    _claim_capability_filtering,
    _claim_health_considered,
    _claim_explicit_assignment,
    _claim_failure_isolated,
    _claim_automatic_failover,
    _claim_cooldown_recorded,
    _claim_context_within_budget,
    _claim_replacement_completed,
    _claim_validation_passed,
    _claim_independent_review,
)


def derive_claims(
    events: Sequence[Mapping[str, Any]], receipt: Mapping[str, Any], *, run_dir: Path | None = None
) -> list[Claim]:
    """Derive claims from recorded evidence only.

    Parameters
    ----------
    events:
        The run's event log as a sequence of dicts (one per JSONL line).
    receipt:
        The parsed ``receipt.json``.
    run_dir:
        Optional path to the run directory.  When provided, receipt integrity
        is VERIFIED only if the supplied events/receipt equal that directory's
        files and ``verify_run_receipt`` reports no problems.

    Returns
    -------
    list[Claim]
        One ``Claim`` per predicate.  Status is ``VERIFIED`` only when the
        evidence satisfies the predicate; ``NOT_OBSERVED`` when the evidence
        is absent; ``CONTRADICTED`` when evidence shows the opposite.
    """
    ordered = _ordered([e for e in events if isinstance(e, Mapping)])
    claims: list[Claim] = [pred(ordered, receipt) for pred in _CLAIM_PREDICATES]
    # Receipt integrity is special: it checks the supplied run against run_dir.
    claims.append(_claim_receipt_integrity(events, receipt, run_dir))
    return claims
