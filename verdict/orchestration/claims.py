"""Evidence-derived claims engine (BOD-279).

``derive_claims(events, receipt)`` inspects *only* the recorded event log and
receipt and returns a list of ``Claim`` objects.  A behaviour that was not
demonstrated in the run gets ``NOT_OBSERVED``, never ``VERIFIED``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from verdict.orchestration.contracts import route_family
from verdict.orchestration.receipt import verify_run_receipt

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


def _events_by_type(
    events: Sequence[Mapping[str, Any]],
) -> dict[str, list[Mapping[str, Any]]]:
    by_type: dict[str, list[Mapping[str, Any]]] = {}
    for e in events:
        by_type.setdefault(e["type"], []).append(e)
    return by_type


def _data(event: Mapping[str, Any]) -> Mapping[str, Any]:
    return event.get("data") or {}


# ---------------------------------------------------------------------------
# Individual claim predicates
# ---------------------------------------------------------------------------


def _claim_task_aware_selection(
    by_type: dict[str, list[Mapping[str, Any]]],
    receipt: Mapping[str, Any],
) -> Claim:
    """Task-aware model selection: selection events carry a task profile."""
    sels = by_type.get("selection", [])
    evidence: list[Evidence] = []
    for s in sels:
        d = _data(s)
        # A selection with capacity_class or plan indicates task-aware ranking.
        if d.get("capacity_class") or d.get("plan"):
            evidence.append(Evidence(
                source=f"event:{s['seq']}",
                value={"route_id": d.get("route_id"), "capacity_class": d.get("capacity_class"), "plan": d.get("plan")},
            ))
    if evidence:
        return Claim(
            id="task_aware_selection",
            text="Task-aware model selection",
            status=CLAIM_STATUS_VERIFIED,
            evidence=tuple(evidence),
        )
    return Claim(id="task_aware_selection", text="Task-aware model selection", status=CLAIM_STATUS_NOT_OBSERVED)


def _claim_capability_filtering(
    by_type: dict[str, list[Mapping[str, Any]]],
    receipt: Mapping[str, Any],
) -> Claim:
    """Capability filtering: eligibility events show discovered > eligible (some rejected)."""
    evals = by_type.get("eligibility", [])
    evidence: list[Evidence] = []
    for e in evals:
        d = _data(e)
        discovered = d.get("discovered", 0)
        eligible = d.get("eligible", 0)
        if isinstance(discovered, int) and isinstance(eligible, int) and discovered > eligible:
            evidence.append(Evidence(
                source=f"event:{e['seq']}",
                value={"discovered": discovered, "eligible": eligible},
            ))
    if evidence:
        return Claim(
            id="capability_filtering",
            text="Capability filtering reduced candidate pool",
            status=CLAIM_STATUS_VERIFIED,
            evidence=tuple(evidence),
        )
    return Claim(id="capability_filtering", text="Capability filtering reduced candidate pool", status=CLAIM_STATUS_NOT_OBSERVED)


def _claim_health_considered(
    by_type: dict[str, list[Mapping[str, Any]]],
    receipt: Mapping[str, Any],
) -> Claim:
    """Provider/model health considered: eligibility records healthy stage."""
    evals = by_type.get("eligibility", [])
    evidence: list[Evidence] = []
    for e in evals:
        d = _data(e)
        if "healthy" in d:
            evidence.append(Evidence(
                source=f"event:{e['seq']}",
                value={"healthy": d["healthy"]},
            ))
    if evidence:
        return Claim(
            id="health_considered",
            text="Provider/model health considered in routing",
            status=CLAIM_STATUS_VERIFIED,
            evidence=tuple(evidence),
        )
    return Claim(id="health_considered", text="Provider/model health considered in routing", status=CLAIM_STATUS_NOT_OBSERVED)


def _claim_explicit_assignment(
    by_type: dict[str, list[Mapping[str, Any]]],
    receipt: Mapping[str, Any],
) -> Claim:
    """Explicit concrete worker assignment: dispatch route_id is concrete (not auto/*)."""
    dispatches = by_type.get("dispatch", [])
    evidence: list[Evidence] = []
    for d_ev in dispatches:
        d = _data(d_ev)
        route = d.get("route_id", "")
        if route and not route.startswith("auto/"):
            evidence.append(Evidence(
                source=f"event:{d_ev['seq']}",
                value={"route_id": route},
            ))
    if evidence:
        return Claim(
            id="explicit_assignment",
            text="Explicit concrete worker assignment",
            status=CLAIM_STATUS_VERIFIED,
            evidence=tuple(evidence),
        )
    return Claim(id="explicit_assignment", text="Explicit concrete worker assignment", status=CLAIM_STATUS_NOT_OBSERVED)


def _claim_failure_isolated(
    by_type: dict[str, list[Mapping[str, Any]]],
    receipt: Mapping[str, Any],
) -> Claim:
    """Worker failure isolated from controller: a failure event AND the run continued."""
    failures = by_type.get("failure", [])
    finished = by_type.get("run_finished", [])
    if not failures:
        return Claim(id="failure_isolated", text="Worker failure isolated from controller", status=CLAIM_STATUS_NOT_OBSERVED)
    evidence: list[Evidence] = []
    for f in failures:
        evidence.append(Evidence(
            source=f"event:{f['seq']}",
            value={"category": _data(f).get("category"), "node_id": f.get("node_id")},
        ))
    # The run must have continued (run_finished exists)
    if finished:
        evidence.append(Evidence(
            source=f"event:{finished[-1]['seq']}",
            value={"outcome": _data(finished[-1]).get("outcome")},
        ))
        return Claim(
            id="failure_isolated",
            text="Worker failure isolated from controller",
            status=CLAIM_STATUS_VERIFIED,
            evidence=tuple(evidence),
        )
    return Claim(id="failure_isolated", text="Worker failure isolated from controller", status=CLAIM_STATUS_NOT_OBSERVED)


def _claim_automatic_failover(
    by_type: dict[str, list[Mapping[str, Any]]],
    receipt: Mapping[str, Any],
) -> Claim:
    """Automatic bounded failover: reassign within the attempt budget."""
    reassigns = by_type.get("reassign", [])
    if not reassigns:
        return Claim(id="automatic_failover", text="Automatic bounded failover", status=CLAIM_STATUS_NOT_OBSERVED)
    evidence: list[Evidence] = []
    for r_ev in reassigns:
        d = _data(r_ev)
        evidence.append(Evidence(
            source=f"event:{r_ev['seq']}",
            value={"from_route": d.get("from_route"), "to_route": d.get("to_route"), "reason": d.get("reason")},
        ))
    return Claim(
        id="automatic_failover",
        text="Automatic bounded failover",
        status=CLAIM_STATUS_VERIFIED,
        evidence=tuple(evidence),
    )


def _claim_cooldown_recorded(
    by_type: dict[str, list[Mapping[str, Any]]],
    receipt: Mapping[str, Any],
) -> Claim:
    """Cooldown recorded after failure."""
    cooldowns = by_type.get("cooldown", [])
    if not cooldowns:
        return Claim(id="cooldown_recorded", text="Cooldown recorded", status=CLAIM_STATUS_NOT_OBSERVED)
    evidence: list[Evidence] = []
    for c in cooldowns:
        d = _data(c)
        evidence.append(Evidence(
            source=f"event:{c['seq']}",
            value={"key": d.get("key"), "scope": d.get("scope")},
        ))
    return Claim(
        id="cooldown_recorded",
        text="Cooldown recorded",
        status=CLAIM_STATUS_VERIFIED,
        evidence=tuple(evidence),
    )


def _claim_context_within_budget(
    by_type: dict[str, list[Mapping[str, Any]]],
    receipt: Mapping[str, Any],
) -> Claim:
    """Context assembled within budget: hydrate budget vs bytes when both recorded."""
    hydrates = by_type.get("hydrate", [])
    evidence: list[Evidence] = []
    for h_ev in hydrates:
        d = _data(h_ev)
        budget = d.get("budget_bytes")
        prompt = d.get("prompt_bytes")
        if budget is not None and prompt is not None:
            within = prompt <= budget
            evidence.append(Evidence(
                source=f"event:{h_ev['seq']}",
                value={"budget_bytes": budget, "prompt_bytes": prompt, "within_budget": within},
            ))
    if evidence:
        all_within = all(e.value["within_budget"] for e in evidence)
        return Claim(
            id="context_within_budget",
            text="Context assembled within budget",
            status=CLAIM_STATUS_VERIFIED if all_within else CLAIM_STATUS_CONTRADICTED,
            evidence=tuple(evidence),
        )
    return Claim(id="context_within_budget", text="Context assembled within budget", status=CLAIM_STATUS_NOT_OBSERVED)


def _claim_replacement_completed(
    by_type: dict[str, list[Mapping[str, Any]]],
    receipt: Mapping[str, Any],
) -> Claim:
    """Replacement completed successfully: node VALIDATED on a different route than failed attempt."""
    nodes = receipt.get("nodes") or []
    evidence: list[Evidence] = []
    for node in nodes:
        attempts = node.get("attempts", [])
        if len(attempts) < 2:
            continue
        failed_routes = {a["route_id"] for a in attempts if a.get("outcome") == "failure"}
        success_attempts = [a for a in attempts if a.get("outcome") == "success"]
        for sa in success_attempts:
            if sa["route_id"] not in failed_routes and failed_routes:
                evidence.append(Evidence(
                    source=f"receipt:nodes[{node['node_id']}]",
                    value={
                        "node_id": node["node_id"],
                        "failed_routes": sorted(failed_routes),
                        "success_route": sa["route_id"],
                        "final_state": node.get("final_state"),
                    },
                ))
    # Also require VALIDATED final_state
    validated = [e for e in evidence if e.value.get("final_state") == "VALIDATED"]
    if validated:
        return Claim(
            id="replacement_completed",
            text="Replacement completed successfully on alternate route",
            status=CLAIM_STATUS_VERIFIED,
            evidence=tuple(validated),
        )
    return Claim(id="replacement_completed", text="Replacement completed successfully on alternate route", status=CLAIM_STATUS_NOT_OBSERVED)


def _claim_validation_passed(
    by_type: dict[str, list[Mapping[str, Any]]],
    receipt: Mapping[str, Any],
) -> Claim:
    """Validation passed: verify ok events exist."""
    verifies = by_type.get("verify", [])
    ok_verifies = [v for v in verifies if _data(v).get("ok") is True]
    if not ok_verifies:
        return Claim(id="validation_passed", text="Validation passed", status=CLAIM_STATUS_NOT_OBSERVED)
    evidence: list[Evidence] = []
    for v in ok_verifies:
        evidence.append(Evidence(
            source=f"event:{v['seq']}",
            value={"node_id": v.get("node_id"), "command": _data(v).get("command")},
        ))
    return Claim(
        id="validation_passed",
        text="Validation passed",
        status=CLAIM_STATUS_VERIFIED,
        evidence=tuple(evidence),
    )


def _claim_independent_review(
    by_type: dict[str, list[Mapping[str, Any]]],
    receipt: Mapping[str, Any],
) -> Claim:
    """Independent review: review PASS by a route different from the workers."""
    review = receipt.get("review") or {}
    status = review.get("status", "")
    reviewer_route = review.get("route_id", "")
    if status != "PASS" or not reviewer_route:
        return Claim(id="independent_review", text="Independent review passed", status=CLAIM_STATUS_NOT_OBSERVED)

    # Collect all worker routes
    worker_routes: set[str] = set()
    for node in receipt.get("nodes") or []:
        for attempt in node.get("attempts", []):
            r_id = attempt.get("route_id", "")
            if r_id:
                worker_routes.add(r_id)

    # Check family independence
    reviewer_family = route_family(reviewer_route)
    worker_families = {route_family(r) for r in worker_routes}
    independent = reviewer_family not in worker_families

    evidence_list: list[Evidence] = [
        Evidence(
            source="receipt:review",
            value={"status": status, "route_id": reviewer_route, "reviewer_family": reviewer_family},
        ),
    ]
    if independent:
        return Claim(
            id="independent_review",
            text="Independent review passed",
            status=CLAIM_STATUS_VERIFIED,
            evidence=tuple(evidence_list),
        )
    # Same family means not truly independent
    return Claim(
        id="independent_review",
        text="Independent review passed",
        status=CLAIM_STATUS_NOT_OBSERVED,
        evidence=tuple(evidence_list),
    )


def _claim_receipt_integrity(
    by_type: dict[str, list[Mapping[str, Any]]],
    receipt: Mapping[str, Any],
    run_dir: Path | None = None,
) -> Claim:
    """Receipt integrity: verify_run_receipt returns empty list."""
    if run_dir is None:
        return Claim(id="receipt_integrity", text="Receipt integrity verified", status=CLAIM_STATUS_NOT_OBSERVED)
    problems = verify_run_receipt(run_dir)
    if not problems:
        return Claim(
            id="receipt_integrity",
            text="Receipt integrity verified",
            status=CLAIM_STATUS_VERIFIED,
            evidence=(Evidence(source="receipt:verify", value={"problems": []}),),
        )
    return Claim(
        id="receipt_integrity",
        text="Receipt integrity verified",
        status=CLAIM_STATUS_CONTRADICTED,
        evidence=(Evidence(source="receipt:verify", value={"problems": problems}),),
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
    events: Sequence[Mapping[str, Any]],
    receipt: Mapping[str, Any],
    *,
    run_dir: Path | None = None,
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
        is checked via ``verify_run_receipt``.

    Returns
    -------
    list[Claim]
        One ``Claim`` per predicate.  Status is ``VERIFIED`` only when the
        evidence satisfies the predicate; ``NOT_OBSERVED`` when the evidence
        is absent; ``CONTRADICTED`` when evidence shows the opposite.
    """
    by_type = _events_by_type(events)
    claims: list[Claim] = []
    for pred in _CLAIM_PREDICATES:
        claims.append(pred(by_type, receipt))
    # Receipt integrity is special: needs run_dir
    claims.append(_claim_receipt_integrity(by_type, receipt, run_dir))
    return claims
