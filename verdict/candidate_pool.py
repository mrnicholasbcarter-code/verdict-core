"""Candidate Pool Intelligence — bounded evidence-fused Top-K shortlist (BOD-122).

Converts a huge advertised model universe into a small, high-recall,
evidence-backed pool of potentially capable concrete routes.

Funnel: discover → normalize identity (via BOD-121 ``lookup_omniroute_id``) →
hard eliminate → task fingerprint → evidence fuse → targeted probe ladder →
alias/family dedupe → provider diversity → Top-K.

Hard-excluded routes are never restored by scoring. Unknown stays unknown.
Qualification is an information-value decision — never a catalog-wide benchmark.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any

from verdict.availability import is_opaque_route_id
from verdict.capability_gate import TaskRequirements, derive_requirements
from verdict.metadata.mapping import IdentityMap
from verdict.metadata.records import (
    DROP_MAP_TARGET_MISSING,
    DROP_STALE,
    MetadataLookup,
    ModelMetadataError,
    ModelMetadataRecord,
    ProvenancedField,
    resolve_required_name,
)
from verdict.metadata.store import MetadataSnapshot, lookup_omniroute_id, model_id_leaf
from verdict.task_profile import TaskProfile

# ── Drop / health / probe constants ─────────────────────────────────────────

DROP_UNMAPPED = "unmapped"
DROP_REQUIRED_UNKNOWN = "required_unknown"
DROP_CAPABILITY_MISMATCH = "capability_mismatch"
DROP_HARD_HEALTH = "hard_health"
DROP_OPAQUE_AUTO = "opaque_auto"
DROP_STALE_METADATA = "stale"
DROP_POLICY = "policy_excluded"

HEALTH_OK = "ok"
HEALTH_FAILED = "failed"
HEALTH_UNKNOWN = "unknown"

PROBE_STATIC_METADATA = "static_metadata"
PROBE_FRESH_CACHE = "fresh_cache"
PROBE_CHEAP_HEALTH = "cheap_health"
PROBE_SMALL_QUAL = "small_qual"
PROBE_LARGER_EVAL = "larger_eval"

_PROBE_LADDER: tuple[str, ...] = (
    PROBE_STATIC_METADATA,
    PROBE_FRESH_CACHE,
    PROBE_CHEAP_HEALTH,
    PROBE_SMALL_QUAL,
    PROBE_LARGER_EVAL,
)

_BOOL_CAPS = frozenset({"tools", "vision", "structured", "reasoning", "attachment"})
_CONTEXT_CAP = "context"

ProbeFn = Callable[[str, str], Mapping[str, Any]]


class CandidatePoolError(ValueError):
    """Raised when the candidate pool cannot be built under fail-closed rules."""


# ── Task fingerprint ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TaskFingerprint:
    """Transparent, replayable description of the requested work."""

    digest: str
    task_family: str
    language: str | None = None
    framework: str | None = None
    edit_scope: str | None = None
    reasoning_depth: str = "unknown"
    required_capabilities: tuple[str, ...] = ()
    modalities: tuple[str, ...] = ()
    context_size: int | None = None
    proof_type: str | None = None
    risk: str = "unknown"
    latency_sensitivity: str = "unknown"
    decomposable: bool | None = None
    objective_preview: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "task_family": self.task_family,
            "language": self.language,
            "framework": self.framework,
            "edit_scope": self.edit_scope,
            "reasoning_depth": self.reasoning_depth,
            "required_capabilities": list(self.required_capabilities),
            "modalities": list(self.modalities),
            "context_size": self.context_size,
            "proof_type": self.proof_type,
            "risk": self.risk,
            "latency_sensitivity": self.latency_sensitivity,
            "decomposable": self.decomposable,
            "objective_preview": self.objective_preview,
        }


def fingerprint_task(
    task: str,
    *,
    context: Mapping[str, Any] | None = None,
    requirements: TaskRequirements | None = None,
) -> TaskFingerprint:
    """Derive a stable task fingerprint. Task text is not a capability oracle."""
    if not isinstance(task, str):
        raise CandidatePoolError("task must be a string")
    payload = dict(context) if isinstance(context, Mapping) else {}
    reqs = requirements or derive_requirements(task, payload)
    language = _optional_str(payload.get("language"))
    framework = _optional_str(payload.get("framework"))
    edit_scope = _optional_str(payload.get("edit_scope"))
    reasoning_depth = _optional_str(payload.get("reasoning_depth")) or _infer_reasoning(task)
    proof_type = _optional_str(payload.get("proof_type"))
    risk = _optional_str(payload.get("risk")) or "unknown"
    latency = _optional_str(payload.get("latency_sensitivity")) or "unknown"
    decomposable = (
        payload.get("decomposable") if isinstance(payload.get("decomposable"), bool) else None
    )
    modalities = tuple(
        sorted(
            {
                *reqs.names,
                *(
                    str(m).strip()
                    for m in (payload.get("modalities") or ())
                    if isinstance(m, str) and m.strip()
                ),
            }
        )
    )
    family = _optional_str(payload.get("task_family")) or _infer_family(task, language)
    context_size = reqs.min_context
    raw_ctx = payload.get("context_size")
    if isinstance(raw_ctx, int) and not isinstance(raw_ctx, bool) and raw_ctx > 0:
        context_size = raw_ctx
    preview = re.sub(r"\s+", " ", task.strip())[:120]
    canonical = {
        "task_family": family,
        "language": language,
        "framework": framework,
        "edit_scope": edit_scope,
        "reasoning_depth": reasoning_depth,
        "required_capabilities": list(reqs.names),
        "modalities": list(modalities),
        "context_size": context_size,
        "proof_type": proof_type,
        "risk": risk,
        "latency_sensitivity": latency,
        "decomposable": decomposable,
        "objective": task.strip(),
        "min_context": reqs.min_context,
    }
    digest = _digest(canonical)
    return TaskFingerprint(
        digest=digest,
        task_family=family,
        language=language,
        framework=framework,
        edit_scope=edit_scope,
        reasoning_depth=reasoning_depth,
        required_capabilities=reqs.names,
        modalities=modalities,
        context_size=context_size,
        proof_type=proof_type,
        risk=risk,
        latency_sensitivity=latency,
        decomposable=decomposable,
        objective_preview=preview,
    )


# ── Evidence / health inputs ────────────────────────────────────────────────


@dataclass(frozen=True)
class RouteHealth:
    state: str = HEALTH_UNKNOWN
    detail: str | None = None
    fresh: bool = True

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"state": self.state, "fresh": self.fresh}
        if self.detail:
            payload["detail"] = self.detail
        return payload


@dataclass(frozen=True)
class RouteEvidence:
    """Caller-supplied soft evidence. Never invents capability."""

    task_success_rate: float | None = None
    eval_score: float | None = None
    latency_ms: float | None = None
    cost_per_million: float | None = None
    conflicts: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_success_rate": self.task_success_rate,
            "eval_score": self.eval_score,
            "latency_ms": self.latency_ms,
            "cost_per_million": self.cost_per_million,
            "conflicts": list(self.conflicts),
        }


@dataclass(frozen=True)
class OutcomeObservation:
    route_id: str
    task_family: str
    success: bool
    fingerprint_digest: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id,
            "task_family": self.task_family,
            "success": self.success,
            "fingerprint_digest": self.fingerprint_digest,
        }


@dataclass(frozen=True)
class ProbeBudget:
    max_probes: int = 4
    max_level: str = PROBE_SMALL_QUAL
    allow_larger_eval: bool = False

    def __post_init__(self) -> None:
        if self.max_probes < 0:
            raise CandidatePoolError("max_probes must be non-negative")
        if self.max_level not in _PROBE_LADDER:
            raise CandidatePoolError(f"unknown probe level: {self.max_level}")


# ── Receipt types ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class HardDrop:
    route_id: str
    reason: str
    detail: str | None = None

    def to_dict(self) -> dict[str, str]:
        payload = {"route_id": self.route_id, "reason": self.reason}
        if self.detail:
            payload["detail"] = self.detail
        return payload


@dataclass(frozen=True)
class ProbeRecord:
    route_id: str
    level: str
    passed: bool
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "route_id": self.route_id,
            "level": self.level,
            "passed": self.passed,
        }
        if self.detail:
            payload["detail"] = self.detail
        return payload


@dataclass(frozen=True)
class ShortlistEntry:
    route_id: str
    metadata_id: str | None
    provider: str
    family_key: str
    score: float
    confidence: float
    score_features: dict[str, float]
    inclusion_reason: str
    aliases: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id,
            "metadata_id": self.metadata_id,
            "provider": self.provider,
            "family_key": self.family_key,
            "score": self.score,
            "confidence": self.confidence,
            "score_features": dict(self.score_features),
            "inclusion_reason": self.inclusion_reason,
            "aliases": list(self.aliases),
        }


@dataclass(frozen=True)
class CandidatePoolReceipt:
    """Transparent, replayable shortlist receipt."""

    task_fingerprint: TaskFingerprint
    discovered_count: int
    hard_drops: tuple[HardDrop, ...]
    probes: tuple[ProbeRecord, ...]
    shortlist: tuple[ShortlistEntry, ...]
    uncertainty: tuple[str, ...]
    evidence_digest: str
    shortlist_digest: str
    requirements: tuple[str, ...] = ()
    eligible_count: int = 0
    confirmation: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_fingerprint": self.task_fingerprint.to_dict(),
            "discovered_count": self.discovered_count,
            "eligible_count": self.eligible_count,
            "requirements": list(self.requirements),
            "hard_drops": [d.to_dict() for d in self.hard_drops],
            "probes": [p.to_dict() for p in self.probes],
            "shortlist": [e.to_dict() for e in self.shortlist],
            "uncertainty": list(self.uncertainty),
            "evidence_digest": self.evidence_digest,
            "shortlist_digest": self.shortlist_digest,
            "confirmation": None if self.confirmation is None else dict(self.confirmation),
        }


# ── Internal survivor after hard eliminate ──────────────────────────────────


@dataclass
class _Survivor:
    route_id: str
    lookup: MetadataLookup
    record: ModelMetadataRecord
    health: RouteHealth
    evidence: RouteEvidence
    family_key: str
    provider: str
    score: float = 0.0
    features: dict[str, float] = field(default_factory=dict)
    confidence: float = 0.0
    qualified: bool = True
    needs_probe: bool = False
    aliases: tuple[str, ...] = ()


# ── Public API ──────────────────────────────────────────────────────────────


def fingerprint_task_profile(profile: TaskProfile) -> TaskFingerprint:
    """Adapt the canonical pre-admission TaskProfile without reinterpreting text."""

    return TaskFingerprint(
        digest=profile.digest,
        task_family=profile.task_family,
        required_capabilities=profile.required_capabilities,
        context_size=profile.min_context,
        risk=profile.task_class_hint or "unknown",
        objective_preview=profile.objective_preview,
    )


def build_candidate_pool(
    advertised: Sequence[str],
    *,
    task: str | None = None,
    task_fingerprint: TaskFingerprint | None = None,
    task_profile: TaskProfile | None = None,
    context: Mapping[str, Any] | None = None,
    metadata: MetadataSnapshot | None,
    identity_map: IdentityMap | None = None,
    health: Mapping[str, RouteHealth] | None = None,
    evidence: Mapping[str, RouteEvidence] | None = None,
    policy_allowed: Collection[str] | None = None,
    outcomes: Sequence[OutcomeObservation] | None = None,
    top_k: int = 8,
    probe_budget: ProbeBudget | None = None,
    probe_fn: ProbeFn | None = None,
    require_min_qualification: bool = False,
) -> CandidatePoolReceipt:
    """Build a bounded Top-K shortlist from an advertised universe.

    Downstream ranking may reorder shortlisted routes but must never restore a
    route present in ``hard_drops``.
    """
    if top_k < 1:
        raise CandidatePoolError("top_k must be >= 1")
    if task_profile is not None and task_fingerprint is not None:
        raise CandidatePoolError("task_profile and task_fingerprint are mutually exclusive")
    if task_profile is not None:
        task_fingerprint = fingerprint_task_profile(task_profile)
    elif task_fingerprint is None:
        if task is None:
            raise CandidatePoolError("task, task_fingerprint, or task_profile is required")
        task_fingerprint = fingerprint_task(task, context=context)
    requirements = TaskRequirements(
        names=task_fingerprint.required_capabilities,
        min_context=task_fingerprint.context_size
        if task_fingerprint.context_size is not None
        else None,
    )
    # When capabilities are required, Core metadata is mandatory (fail closed).
    if (requirements.names or requirements.min_context is not None) and metadata is None:
        raise CandidatePoolError("Core metadata store is required when capabilities are mandated")

    discovered = tuple(dict.fromkeys(str(item).strip() for item in advertised if str(item).strip()))
    health_map = dict(health or {})
    evidence_map = dict(evidence or {})
    policy_allowed_ids = None if policy_allowed is None else set(policy_allowed)
    outcome_list = tuple(
        sorted(
            outcomes or (),
            key=lambda item: json.dumps(item.to_dict(), sort_keys=True, separators=(",", ":")),
        )
    )
    budget = probe_budget or ProbeBudget()

    hard_drops: list[HardDrop] = []
    survivors: list[_Survivor] = []
    uncertainty: list[str] = []
    # Track metadata-id → advertised aliases for dedupe.
    alias_groups: dict[str, list[str]] = {}

    for route_id in discovered:
        if policy_allowed_ids is not None and route_id not in policy_allowed_ids:
            hard_drops.append(
                HardDrop(route_id, DROP_POLICY, "excluded by upstream spend or eligibility policy")
            )
            continue
        if is_opaque_route_id(route_id):
            hard_drops.append(
                HardDrop(route_id, DROP_OPAQUE_AUTO, "resolver alias is not concrete")
            )
            continue

        route_health = health_map.get(route_id, RouteHealth())
        if route_health.state == HEALTH_FAILED:
            hard_drops.append(
                HardDrop(route_id, DROP_HARD_HEALTH, route_health.detail or "health_failed")
            )
            continue

        if metadata is None:
            # No requirements path already returned; treat as unmapped for safety.
            hard_drops.append(HardDrop(route_id, DROP_UNMAPPED, "no metadata store"))
            continue

        try:
            lookup = lookup_omniroute_id(
                metadata, route_id, required=requirements.names, identity_map=identity_map
            )
        except ModelMetadataError as exc:
            hard_drops.append(HardDrop(route_id, DROP_REQUIRED_UNKNOWN, str(exc)))
            uncertainty.append(f"{route_id}: lookup_error")
            continue

        drop = _hard_drop_from_lookup(route_id, lookup, requirements)
        if drop is not None:
            hard_drops.append(drop)
            if drop.reason in {DROP_REQUIRED_UNKNOWN, DROP_UNMAPPED}:
                uncertainty.append(f"{route_id}:{drop.reason}")
            continue

        record = lookup.record
        assert record is not None  # hard drop would have fired
        meta_id = record.id
        alias_groups.setdefault(meta_id, []).append(route_id)
        ev = evidence_map.get(route_id, RouteEvidence())
        if ev.conflicts:
            uncertainty.append(f"{route_id}:evidence_conflict:{','.join(ev.conflicts)}")
        survivors.append(
            _Survivor(
                route_id=route_id,
                lookup=lookup,
                record=record,
                health=route_health,
                evidence=ev,
                family_key=_family_key(meta_id, route_id),
                provider=_provider(route_id, record),
                aliases=tuple(record.omniroute_ids),
            )
        )

    # Fuse evidence + score only among hard-eligible survivors.
    for survivor in survivors:
        features = _score_features(
            survivor, task_fingerprint=task_fingerprint, outcomes=outcome_list
        )
        survivor.features = features
        survivor.score = _fuse_score(features)
        survivor.confidence = _confidence(features, survivor)
        survivor.needs_probe = _needs_probe(survivor, require_min_qualification)

    # Build the diverse bounded scope *before* any active probe. A probe may
    # shrink this scope, but it may never expand into a catalog-wide scan.
    probe_scope_ids: set[str] | None = None
    probes: list[ProbeRecord] = []
    if probe_fn is not None and budget.max_probes > 0:
        pre_probe = _portfolio_shortlist(survivors, alias_groups=alias_groups, top_k=top_k)
        probe_scope_ids = {entry.route_id for entry in pre_probe}
        probe_scope = [item for item in survivors if item.route_id in probe_scope_ids]
        probes.extend(
            _run_probe_ladder(
                probe_scope,
                budget=budget,
                probe_fn=probe_fn,
                require_min_qualification=require_min_qualification,
            )
        )

    # A probed pool is closed over its pre-probe Top-K. Failed qualification
    # shrinks the pool; candidates outside it are never silently backfilled.
    eligible = [
        s
        for s in survivors
        if s.qualified and (probe_scope_ids is None or s.route_id in probe_scope_ids)
    ]
    if require_min_qualification:
        eligible = [s for s in eligible if not s.needs_probe or _probe_passed(s, probes)]

    # Alias/family dedupe then provider diversity → Top-K.
    shortlist = _portfolio_shortlist(eligible, alias_groups=alias_groups, top_k=top_k)

    evidence_digest = _digest(
        {
            "survivors": [
                {
                    "route_id": s.route_id,
                    "features": s.features,
                    "evidence": s.evidence.to_dict(),
                    "health": s.health.to_dict(),
                }
                for s in sorted(survivors, key=lambda x: x.route_id)
            ],
            "outcomes": [o.to_dict() for o in outcome_list],
        }
    )
    shortlist_digest = _digest([e.to_dict() for e in shortlist])

    return CandidatePoolReceipt(
        task_fingerprint=task_fingerprint,
        discovered_count=len(discovered),
        hard_drops=tuple(hard_drops),
        probes=tuple(probes),
        shortlist=tuple(shortlist),
        uncertainty=tuple(uncertainty),
        evidence_digest=evidence_digest,
        shortlist_digest=shortlist_digest,
        requirements=requirements.names
        + (() if requirements.min_context is None else (f"context>={requirements.min_context}",)),
        eligible_count=len(eligible),
    )


# ── Hard eliminate ──────────────────────────────────────────────────────────


def _hard_drop_from_lookup(
    route_id: str, lookup: MetadataLookup, requirements: TaskRequirements
) -> HardDrop | None:
    if lookup.drop is not None:
        reason = lookup.drop.reason
        mapped = {
            "unmapped": DROP_UNMAPPED,
            DROP_MAP_TARGET_MISSING: DROP_UNMAPPED,
            "required_unknown": DROP_REQUIRED_UNKNOWN,
            DROP_STALE: DROP_STALE_METADATA,
        }.get(reason, DROP_UNMAPPED)
        detail = lookup.drop.detail
        if lookup.drop.missing_fields:
            detail = ",".join(lookup.drop.missing_fields)
        return HardDrop(route_id, mapped, detail)

    record = lookup.record
    if record is None:
        return HardDrop(route_id, DROP_UNMAPPED, "no metadata record")

    if not requirements.names and requirements.min_context is None:
        return None

    missing: list[str] = []
    mismatched: list[str] = []
    for name in requirements.names:
        canonical = resolve_required_name(name)
        field = record.caps.field(canonical)
        if field is None:
            missing.append(canonical)
            continue
        if canonical in _BOOL_CAPS:
            observed = _field_true(field)
            if observed is None:
                missing.append(canonical)
            elif observed is False:
                mismatched.append(canonical)
    if requirements.min_context is not None:
        context_field = record.caps.field(_CONTEXT_CAP)
        if context_field is None:
            missing.append(_CONTEXT_CAP)
        else:
            try:
                window = int(context_field.value)
            except (TypeError, ValueError):
                missing.append(_CONTEXT_CAP)
            else:
                if window < requirements.min_context:
                    mismatched.append(_CONTEXT_CAP)
    if missing:
        return HardDrop(route_id, DROP_REQUIRED_UNKNOWN, ",".join(missing))
    if mismatched:
        return HardDrop(route_id, DROP_CAPABILITY_MISMATCH, ",".join(mismatched))
    return None


def _field_true(field: ProvenancedField | None) -> bool | None:
    if field is None:
        return None
    value = field.value
    if isinstance(value, bool):
        return value
    return None


# ── Evidence fuse ───────────────────────────────────────────────────────────


def _score_features(
    survivor: _Survivor,
    *,
    task_fingerprint: TaskFingerprint,
    outcomes: Sequence[OutcomeObservation],
) -> dict[str, float]:
    features: dict[str, float] = {}
    ev = survivor.evidence

    # Matching-task outcome history (eligible only — never overrides hard health).
    matched = [
        o
        for o in outcomes
        if o.route_id == survivor.route_id
        and (
            o.task_family == task_fingerprint.task_family
            or (
                o.fingerprint_digest is not None and o.fingerprint_digest == task_fingerprint.digest
            )
        )
    ]
    if matched:
        successes = sum(1 for o in matched if o.success)
        features["task_similarity"] = successes / len(matched)
        features["outcome_n"] = float(len(matched))
    elif ev.task_success_rate is not None:
        features["task_similarity"] = max(0.0, min(1.0, float(ev.task_success_rate)))

    # Soft eval score from caller or metadata (nullable — never invented).
    eval_score = ev.eval_score
    if eval_score is None:
        soft = survivor.record.scores.aa_coding or survivor.record.scores.aa_intelligence
        if soft is not None and isinstance(soft.value, (int, float)):
            eval_score = float(soft.value)
    if eval_score is not None:
        features["eval_score"] = max(0.0, min(100.0, float(eval_score))) / 100.0

    # Health freshness boost.
    if survivor.health.state == HEALTH_OK:
        features["health"] = 1.0 if survivor.health.fresh else 0.6
    elif survivor.health.state == HEALTH_UNKNOWN:
        features["health"] = 0.3
    else:
        features["health"] = 0.0

    # Latency is a small tie-breaker, never the main quality signal.
    latency = ev.latency_ms
    if latency is not None and latency >= 0:
        features["latency_fit"] = 1.0 / (1.0 + float(latency) / 1000.0)

    # Cost pressure — lower is better when known.
    cost = ev.cost_per_million
    if cost is None:
        cost_field = survivor.record.caps.input_cost_per_million
        if cost_field is not None and isinstance(cost_field.value, (int, float)):
            cost = float(cost_field.value)
    if cost is not None and cost >= 0:
        features["cost_fit"] = 1.0 / (1.0 + float(cost))

    # Uncertainty penalty — missing soft evidence or conflicts.
    uncertainty = 0.0
    if "task_similarity" not in features and "eval_score" not in features:
        uncertainty += 0.35
    if ev.conflicts:
        uncertainty += 0.25 * len(ev.conflicts)
    if survivor.health.state == HEALTH_UNKNOWN:
        uncertainty += 0.15
    features["uncertainty_penalty"] = min(1.0, uncertainty)

    # Context headroom when required.
    if task_fingerprint.context_size is not None:
        ctx = survivor.record.caps.context
        if ctx is not None and isinstance(ctx.value, (int, float)):
            ratio = float(ctx.value) / max(1, task_fingerprint.context_size)
            features["context_fit"] = max(0.0, min(1.0, ratio / 2.0))

    return features


def _fuse_score(features: Mapping[str, float]) -> float:
    """Inspectable linear fuse — transparent and replayable."""
    score = 0.0
    score += 0.40 * features.get("task_similarity", 0.0)
    score += 0.25 * features.get("eval_score", 0.0)
    score += 0.15 * features.get("health", 0.0)
    score += 0.05 * features.get("cost_fit", 0.0)
    score += 0.05 * features.get("latency_fit", 0.0)
    score += 0.10 * features.get("context_fit", 0.0)
    score -= 0.30 * features.get("uncertainty_penalty", 0.0)
    return round(score, 6)


def _confidence(features: Mapping[str, float], survivor: _Survivor) -> float:
    base = 0.4
    if "task_similarity" in features:
        base += 0.25
    if "eval_score" in features:
        base += 0.15
    if survivor.health.state == HEALTH_OK and survivor.health.fresh:
        base += 0.15
    base -= features.get("uncertainty_penalty", 0.0) * 0.4
    return round(max(0.05, min(0.99, base)), 4)


def _needs_probe(survivor: _Survivor, require_min_qualification: bool) -> bool:
    if require_min_qualification:
        return True
    # Insufficient soft evidence on an otherwise promising survivor.
    has_outcome = "task_similarity" in survivor.features
    has_eval = "eval_score" in survivor.features
    return not (has_outcome or has_eval)


# ── Probe ladder ────────────────────────────────────────────────────────────


def _max_level_index(level: str) -> int:
    return _PROBE_LADDER.index(level)


def _run_probe_ladder(
    survivors: list[_Survivor],
    *,
    budget: ProbeBudget,
    probe_fn: ProbeFn,
    require_min_qualification: bool,
) -> list[ProbeRecord]:
    """Escalate cheapest-first only for uncertain/high-value survivors.

    Hard-excluded routes are never in ``survivors``, so they are never probed.
    """
    records: list[ProbeRecord] = []
    remaining = budget.max_probes
    max_idx = _max_level_index(budget.max_level)
    if budget.allow_larger_eval:
        max_idx = _max_level_index(PROBE_LARGER_EVAL)

    # Prefer probing high-score uncertain candidates first (info value).
    candidates = sorted(
        [s for s in survivors if s.needs_probe], key=lambda s: s.score, reverse=True
    )
    for survivor in candidates:
        if remaining <= 0:
            break
        # Start at static metadata (already known) → escalate only if needed.
        start_level = PROBE_STATIC_METADATA
        if survivor.health.state == HEALTH_OK and survivor.health.fresh:
            # Skip redundant health when fresh cache says ok; jump to small qual
            # when we still lack task-relevant evidence.
            start_level = (
                PROBE_SMALL_QUAL
                if require_min_qualification or survivor.needs_probe
                else PROBE_FRESH_CACHE
            )
        start_idx = min(_max_level_index(start_level), max_idx)
        passed = True
        last_level = start_level
        for level in _PROBE_LADDER[start_idx : max_idx + 1]:
            if remaining <= 0:
                break
            if level == PROBE_LARGER_EVAL and not budget.allow_larger_eval:
                break
            # static_metadata / fresh_cache are local — still count as ladder steps
            # when a probe_fn is supplied so receipts show escalation path.
            result = dict(probe_fn(survivor.route_id, level))
            remaining -= 1
            ok = bool(result.get("ok", False))
            qualified = result.get("qualified")
            if qualified is None:
                qualified = ok
            passed = bool(qualified) and ok
            detail = result.get("detail")
            records.append(
                ProbeRecord(
                    route_id=survivor.route_id,
                    level=level,
                    passed=passed,
                    detail=str(detail) if detail is not None else None,
                )
            )
            last_level = level
            if not passed:
                survivor.qualified = False
                break
            # One successful task-relevant qual is enough for min qualification.
            if level in {PROBE_SMALL_QUAL, PROBE_LARGER_EVAL} and passed:
                survivor.needs_probe = False
                survivor.features["probe_bonus"] = 0.1
                survivor.score = _fuse_score(survivor.features)
                break
            # Cheap health failure is hard for this pass.
            if level == PROBE_CHEAP_HEALTH and not passed:
                survivor.qualified = False
                break
        else:
            # Exhausted ladder without small_qual pass.
            if (
                require_min_qualification
                and survivor.needs_probe
                and last_level not in {PROBE_SMALL_QUAL, PROBE_LARGER_EVAL}
            ):
                survivor.qualified = False
    return records


def _probe_passed(survivor: _Survivor, probes: Sequence[ProbeRecord]) -> bool:
    relevant = [p for p in probes if p.route_id == survivor.route_id]
    if not relevant:
        return False
    return any(
        p.passed and p.level in {PROBE_SMALL_QUAL, PROBE_LARGER_EVAL, PROBE_CHEAP_HEALTH}
        for p in relevant
    )


# ── Portfolio shortlist ─────────────────────────────────────────────────────


def _portfolio_shortlist(
    eligible: Sequence[_Survivor], *, alias_groups: Mapping[str, Sequence[str]], top_k: int
) -> list[ShortlistEntry]:
    # Collapse aliases that share the same metadata identity — keep best score.
    by_meta: dict[str, _Survivor] = {}
    for survivor in eligible:
        meta_id = survivor.record.id
        current = by_meta.get(meta_id)
        if current is None or survivor.score > current.score:
            by_meta[meta_id] = survivor

    # ``by_meta`` removes only proven aliases that resolve to the same Core
    # identity. Do not collapse same-leaf IDs across providers: provider
    # diversity must remain available to the portfolio pass.
    ranked = sorted(by_meta.values(), key=lambda s: (-s.score, s.route_id))

    selected: list[_Survivor] = []
    seen_providers: set[str] = set()
    # First pass: pick best overall, then prefer unseen providers for diversity.
    for survivor in ranked:
        if len(selected) >= top_k:
            break
        selected.append(survivor)
        seen_providers.add(survivor.provider)

    # If we still have room, nothing more to add. If top_k filled with one
    # provider, try to swap the lowest for a distinct-provider fallback.
    if len(selected) >= top_k and len(seen_providers) == 1 and len(ranked) > 1:
        primary_provider = selected[0].provider
        fallback = next((s for s in ranked if s.provider != primary_provider), None)
        if fallback is not None and fallback not in selected:
            # Replace the lowest-scoring duplicate-provider slot.
            selected[-1] = fallback

    # Ensure distinct-provider fallback is present when top_k >= 2.
    if top_k >= 2 and ranked:
        providers_in = {s.provider for s in selected}
        if len(providers_in) < min(2, len({s.provider for s in ranked})):
            for survivor in ranked:
                if survivor.provider not in providers_in:
                    if len(selected) < top_k:
                        selected.append(survivor)
                    else:
                        # Replace last same-provider entry if possible.
                        for idx in range(len(selected) - 1, 0, -1):
                            if (
                                selected[idx].provider in providers_in
                                and selected[idx] is not survivor
                            ):
                                selected[idx] = survivor
                                break
                    break
        # Re-sort after possible swap.
        selected = sorted(selected, key=lambda s: (-s.score, s.route_id))[:top_k]

    entries: list[ShortlistEntry] = []
    for survivor in selected:
        meta_id = survivor.record.id
        aliases = tuple(alias_groups.get(meta_id, (survivor.route_id,)))
        reason = "evidence_fused_topk"
        if entries and survivor.provider not in {e.provider for e in entries}:
            reason = "provider_diversity_fallback"
        entries.append(
            ShortlistEntry(
                route_id=survivor.route_id,
                metadata_id=meta_id,
                provider=survivor.provider,
                family_key=survivor.family_key,
                score=survivor.score,
                confidence=survivor.confidence,
                score_features=dict(survivor.features),
                inclusion_reason=reason,
                aliases=aliases,
            )
        )
    return entries


# ── Helpers ─────────────────────────────────────────────────────────────────


def _digest(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return f"sha256:{sha256(raw.encode('utf-8')).hexdigest()}"


def _optional_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip().lower()
    return None


def _infer_family(task: str, language: str | None) -> str:
    lowered = task.lower()
    if any(tok in lowered for tok in ("refactor", "edit", "implement", "fix", "bug")):
        base = "code_edit"
    elif any(tok in lowered for tok in ("review", "audit")):
        base = "code_review"
    elif any(tok in lowered for tok in ("plan", "design", "architect")):
        base = "planning"
    elif any(tok in lowered for tok in ("summar", "explain", "docs")):
        base = "docs"
    else:
        base = "general"
    if language:
        return f"{base}:{language}"
    return base


def _infer_reasoning(task: str) -> str:
    lowered = task.lower()
    if any(tok in lowered for tok in ("architect", "prove", "complex", "multi-step")):
        return "deep"
    if any(tok in lowered for tok in ("refactor", "implement", "debug")):
        return "moderate"
    if any(tok in lowered for tok in ("list", "rename", "format")):
        return "shallow"
    return "unknown"


def _family_key(metadata_id: str, route_id: str) -> str:
    """Collapse aliases/variants that share an effective leaf execution risk."""
    leaf = model_id_leaf(metadata_id)
    # Strip common tier suffixes so :free / :nitro variants share a family.
    leaf = leaf.split(":", 1)[0]
    # Normalize obvious duplicate naming (meta-llama/llama-… vs llama-…).
    leaf = re.sub(r"^(meta-)?llama", "llama", leaf)
    return leaf.lower()


def _provider(route_id: str, record: ModelMetadataRecord) -> str:
    if "/" in route_id:
        return route_id.split("/", 1)[0].lower()
    if record.provider:
        return record.provider.lower()
    return "unknown"


__all__ = [
    "DROP_CAPABILITY_MISMATCH",
    "DROP_HARD_HEALTH",
    "DROP_OPAQUE_AUTO",
    "DROP_POLICY",
    "DROP_REQUIRED_UNKNOWN",
    "DROP_STALE_METADATA",
    "DROP_UNMAPPED",
    "HEALTH_FAILED",
    "HEALTH_OK",
    "HEALTH_UNKNOWN",
    "PROBE_CHEAP_HEALTH",
    "PROBE_FRESH_CACHE",
    "PROBE_LARGER_EVAL",
    "PROBE_SMALL_QUAL",
    "PROBE_STATIC_METADATA",
    "CandidatePoolError",
    "CandidatePoolReceipt",
    "HardDrop",
    "OutcomeObservation",
    "ProbeBudget",
    "ProbeRecord",
    "RouteEvidence",
    "RouteHealth",
    "ShortlistEntry",
    "TaskFingerprint",
    "build_candidate_pool",
    "fingerprint_task",
    "fingerprint_task_profile",
]
