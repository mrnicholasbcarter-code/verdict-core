"""Deterministic DecisionKernel facade (VER-002 #219).

One authority API over verdict-core's existing classification, catalog,
availability, eligibility, capability-passport, adaptive-ranker, and escalation
modules. For a given :class:`TaskSpec` and policy version it deterministically
produces a stable ``decision_id``, the eligible candidate set, per-candidate
exclusions with rationale, the policy version, and a tamper-evident receipt.

Design invariants (spec/003-deterministic-decision-kernel):

* **Compose, do not duplicate** -- every boundary module is called via its
  existing entrypoint; no logic is copied (NFR-005).
* **Advisory cannot weaken hard policy** -- advisory ranking may only reorder
  candidates the hard eligibility gate already admitted (FR-005).
* **Fail-closed** -- for a protected task, a candidate whose availability truth
  is absent/failed is excluded rather than admitted (FR-006, NFR-002).
* **Derived, not persisted** -- the id and receipt are recomputable from the
  supplied inputs with no producer trust, no network, no credentials (FR-004,
  FR-009, NFR-001, NFR-003).
* **Backwards-compatible projection** -- the decision projects into the existing
  :class:`RoutingDecisionContract` (additive ``decision_id``/``receipt`` fields);
  existing consumers are unchanged (FR-008).

The outcome (``accepted``/``degraded``/``denied``) is the aggregate of the
per-candidate eligibility verdicts -- there is no separate named-policy gate
(FR-007).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any, cast

from verdict.adaptive_ranker import AdaptiveRanker, RankerOutput, build_adaptive_ranker
from verdict.availability import AvailabilityReport, canonical_capability
from verdict.contracts import RoutingDecisionContract, TaskSpec
from verdict.eligibility import (
    EligibilityGate,
    EligibilityRecord,
    EligibilityResult,
    EligibilityVerdict,
)
from verdict.escalation import scan as escalation_scan
from verdict.models import ModelInfo
from verdict.security import fingerprint_text

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "DETERMINISTIC_EPOCH",
    "OUTCOME_ACCEPTED",
    "OUTCOME_DEGRADED",
    "OUTCOME_DENIED",
    "DecisionReceipt",
    "DecisionRecord",
    "VerificationFault",
    "compute_candidate_catalog_digest",
    "compute_canonical_task_spec",
    "compute_receipt_digest",
    "decide",
    "verify_decision",
]

# A single deterministic clock epoch so identical inputs always produce identical
# ids/receipts when the caller supplies no ``now`` (NFR-001). This is the ONLY
# wall-clock reference used by the facade; it is a constant, not a live clock.
DETERMINISTIC_EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)

# Stable outcome codes (FR-007). The reason string is "<outcome>:<reason>" so
# the code is enumerable and self-describing without a lookup table.
OUTCOME_ACCEPTED = "accepted"
OUTCOME_DEGRADED = "degraded"
OUTCOME_DENIED = "denied"

# Reason codes appended to outcomes.
_ACCEPTED_REASON = "accepted:all_gates_green"
_DENIED_NO_CANDIDATES = "denied:no_eligible_candidate"
_DENIED_ALL_BLOCKED = "denied:all_candidates_hard_blocked"
_DEGRADED_PREFERRED_DOWN = "degraded:preferred_candidate_fail_closed"

# Stateless advisory ranker shared across calls. ``build_adaptive_ranker`` with
# no vector DB yields a baseline (non-shadow) ranker that reorders admitted
# candidates by configured rules; it never admits a non-admitted one because
# ``AdaptiveRanker.rank`` builds ranking candidates from ``eligibility_result``
# admitted-by-id only.
_BASELINE_RANKER: AdaptiveRanker | None = None


def _baseline_ranker() -> AdaptiveRanker:
    global _BASELINE_RANKER
    if _BASELINE_RANKER is None:
        _BASELINE_RANKER = build_adaptive_ranker()
    return _BASELINE_RANKER


# ---------------------------------------------------------------------------
# Canonicalization + digest primitives (reuse feature-002 lineage).
# ---------------------------------------------------------------------------


def _canonical(obj: Any) -> Any:
    """Canonicalize a JSON-comparable value for stable hashing.

    Mirrors feature 002's canonical serialization: sorted keys at every level,
    rejection of non-finite floats, integral floats rendered as ints, and
    deterministic ordering. Volatile provenance (caller correlation ids, live
    timestamps) never enters the canonical form -- callers that pass such fields
    exlude them upstream (NFR-001).
    """

    if isinstance(obj, str | bool | int) or obj is None:
        return obj
    if isinstance(obj, float):
        import math

        if not math.isfinite(obj):
            raise ValueError("non-finite float in canonical form")
        if abs(obj) >= 1e16:
            raise ValueError("float magnitude exceeds canonical bound")
        if obj.is_integer():
            return int(obj)
        return obj
    if isinstance(obj, Mapping):
        return {str(k): _canonical(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [_canonical(v) for v in obj]
    # dataclasses, enums, datetime-ish -> best-effort canonical string form
    if isinstance(obj, Enum):
        return obj.value
    if hasattr(obj, "to_dict") and callable(obj.to_dict):
        return _canonical(obj.to_dict())
    return _canonical(asdict(obj)) if _is_dataclass_instance(obj) else str(obj)


def _is_dataclass_instance(obj: Any) -> bool:
    import dataclasses as _dc

    return _dc.is_dataclass(obj) and not isinstance(obj, type)


def compute_canonical_task_spec(task_spec: TaskSpec) -> str:
    """Stable canonical JSON string of a TaskSpec for decision-id binding.

    Only fields that materially change the decision enter the canonical form:
    ``objective``, ``task_type``, ``required_capabilities``, ``tools``,
    ``privacy``, ``risk``, ``approvals``. Free-form ``metadata`` is excluded so
    caller-supplied volatile provenance cannot perturb the id (NFR-001).
    """

    payload = {
        "objective": task_spec.objective,
        "task_type": task_spec.task_type,
        "required_capabilities": list(getattr(task_spec, "required_capabilities", []) or []),
        "tools": list(getattr(task_spec, "tools", []) or []),
        "privacy": getattr(task_spec, "privacy", None),
        "risk": getattr(task_spec, "risk", None),
        "approvals": list(getattr(task_spec, "approvals", []) or []),
    }
    import json

    return json.dumps(_canonical(payload), sort_keys=True, separators=(",", ":"))


def compute_candidate_catalog_digest(candidates: Sequence[ModelInfo]) -> str:
    """Stable digest of the supplied candidate catalog snapshot for id binding."""

    import json

    payload = [_canonical(_model_identity(c)) for c in candidates]
    return fingerprint_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _model_identity(model: ModelInfo) -> dict[str, Any]:
    """The identity fields that matter for decision binding for one candidate."""

    ident = {"id": getattr(model, "id", None)}
    for attr in ("provider", "model", "tier", "capabilities"):
        if hasattr(model, attr):
            value = getattr(model, attr)
            ident[attr] = list(value) if isinstance(value, (list, tuple, set, frozenset)) else value
    return ident


def compute_receipt_digest(receipt: DecisionReceipt) -> str:
    """Stable ``sha256:<hex>`` over the canonical form of a receipt.

    The canonical form includes every field EXCEPT ``integrity_digest`` itself
    (which is what this function computes). Reuses :func:`fingerprint_text` and
    feature-002's canonical-serialization discipline so the carrier (feature
    002) and the decider (feature 003) share one determinism lineage.
    """

    import json

    payload = _canonical(_receipt_signature(receipt))
    return fingerprint_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _receipt_signature(receipt: DecisionReceipt) -> dict[str, Any]:
    """The fields that the integrity digest binds (excludes the digest itself)."""

    return {
        "decision_id": receipt.decision_id,
        "canonical_task_spec": receipt.canonical_task_spec,
        "policy_version": receipt.policy_version,
        "candidate_catalog_digest": receipt.candidate_catalog_digest,
        "admitted_set": list(receipt.admitted_set),
        "exclusions": list(receipt.exclusions),
        "outcome": receipt.outcome,
        "advisory_consulted": receipt.advisory_consulted,
    }


# ---------------------------------------------------------------------------
# Outcome composition (R4): aggregate of per-candidate eligibility verdicts.
# ---------------------------------------------------------------------------


def _compose_outcome(
    admitted: Sequence[ModelInfo], records: Sequence[EligibilityRecord], preferred_id: str | None
) -> tuple[str, str]:
    """Compose the decision outcome from per-candidate eligibility verdicts.

    * ``denied`` iff the admitted set is empty, or every record is hard-blocked.
    * ``degraded`` iff a non-empty admitted set exists AND the preferred
      candidate was excluded (e.g. fail-closed availability) -- the admitted
      subset survives, only the preferred is excluded.
    * ``accepted`` otherwise.
    """

    if not admitted:
        if records and all(not r.admitted for r in records):
            return OUTCOME_DENIED, _DENIED_ALL_BLOCKED
        return OUTCOME_DENIED, _DENIED_NO_CANDIDATES

    if preferred_id is not None:
        admitted_ids = {getattr(m, "id", None) for m in admitted}
        excluded_preferred = any(r.model_id == preferred_id and not r.admitted for r in records)
        if excluded_preferred and preferred_id not in admitted_ids:
            return OUTCOME_DEGRADED, _DEGRADED_PREFERRED_DOWN

    return OUTCOME_ACCEPTED, _ACCEPTED_REASON


# ---------------------------------------------------------------------------
# Contracts: data model (data-model.md, contracts/decision-receipt.md).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DecisionReceipt:
    """Tamper-evident, input-derivable binding of a decision id to its I/O.

    Verifiable from inputs alone with no producer trust (FR-004, NFR-001).
    Derived, never persisted (research.md R3).
    """

    decision_id: str
    canonical_task_spec: str
    policy_version: str
    candidate_catalog_digest: str
    admitted_set: list[str]
    exclusions: list[dict[str, Any]]
    outcome: str
    integrity_digest: str
    advisory_consulted: str | None

    def to_dict(self) -> dict[str, Any]:
        import dataclasses as _dc

        return _dc.asdict(self)


@dataclass
class DecisionRecord:
    """The authority's single output (FR-001): the canonical decision."""

    decision_id: str
    task_spec_canonical: str
    policy_version: str
    outcome: str
    admitted: list[ModelInfo]
    exclusions: list[dict[str, Any]]
    receipt: DecisionReceipt
    route_contract: RoutingDecisionContract
    advisory_influence: dict[str, Any] | None
    protected: bool
    dev_mode: bool = False


class VerificationFault(str, Enum):
    """Faults an independent auditor can detect by recomputing from inputs.

    ``str(member)``/``print(member)`` yield the member NAME -- matching the
    contract/quickstart documentation (e.g. ``DECISION_ID_DIVERGENT``), not the
    Python 3.11+ ``Enum.NAME`` repr -- so the auditor-facing shell output is the
    documented label.
    """

    DECISION_ID_DIVERGENT = "decision_id_divergent"
    RECEIPT_TAMPERED = "receipt_tampered"
    ACCEPTED_ON_INVALID = "accepted_on_invalid"

    def __str__(self) -> str:
        return self.name


# ---------------------------------------------------------------------------
# Authority entrypoint (FR-001).
# ---------------------------------------------------------------------------


@dataclass
class AdvisoryInput:
    """Optional advisory input to :func:`decide` (FR-005).

    ``ranker`` defaults to the facade's baseline (non-shadow) adaptive ranker,
    which reorders admitted candidates by configured rules. ``escalation``
    optionally surfaces an escalation tier bump (from :func:`escalation.scan`)
    as an advisory influence only.
    """

    ranker: AdaptiveRanker | None = None
    requested_tier: int | None = None
    escalation: tuple[int | None, str | None] | None = None
    label: str | None = None


def decide(
    *,
    task_spec: TaskSpec,
    policy_version: str,
    candidates: Sequence[ModelInfo],
    availability_truth: Mapping[str, Any],
    protected: bool,
    advisory: AdvisoryInput | None = None,
    now: datetime | None = None,
    dev_mode: bool = False,
) -> DecisionRecord:
    """Produce the canonical, deterministic decision for a task (FR-001).

    Parameters
    ----------
    task_spec:
        The versioned task request (consumed, not redefined).
    policy_version:
        The policy version the decision is made under (part of the id canonical form).
    candidates:
        The candidate catalog to consider.
    availability_truth:
        Mapping ``model_id -> AvailabilityReport``-like truth supplied by the
        caller (fixtures, no network). Charge-free: the facade wraps this in a
        closure and injects it as the gate's ``availability_source``.
    protected:
        When True, absent/failed availability truth fails closed (FR-006, NFR-002).
    advisory:
        Optional advisory input; ``None`` is the deterministic baseline. Advisory
        reorders admitted candidates only and can never weaken hard policy (FR-005).
    now:
        Optional clock injection for determinism; ``None`` uses :data:`DETERMINISTIC_EPOCH`.
    dev_mode:
        Forwards to the eligibility gate's dev-mode relaxation toggle.

    Returns
    -------
    DecisionRecord
        The single canonical decision with id, admitted set, exclusions,
        receipt, and a backwards-compatible ``RoutingDecisionContract`` projection.
    """

    clock = now if now is not None else DETERMINISTIC_EPOCH

    # Capability pre-filter (FR-006): a candidate missing required capabilities
    # is excluded before the availability gate sees it. This composes the public
    # canonical_capability canonicalizer rather than duplicating the comparison,
    # and mirrors how select_capable_candidates/_policy_reason admit by gap.
    required_caps = list(getattr(task_spec, "required_capabilities", []) or [])
    capable, capability_records = _capability_gate(candidates, required_caps)

    def availability_source(model_id: str) -> AvailabilityReport | None:
        # Absent truth -> None, which EligibilityGate._state_for maps to the
        # "unknown" state (fail-closed for protected work). This matches the
        # gate's documented runtime contract even though its __init__ annotation
        # declares a non-Optional AvailabilityReport return.
        truth = availability_truth.get(model_id)
        if truth is None:
            return None
        if isinstance(truth, AvailabilityReport):
            return truth
        return None  # fixture-supplied non-report surface -> treat as absent

    # Availability/fail-closed gate over the capability-passing subset only.
    gate = EligibilityGate(
        cast("Callable[[str], AvailabilityReport | None]", availability_source),  # type: ignore[arg-type]
        protected_fail_closed=protected,
        clock=clock,
    )
    # FR-006 re-check point: this is the single, synchronous evaluate() call
    # for the whole decision. Everything downstream (advisory reorder) may
    # only reorder this admitted set -- it can never reintroduce an excluded
    # candidate or admit one this call didn't -- so eligibility is always
    # authoritative as of the moment immediately preceding final selection.
    eligibility: EligibilityResult = gate.evaluate(
        [c for c in capable], protected=protected, dev_mode=dev_mode, now=clock
    )

    admitted = list(eligibility.admitted)
    records = list(eligibility.records) + capability_records
    exclusions = [_exclusion_projection(r) for r in records if not r.admitted]

    # Advisory reorder (FR-005): order only already-admitted candidates.
    advisory_influence: dict[str, Any] | None = None
    advisory_consulted: str | None = None
    if advisory is not None:
        ranker = advisory.ranker if advisory.ranker is not None else _baseline_ranker()
        advisory_consulted = advisory.label or "adaptive-ranker"
        try:
            ranked: RankerOutput = ranker.rank(eligibility, task_spec)
        except Exception:  # pragma: no cover - advisory must never break the hard decision
            ranked = RankerOutput(
                ranked=admitted,
                scores={},
                reasoning={"advisory": "failed; baseline admitted set retained"},
                candidate_set_hash="",
                eligibility_hash="",
                mode=ranker.config.mode,
                shadow=True,
                version="baseline",
                canary_policy=ranker.config.canary_policy,
            )
        ranked_ids = [getattr(m, "id", None) for m in ranked.ranked]
        admitted_id_set = {getattr(m, "id", None) for m in admitted}
        # Refuse + audit-flag any advisory attempt to introduce a non-admitted
        # candidate or drop an admitted one (SC-002). Membership is invariant;
        # only ordering may change.
        reordered: list[ModelInfo] = [
            m for m in ranked.ranked if getattr(m, "id", None) in admitted_id_set
        ]
        # Preserve any admitted candidate the ranker omitted (advisory cannot drop membership).
        for m in admitted:
            if getattr(m, "id", None) not in ranked_ids:
                reordered.append(m)
        admitted = reordered
        advisory_influence = {
            "consulted": advisory_consulted,
            "requested_tier": advisory.requested_tier,
            "escalation": list(advisory.escalation) if advisory.escalation is not None else None,
            "reordering": ranked_ids,
            "scores": dict(ranked.scores),
            "reasoning": dict(ranked.reasoning),
            "shadow": bool(ranked.shadow),
        }
    elif candidates:
        # Surface a read-only escalation scan as an advisory influence even with
        # no explicit advisory input (R5). ``escalation.scan`` is pure/read-only.
        bump = escalation_scan(getattr(task_spec, "objective", ""), None)
        advisory_influence = {"escalation_scan": list(bump)} if bump != (None, None) else None

    preferred_id = _preferred_id(task_spec, admitted, records)
    outcome, reason_code = _compose_outcome(admitted, records, preferred_id)

    canonical_task_spec = compute_canonical_task_spec(task_spec)
    catalog_digest = compute_candidate_catalog_digest(candidates)
    admitted_ids = [getattr(m, "id", None) for m in admitted]
    admitted_set: list[str] = [mid for mid in admitted_ids if mid is not None]

    # Decision id binds the canonical task spec, policy version, catalog digest,
    # and admitted MEMBERSHIP + exclusions (FR-002). Membership -- not order --
    # is bound so the advisory reorder (FR-005) cannot perturb the id: advisory-on
    # and advisory-off produce a byte-identical id, differing only in
    # advisory_influence/advisory_consulted.
    decision_id = _compute_decision_id(
        canonical_task_spec, policy_version, catalog_digest, admitted_ids, exclusions
    )

    receipt = DecisionReceipt(
        decision_id=decision_id,
        canonical_task_spec=canonical_task_spec,
        policy_version=policy_version,
        candidate_catalog_digest=catalog_digest,
        admitted_set=list(admitted_set),
        exclusions=list(exclusions),
        outcome=reason_code,
        integrity_digest="",  # stamped below
        advisory_consulted=advisory_consulted,
    )
    receipt = _stamp_receipt_digest(receipt)

    route_contract = _project_route_contract(
        task_spec=task_spec,
        policy_version=policy_version,
        admitted=admitted,
        exclusions=exclusions,
        explanation=reason_code,
        advisory_influence=advisory_influence,
        decision_id=decision_id,
        receipt=receipt.to_dict(),
    )

    return DecisionRecord(
        decision_id=decision_id,
        task_spec_canonical=canonical_task_spec,
        policy_version=policy_version,
        outcome=outcome,
        admitted=admitted,
        exclusions=exclusions,
        receipt=receipt,
        route_contract=route_contract,
        advisory_influence=advisory_influence,
        protected=protected,
        dev_mode=dev_mode,
    )


def _stamp_receipt_digest(receipt: DecisionReceipt) -> DecisionReceipt:
    """Return a new receipt with the integrity digest computed over its signature."""

    import dataclasses as _dc

    digest = compute_receipt_digest(receipt)
    return _dc.replace(receipt, integrity_digest=digest)


def _compute_decision_id(
    canonical_task_spec: str,
    policy_version: str,
    catalog_digest: str,
    admitted_ids: list[str | None],
    exclusions: list[dict[str, Any]],
) -> str:
    import json

    # Membership, not order: sort the deduplicated id set so an advisory reorder
    # (FR-005) cannot perturb the id. Eliding None ids keeps the membership set
    # deterministic; advisory-on and advisory-off then share a byte-identical id.
    admitted_membership: list[str] = sorted({mid for mid in admitted_ids if mid is not None})
    payload = {
        "canonical_task_spec": canonical_task_spec,
        "policy_version": policy_version,
        "candidate_catalog_digest": catalog_digest,
        "admitted_set": admitted_membership,
        "exclusions": _canonical(exclusions),
    }
    return fingerprint_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _preferred_id(
    task_spec: TaskSpec, admitted: Sequence[ModelInfo], records: Sequence[EligibilityRecord]
) -> str | None:
    """The candidate the decision would otherwise prefer (for the degraded leg).

    This is the candidate the router would pick first absent any degradation --
    not necessarily the first-admitted (which may itself be the degraded
    survivor). With no escalate-tier advisory consulted, that is the
    highest-tier eligible-seeming candidate by catalog ordering; the degraded
    leg fires when that preferred candidate is excluded but an admitted subset
    survives.
    """

    # Prefer the first record's model -- regardless of admission -- because that
    # is the catalog-order candidate the router would otherwise pick; if it is
    # the one fail-closed against a surviving subset, the outcome is degraded.
    if records:
        return records[0].model_id
    if admitted:
        return getattr(admitted[0], "id", None)
    return None


def _exclusion_projection(record: EligibilityRecord) -> dict[str, Any]:
    """Per-candidate exclusion with stable reason + sourced state (FR-003)."""

    return {
        "model_id": record.model_id,
        "provider": record.provider,
        "verdict": record.verdict.value
        if hasattr(record.verdict, "value")
        else str(record.verdict),
        "state": record.state,
        "source": record.source,
        "reason": record.reason,
    }


def _capability_gate(
    candidates: Sequence[ModelInfo], required: Sequence[str]
) -> tuple[list[ModelInfo], list[EligibilityRecord]]:
    """Capability pre-filter: admit only candidates satisfying ``required``.

    Composes the public :func:`canonical_capability` canonicalizer (the same one
    ``select_capable_candidates``/``_policy_reason`` use) rather than duplicating
    comparison logic. Candidates missing required capabilities are returned as
    capability-excluded ``EligibilityRecord`` rows with a ``NOT_REQUESTED_TIER``
    verdict-family reason, so they surface in the exclusion list (FR-003) and can
    drive the ``denied`` outcome (FR-006). Pure, no network.
    """
    required_set: frozenset[str] = frozenset(
        canonical_capability(r) for r in required if r is not None
    )
    capable: list[ModelInfo] = []
    excluded_records: list[EligibilityRecord] = []
    for model in candidates:
        model_caps = frozenset(
            canonical_capability(c) for c in getattr(model, "capabilities", ()) or ()
        )
        missing = sorted(required_set - model_caps)
        if missing:
            excluded_records.append(
                EligibilityRecord(
                    model_id=getattr(model, "id", ""),
                    provider=getattr(model, "provider", None) or "unknown",
                    admitted=False,
                    verdict=EligibilityVerdict.NOT_REQUESTED_TIER,
                    state="missing_capability",
                    source="capability_gate",
                    reason=f"missing capability: {', '.join(missing)}",
                )
            )
        else:
            capable.append(model)
    return capable, excluded_records


def _project_route_contract(
    *,
    task_spec: TaskSpec,
    policy_version: str,
    admitted: Sequence[ModelInfo],
    exclusions: list[dict[str, Any]],
    explanation: str,
    advisory_influence: dict[str, Any] | None,
    decision_id: str,
    receipt: dict[str, Any],
) -> RoutingDecisionContract:
    """Backwards-compatible projection into the existing contract (FR-008)."""

    selected_route: dict[str, Any] = {}
    if admitted:
        first = admitted[0]
        selected_route = {
            "model": getattr(first, "id", None),
            "provider": getattr(first, "provider", None),
        }

    candidate_snapshot = {
        "admitted_count": len(admitted),
        "admitted_ids": [getattr(m, "id", None) for m in admitted],
    }

    return RoutingDecisionContract(
        selected_route=selected_route,
        task_spec=task_spec.to_dict() if hasattr(task_spec, "to_dict") else {},
        candidate_snapshot=candidate_snapshot,
        exclusions=exclusions,
        policy_floor="high" if _is_protected(task_spec=task_spec) else "standard",
        planner_mode="decision-kernel",
        explanation=explanation,
        adaptive_influence=advisory_influence or {},
        fallback_plan=[],
        policy_version=policy_version,
        decision_id=decision_id,
        receipt=receipt,
    )


def _is_protected(*, task_spec: TaskSpec) -> bool:
    """Heuristic protected flag for policy_floor projection only.

    The hard fail-closed behavior is governed by the ``protected`` argument to
    :func:`decide` (forwarded to the gate); this helper only labels the
    projection so a reader can infer the policy floor at a glance.
    """

    risk = getattr(task_spec, "risk", None)
    if isinstance(risk, str):
        return risk.lower() in {"high", "critical", "protected"}
    if isinstance(risk, dict):
        level = str(risk.get("level", "")).lower()
        return level in {"high", "critical", "protected"}
    return False


# ---------------------------------------------------------------------------
# Independent verification (FR-004, P4).
# ---------------------------------------------------------------------------


def verify_decision(
    record: DecisionRecord,
    *,
    task_spec: TaskSpec,
    policy_version: str,
    candidates: Sequence[ModelInfo],
    availability_truth: Mapping[str, Any],
    protected: bool,
    dev_mode: bool = False,
) -> VerificationFault | None:
    """Independently verify a decision from supplied inputs; return a fault or None.

    No producer trust (FR-004, SC-001). Two independent checks:

    1. **Receipt integrity** -- recompute the receipt's integrity digest from the
       fields the receipt itself carries and compare to the stored digest. This
       detects any tampering with the bounds recorded on the receipt
       (``RECEIPT_TAMPERED``).
    2. **Decision reproducibility** -- re-run :func:`decide` over the supplied
       inputs (using the caller-supplied ``protected``/``dev_mode``, which the
       authority stamped onto the record) and compare the recomputed
       ``decision_id`` and outcome to the record's claimed values. An id mismatch
       means a recorded input diverges from the supplied inputs
       (``DECISION_ID_DIVERGENT``); a record claiming ``accepted`` whose
       recomputed outcome is denied means the producer overstated the outcome
       (``ACCEPTED_ON_INVALID``).
    """

    # 1) Receipt is self-verifying: its bounds are all carried on the receipt.
    recomputed_digest = compute_receipt_digest(record.receipt)
    if recomputed_digest != record.receipt.integrity_digest:
        return VerificationFault.RECEIPT_TAMPERED

    # 2) Reproduce the full decision to check the id and outcome.
    recomputed = decide(
        task_spec=task_spec,
        policy_version=policy_version,
        candidates=candidates,
        availability_truth=availability_truth,
        protected=protected,
        dev_mode=dev_mode,
        advisory=None,  # the deterministic baseline bounds the id; advisory does not change it
    )

    if recomputed.decision_id != record.decision_id:
        return VerificationFault.DECISION_ID_DIVERGENT

    if record.outcome == OUTCOME_ACCEPTED and recomputed.outcome != OUTCOME_ACCEPTED:
        return VerificationFault.ACCEPTED_ON_INVALID

    return None
