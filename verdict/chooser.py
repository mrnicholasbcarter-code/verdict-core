"""Evidence-based model chooser for BOD-95.

Reuses EligibilityGate via select_eligible_route. The ranker is advisory only
and never reintroduces excluded candidates. Resource class is config/evidence
mapped, never inferred from brand prefixes.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from verdict.autodev_routing import CandidateEvidence, RouteSelection, select_eligible_route
from verdict.availability import AvailabilityState

POLICY_VERSION = "chooser-policy/v1"
RANKER_VERSION = "chooser-ranker/v1"

FREE = "free"
SUBSCRIPTION_WORKER = "subscription_worker"
SUBSCRIPTION_PREMIUM = "subscription_premium"
METERED = "metered"
LOCAL = "local"

RESOURCE_CLASSES = (FREE, SUBSCRIPTION_WORKER, SUBSCRIPTION_PREMIUM, METERED, LOCAL)

PROTECTED_TASK_CLASSES = frozenset({"architecture", "orchestration", "hard-debug", "final-review"})

# Ordinary work: cheaper classes first. Protected work prefers premium.
_ORDINARY_CLASS_SCORE = {
    FREE: 500.0,
    SUBSCRIPTION_WORKER: 400.0,
    LOCAL: 300.0,
    SUBSCRIPTION_PREMIUM: 200.0,
    METERED: 100.0,
}
_PROTECTED_CLASS_SCORE = {
    SUBSCRIPTION_PREMIUM: 500.0,
    SUBSCRIPTION_WORKER: 300.0,
    FREE: 200.0,
    LOCAL: 150.0,
    METERED: 100.0,
}

_NO_ELIGIBLE = "no_eligible_target"


class ChooserError(ValueError):
    """Fail-closed chooser error with a machine-stable reason code."""

    def __init__(
        self, reason: str, message: str, *, exclusions: tuple[dict[str, str], ...] = ()
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.exclusions = exclusions


def task_class_is_protected(task_class: str) -> bool:
    return task_class in PROTECTED_TASK_CLASSES


def resource_class_from_evidence(evidence: CandidateEvidence) -> str:
    """Return a normalized resource class from evidence, never brand prefixes."""
    raw = evidence.capabilities.get("resource_class")
    if raw in RESOURCE_CLASSES:
        return raw
    return FREE


def _identity(evidence: CandidateEvidence) -> dict[str, str]:
    route = evidence.route
    return {
        "gateway": route.gateway_id,
        "provider": route.provider,
        "resource_pool": resource_class_from_evidence(evidence),
        "model": route.model_id,
        "route_id": route.route_id,
    }


def _matches_explicit(evidence: CandidateEvidence, explicit_model: str) -> bool:
    explicit = explicit_model.strip()
    return explicit in {
        evidence.requested_alias,
        evidence.route.model_id,
        evidence.route.route_id,
        f"{evidence.route.gateway_id}/{evidence.route.model_id}",
    }


def _capability_ok(evidence: CandidateEvidence, requires: Sequence[str]) -> bool:
    for capability in requires:
        name = capability.strip()
        if not name:
            continue
        if evidence.capability_status(name) != "observed":
            return False
    return True


def _unknown_fields(evidence: CandidateEvidence) -> tuple[str, ...]:
    unknown: list[str] = []
    if evidence.quota_remaining_pct is None:
        unknown.append("quota")
    if evidence.headroom_pct is None:
        unknown.append("headroom")
    if evidence.freshness_seconds is None:
        unknown.append("freshness_seconds")
    for name, status in sorted(evidence.capabilities.items()):
        if status == "unknown":
            unknown.append(f"capability:{name}")
    return tuple(unknown)


def _quality_score(evidence: CandidateEvidence) -> float:
    """Observed health/headroom may raise rank; UNKNOWN never outranks observed."""
    score = 0.0
    if evidence.headroom_pct is None:
        score += 0.0
    else:
        score += evidence.headroom_pct / 100.0
    if evidence.quota_remaining_pct is None:
        score += 0.0
    else:
        score += evidence.quota_remaining_pct / 200.0
    if evidence.availability is AvailabilityState.ELIGIBLE:
        score += 0.05
    return score


def production_ranker(
    task_class: str, explicit_model: str | None = None
) -> Callable[[CandidateEvidence], float]:
    """Higher is better. EligibilityGate already filtered the input set."""

    class_scores = (
        _PROTECTED_CLASS_SCORE if task_class_is_protected(task_class) else _ORDINARY_CLASS_SCORE
    )

    def _rank(candidate: CandidateEvidence) -> float:
        score = class_scores.get(resource_class_from_evidence(candidate), 0.0)
        score += _quality_score(candidate)
        if explicit_model and _matches_explicit(candidate, explicit_model):
            score += 10_000.0
        # Stable tie-break: gateway/provider/model identity, not brand.
        identity = (
            f"{candidate.route.gateway_id}\0{candidate.route.provider}\0"
            f"{candidate.route.route_id}\0{candidate.route.model_id}"
        )
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        score += int(digest[:8], 16) / 1e12
        return score

    return _rank


def _exclusion(evidence: CandidateEvidence, reason: str) -> dict[str, str]:
    identity = _identity(evidence)
    return {
        "gateway": identity["gateway"],
        "provider": identity["provider"],
        "resource_pool": identity["resource_pool"],
        "model": identity["model"],
        "route_id": identity["route_id"],
        "reason": reason,
    }


def _prefilter(
    candidates: Sequence[CandidateEvidence], requires: Sequence[str]
) -> tuple[list[CandidateEvidence], list[dict[str, str]]]:
    kept: list[CandidateEvidence] = []
    exclusions: list[dict[str, str]] = []
    for candidate in candidates:
        if not _capability_ok(candidate, requires):
            missing = [
                name for name in requires if candidate.capability_status(name.strip()) != "observed"
            ]
            exclusions.append(_exclusion(candidate, "capability_mismatch:" + ",".join(missing)))
            continue
        kept.append(candidate)
    return kept, exclusions


@dataclass(frozen=True)
class ChooseReceipt:
    """Versioned machine contract consumed by Prime."""

    task_class: str
    protected: bool
    selected: dict[str, str] | None
    evidence_digest: str | None
    evidence_freshness_seconds: float | None
    ranked_fallbacks: tuple[dict[str, str], ...]
    exclusions: tuple[dict[str, str], ...]
    ranking_factors: dict[str, Any]
    unknown_evidence_fields: tuple[str, ...]
    policy_version: str
    ranker_version: str
    explicit_model: str | None
    reason: str
    selected_because: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_class": self.task_class,
            "protected": self.protected,
            "selected": self.selected,
            "evidence_digest": self.evidence_digest,
            "evidence_freshness_seconds": self.evidence_freshness_seconds,
            "ranked_fallbacks": list(self.ranked_fallbacks),
            "exclusions": list(self.exclusions),
            "ranking_factors": dict(self.ranking_factors),
            "unknown_evidence_fields": list(self.unknown_evidence_fields),
            "policy_version": self.policy_version,
            "ranker_version": self.ranker_version,
            "explicit_model": self.explicit_model,
            "reason": self.reason,
            "selected_because": self.selected_because,
        }


def _digest(evidence: CandidateEvidence) -> str:
    payload = json.dumps(
        evidence.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _because(selected: CandidateEvidence, *, task_class: str, explicit_model: str | None) -> str:
    pool = resource_class_from_evidence(selected)
    if explicit_model and _matches_explicit(selected, explicit_model):
        return (
            f"selected because explicit model {explicit_model} is eligible "
            f"and beats automatic ranking"
        )
    if task_class_is_protected(task_class):
        return (
            f"selected because protected task class {task_class} prefers eligible {pool} capacity"
        )
    return (
        f"selected because ordinary task class {task_class} prefers eligible "
        f"{pool} capacity over more expensive classes"
    )


def _receipt_from_selection(
    selection: RouteSelection,
    *,
    task_class: str,
    explicit_model: str | None,
    extra_exclusions: Sequence[dict[str, str]],
    candidates_by_alias: Mapping[str, CandidateEvidence],
    candidates_by_model: Mapping[str, list[CandidateEvidence]],
) -> ChooseReceipt:
    selected = selection.selected
    selected_key = (
        selected.route.gateway_id,
        selected.route.provider,
        selected.route.route_id,
        selected.route.model_id,
    )
    fallbacks: list[dict[str, str]] = []
    seen: set[tuple[str, str, str, str]] = {selected_key}
    admitted_model_ids = set(selection.admitted_ids) or {selected.route.model_id}
    # EligibilityGate keys admission by model_id, so same-model identities from
    # other gateways are still admitted. Keep them distinct in the receipt.
    for model_id in (*selection.ranked_ids, *admitted_model_ids):
        for candidate in candidates_by_model.get(model_id, ()):
            key = (
                candidate.route.gateway_id,
                candidate.route.provider,
                candidate.route.route_id,
                candidate.route.model_id,
            )
            if key in seen:
                continue
            seen.add(key)
            fallbacks.append(_identity(candidate))
    exclusions = list(extra_exclusions)
    for alias in selection.exclusion_reasons:
        excluded = candidates_by_alias.get(alias)
        if excluded is None:
            exclusions.append({"model": alias, "reason": "not_admitted"})
            continue
        state = excluded.availability.value
        exclusions.append(_exclusion(excluded, f"not_admitted:{state}"))
    ranking_factors = {
        "resource_class_order": (
            list(_PROTECTED_CLASS_SCORE)
            if task_class_is_protected(task_class)
            else list(_ORDINARY_CLASS_SCORE)
        ),
        "explicit_model_wins": bool(explicit_model),
        "quality": "observed_headroom_then_quota_unknown_never_promoted",
    }
    return ChooseReceipt(
        task_class=task_class,
        protected=task_class_is_protected(task_class),
        selected=_identity(selected),
        evidence_digest=_digest(selected),
        evidence_freshness_seconds=selected.freshness_seconds,
        ranked_fallbacks=tuple(fallbacks),
        exclusions=tuple(exclusions),
        ranking_factors=ranking_factors,
        unknown_evidence_fields=_unknown_fields(selected),
        policy_version=POLICY_VERSION,
        ranker_version=RANKER_VERSION,
        explicit_model=explicit_model,
        reason="selected",
        selected_because=_because(selected, task_class=task_class, explicit_model=explicit_model),
    )


def choose_route(
    candidates: Iterable[CandidateEvidence],
    *,
    task_class: str,
    requires: Sequence[str] = (),
    explicit_model: str | None = None,
) -> ChooseReceipt:
    """Apply hard eligibility, then the production advisory ranker."""
    candidate_list = list(candidates)
    kept, pre_exclusions = _prefilter(candidate_list, requires)
    by_alias = {candidate.requested_alias: candidate for candidate in candidate_list}
    by_model: dict[str, list[CandidateEvidence]] = {}
    for candidate in candidate_list:
        by_model.setdefault(candidate.route.model_id, []).append(candidate)

    if explicit_model:
        matches = [
            candidate
            for candidate in candidate_list
            if _matches_explicit(candidate, explicit_model)
        ]
        if not matches:
            raise ChooserError(
                "explicit_model_not_found",
                f"explicit model {explicit_model} was not in the candidate set",
                exclusions=tuple(pre_exclusions),
            )

    if not kept:
        raise ChooserError(
            _NO_ELIGIBLE,
            "no eligible route; exclusions: "
            + ", ".join(item["reason"] for item in pre_exclusions),
            exclusions=tuple(pre_exclusions),
        )

    try:
        selection = select_eligible_route(
            kept,
            ranker=production_ranker(task_class, explicit_model),
            protected=task_class_is_protected(task_class),
        )
    except ValueError as exc:
        raise ChooserError(
            _NO_ELIGIBLE,
            str(exc),
            exclusions=tuple(pre_exclusions)
            + tuple(
                {"model": alias, "reason": "not_admitted"}
                for alias in str(exc).split("exclusions:", 1)[-1].split(",")
                if alias.strip()
            ),
        ) from exc

    if explicit_model:
        admitted: list[CandidateEvidence] = []
        for model_id in selection.admitted_ids:
            admitted.extend(by_model.get(model_id, ()))
        if not any(_matches_explicit(candidate, explicit_model) for candidate in admitted):
            matches = [
                candidate
                for candidate in candidate_list
                if _matches_explicit(candidate, explicit_model)
            ]
            exclusions = list(pre_exclusions)
            for candidate in matches:
                exclusions.append(
                    _exclusion(candidate, f"explicit_ineligible:{candidate.availability.value}")
                )
            raise ChooserError(
                "explicit_model_ineligible",
                f"explicit model {explicit_model} is ineligible; refusing silent substitute",
                exclusions=tuple(exclusions),
            )
        if not _matches_explicit(selection.selected, explicit_model):
            raise ChooserError(
                "explicit_model_ineligible",
                f"explicit model {explicit_model} is ineligible; refusing silent substitute",
            )

    return _receipt_from_selection(
        selection,
        task_class=task_class,
        explicit_model=explicit_model,
        extra_exclusions=pre_exclusions,
        candidates_by_alias=by_alias,
        candidates_by_model=by_model,
    )


def human_summary(receipt: ChooseReceipt) -> str:
    lines: list[str] = []
    if receipt.selected is None:
        lines.append(f"no eligible target ({receipt.reason})")
    else:
        selected = receipt.selected
        lines.append(
            f"selected {selected['gateway']}/{selected['provider']}/"
            f"{selected['resource_pool']}/{selected['model']}"
        )
        lines.append(receipt.selected_because)
    if receipt.ranked_fallbacks:
        top = receipt.ranked_fallbacks[0]
        lines.append(
            "top fallback: "
            f"{top['gateway']}/{top['provider']}/{top['resource_pool']}/{top['model']}"
        )
    if receipt.exclusions:
        reasons = ", ".join(
            f"{item.get('model', '?')} ({item.get('reason', 'excluded')})"
            for item in receipt.exclusions[:3]
        )
        lines.append(f"excluded: {reasons}")
    return "\n".join(lines)


def evidence_from_mapping(raw: Mapping[str, Any]) -> CandidateEvidence:
    """Build CandidateEvidence from a JSON fixture mapping."""
    from verdict.gateway_adapters import AdapterRouteIdentity

    route_raw = raw.get("route")
    if not isinstance(route_raw, Mapping):
        raise ChooserError("invalid_candidates", "candidate route object is required")
    observed_raw = str(raw.get("observed_at") or datetime.now(timezone.utc).isoformat())
    observed = datetime.fromisoformat(observed_raw.replace("Z", "+00:00"))
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    capabilities = raw.get("capabilities") or {}
    if not isinstance(capabilities, Mapping):
        raise ChooserError("invalid_candidates", "capabilities must be an object")
    return CandidateEvidence(
        requested_alias=str(raw.get("requested_alias") or route_raw.get("model_id") or ""),
        route=AdapterRouteIdentity(
            gateway_id=str(route_raw["gateway_id"]),
            route_id=str(route_raw["route_id"]),
            provider=str(route_raw["provider"]),
            model_id=str(route_raw["model_id"]),
            protocol=str(route_raw.get("protocol") or "openai.chat"),
        ),
        availability=AvailabilityState(str(raw.get("availability") or "eligible")),
        capabilities={str(key): str(value) for key, value in capabilities.items()},
        observed_at=observed,
        ttl_seconds=int(raw.get("ttl_seconds") or 60),
        source=str(raw.get("source") or "chooser-fixture"),
        freshness_seconds=raw.get("freshness_seconds"),
        quota_remaining_pct=raw.get("quota_remaining_pct"),
        headroom_pct=raw.get("headroom_pct"),
    )


def load_candidates_json(path: str) -> tuple[CandidateEvidence, ...]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(payload, Mapping) and "candidates" in payload:
        payload = payload["candidates"]
    if not isinstance(payload, list):
        raise ChooserError("invalid_candidates", "candidates JSON must be a list")
    return tuple(evidence_from_mapping(item) for item in payload)
