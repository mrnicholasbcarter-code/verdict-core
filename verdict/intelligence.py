import hashlib
import json
import os
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from verdict.admit_prove_confirm import gate_admit_prove_confirm
from verdict.candidate_pool import (
    HEALTH_OK,
    CandidatePoolError,
    CandidatePoolReceipt,
    RouteEvidence,
    RouteHealth,
    build_candidate_pool,
)
from verdict.capability_gate import derive_requirements, gate_capability
from verdict.chooser import ChooserError, apply_best_of_admitted
from verdict.classifier import classify
from verdict.context_pack import ContextPlan
from verdict.cost_ledger import CostTerm
from verdict.discovery import fetch_models
from verdict.effective_capability import AssistanceCost, AssistancePlan
from verdict.eligibility import EligibilityGate
from verdict.escalation import scan
from verdict.expected_cost import ExpectedStrategyCost
from verdict.free_tier_admit import (
    DEFAULT_CHEAP_PATH_TOKEN_BUDGET,
    FAIL_CLOSED_REASON,
    NO_ELIGIBLE_TARGET,
    FreeTierAdmitReceipt,
    LiveAdmitError,
    NamedDrop,
    OmniRouteAdmitSnapshot,
    admit_free_tier_active,
    build_cheap_path_context_pack,
    execute_offload_chat,
    expand_admit_for_worthiness,
    load_omniroute_admit_snapshot,
    normalize_omniroute_origin,
    omniroute_endpoint_from_env,
)
from verdict.logger import log_decision
from verdict.metadata.mapping import IdentityMap, load_identity_map
from verdict.metadata.records import ModelMetadataError
from verdict.metadata.store import (
    MetadataSnapshot,
    default_store_path,
    load_store,
    lookup_omniroute_id,
)
from verdict.model_passports import ModelPassport
from verdict.models import ModelInfo, ProviderConfig, RoutingDecision
from verdict.planner import StructuredPlanner
from verdict.probes import ProbeTransport, openai_probe_transport
from verdict.router import select_best_eligible_model, select_best_model
from verdict.task_profile import TaskProfile, TaskProfileError, profile_task
from verdict.worthiness import classify_worthiness


def _bind_context_plan(assistance_plan: AssistancePlan, plan: ContextPlan) -> AssistancePlan:
    """Attach a plan and bind the mutated assistance payload to a fresh digest."""
    updated = replace(
        assistance_plan,
        context_plan_requirements=plan.to_dict(),
        assistance_cost=replace(
            assistance_plan.assistance_cost,
            context_tokens=plan.estimated_input_tokens or plan.input_token_budget,
            tool_tokens=max(assistance_plan.assistance_cost.tool_tokens, plan.tool_token_reserve),
        ),
    )
    payload = updated.to_dict()
    payload.pop("plan_id", None)
    payload.pop("evidence_digest", None)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    digest = f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"
    return replace(updated, plan_id=f"ecp:{digest[7:23]}", evidence_digest=digest)


def _cost_with_context_plan(
    cost: ExpectedStrategyCost, *, new_assistance: AssistanceCost, plan: ContextPlan
) -> ExpectedStrategyCost:
    """Rebind hydration/tool/output resource estimates without inventing a price."""
    targets = {"hydration": new_assistance.context_tokens, "tools": new_assistance.tool_tokens}
    execution_tokens = next(
        (
            int(term.amount)
            for term in cost.terms
            if term.kind == "execution" and term.unit == "tokens" and term.amount is not None
        ),
        0,
    )
    if execution_tokens < plan.output_token_reserve:
        targets["execution"] = plan.output_token_reserve

    rates: dict[str, tuple[Decimal, CostTerm]] = {}
    for token_term in cost.terms:
        if token_term.unit != "tokens" or token_term.amount in (None, Decimal("0")):
            continue
        usd_term = next(
            (
                item
                for item in cost.terms
                if item.kind == token_term.kind
                and item.unit == "usd"
                and item.amount is not None
                and item.status != "unknown"
            ),
            None,
        )
        if usd_term is not None:
            usd_amount = usd_term.amount
            token_amount = token_term.amount
            assert usd_amount is not None and token_amount is not None
            rates[token_term.kind] = (usd_amount / token_amount, usd_term)

    revised = [
        term
        for term in cost.terms
        if term.kind not in targets
        or (
            term.unit != "tokens"
            and not (
                term.unit == "usd"
                and term.kind in rates
                and term.status != "unknown"
                and term.amount is not None
            )
        )
    ]
    for kind, tokens in targets.items():
        if tokens <= 0:
            continue
        revised.append(
            CostTerm(kind=kind, amount=Decimal(tokens), unit="tokens", status="estimated")
        )
        rate_and_template = rates.get(kind)
        if rate_and_template is not None:
            rate, price_template = rate_and_template
            revised.append(
                replace(price_template, amount=rate * Decimal(tokens), status="estimated")
            )
    return ExpectedStrategyCost.build(
        strategy_id=cost.strategy_id,
        trajectory_id=cost.trajectory_id,
        terms=tuple(revised),
        policy_mode=cost.policy_mode,
        free_first_preferred=cost.free_first_preferred,
        qualified=cost.qualified,
        is_free=cost.is_free,
        notes=cost.notes,
    )


DEFAULT_PROFILE = "development"
DEGRADED_PROFILE = "degraded"
DEFAULT_TIMEOUT_MS = 1000
DEFAULT_CANDIDATE_TOP_K = 4
REASON_CANDIDATE_POOL_NOT_SHORTLISTED = "candidate_pool_not_shortlisted"
TASK_INSTRUCTIONS_OMITTED_REASON = (
    "denied — task instructions exceed the cheap-path context budget and were not packed"
)


@dataclass
class ReadinessReport:
    status: str
    production_ready: bool
    profile: str
    managed_backend_status: str
    degraded_mode: bool
    policy_version: str
    reason: str
    adapter_versions: dict[str, str]


@dataclass
class RankedCandidate:
    """Advisory ranking row retained for embedders using ``rank()``."""

    model_id: str
    score: float
    reasoning: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class IntelligenceRanking:
    """Advisory ranking result; it cannot bypass EligibilityGate."""

    ranked: list[RankedCandidate]
    task_spec_id: str
    profile: str


class IntelligenceService:
    def __init__(
        self,
        primary_model: str,
        providers: dict[str, ProviderConfig],
        profile: str,
        log_path: str,
        log_full_task: bool,
        discovery_ttl: int,
        timeout_ms: int = 1000,
        frontier_allowlist: tuple[str, ...] | None = None,
        allow_client_model_override: bool = False,
        planner: StructuredPlanner | None = None,
        eligibility_gate: EligibilityGate | None = None,
        allow_offline: bool = False,
        admit_snapshot: OmniRouteAdmitSnapshot | None = None,
        offload_executor: Any | None = None,
        execute_offload: bool | None = None,
        passports: dict[str, ModelPassport] | None = None,
        confirm_transport: ProbeTransport | None = None,
        passport_store_path: Path | None = None,
        admit_now: datetime | None = None,
        workspace_root: Path | str | None = None,
        context_roots: Sequence[str] | None = None,
        mcp_root: Path | str | None = None,
        metadata_snapshot: MetadataSnapshot | None = None,
        metadata_store_path: Path | str | None = None,
        identity_map: IdentityMap | None = None,
        require_execution_path_authority: bool | None = None,
        candidate_top_k: int = DEFAULT_CANDIDATE_TOP_K,
        receipt_store: Any | None = None,
        persist_routing_receipts: bool = True,
        decision_signal_provider: Any | None = None,
    ):
        self.primary_model = primary_model
        self.providers = providers
        self.profile = profile
        self.log_path = log_path
        self.log_full_task = log_full_task
        self.discovery_ttl = discovery_ttl
        self.timeout_ms = timeout_ms
        self.frontier_allowlist = frontier_allowlist
        self.allow_client_model_override = allow_client_model_override
        self.planner = planner or StructuredPlanner()
        # Issue #57: single-source-of-truth eligibility gate consulted before
        # any ranking.  When None, routing falls back to catalog truth only.
        self.eligibility_gate = eligibility_gate
        # Issue #265 (V1-002): allow_offline=True keeps every decision readable
        # without network or subprocess probes — static catalog truth only.
        self.allow_offline = allow_offline
        self.admit_snapshot = admit_snapshot
        self.offload_executor = offload_executor
        # None = execute only for a live (non-fixture) snapshot.
        self.execute_offload = execute_offload
        # Serve admit gate: fresh prove-at-rest passports ∩ budgeted confirm.
        # None passports → load from prove-at-rest store at request time.
        self.passports = passports
        self.confirm_transport = confirm_transport
        self.passport_store_path = passport_store_path
        self.admit_now = admit_now
        self.workspace_root = workspace_root
        self.context_roots = tuple(context_roots) if context_roots is not None else None
        self.mcp_root = mcp_root
        self.metadata_snapshot = metadata_snapshot
        self.metadata_store_path = metadata_store_path
        self.identity_map = identity_map
        # BOD-127: production/default serve fails closed without BOD-104.
        # None = derive from profile / VERDICT_REQUIRE_EXECUTION_PATH / context.
        self.require_execution_path_authority = require_execution_path_authority
        if candidate_top_k < 1:
            raise ValueError("candidate_top_k must be >= 1")
        self.candidate_top_k = candidate_top_k
        self.receipt_store = receipt_store
        self.persist_routing_receipts = persist_routing_receipts
        # Optional decision signal provider for ADVISORY mode (BOD-238).
        # None = no advisory; set to a DecisionSignalProvider-compatible object
        # to collect signals and apply advisory reordering in ADVISORY mode.
        self.decision_signal_provider = decision_signal_provider
        # Request-time OmniRoute evidence cache. A failed refresh never erases
        # the last complete snapshot; cold-start authority mode still fails closed.
        self._admit_snapshot_lkg: OmniRouteAdmitSnapshot | None = None
        self._admit_snapshot_endpoint: tuple[str, str | None] | None = None
        self._admit_snapshot_loaded_at: float = 0.0
        self._admit_snapshot_refresh_error: str | None = None
        # Cheap path does not require Ruflo/RuVector. Those remain optional
        # swarm/workflow adapters and must not mark routing degraded.
        self.managed_backend_status = "offline" if allow_offline else "not_used"
        self._policy_version = "policy-2026-07-13.1"

    async def rank(self, eligible: list[ModelInfo], task_spec: Any) -> IntelligenceRanking:
        """Return a deterministic advisory ordering for already-eligible rows."""
        ranked = [
            RankedCandidate(
                model_id=model.id,
                score=1.0 - index * 0.1,
                reasoning=f"Intelligence ranked #{index + 1} for task",
            )
            for index, model in enumerate(eligible)
        ]
        return IntelligenceRanking(
            ranked=ranked,
            task_spec_id=str(getattr(task_spec, "prompt", ""))[:50],
            profile=self.profile,
        )

    def static_catalog(self) -> list[ModelInfo]:
        """Build routing candidates from configured provider models only.

        This is the allow_offline decision surface (issue #265): every row is
        derived from static configuration, never from network discovery, so
        the result is deterministic and readable without connectivity.
        """
        candidates: list[ModelInfo] = []
        for provider_name, cfg in self.providers.items():
            for model_id, model_cfg in cfg.models.items():
                candidates.append(
                    ModelInfo(
                        id=model_id,
                        provider=provider_name,
                        capability_tier=classify(model_id),
                        capabilities=frozenset(model_cfg.capabilities),
                        max_tokens=model_cfg.max_tokens,
                        cost_per_1k=model_cfg.cost_per_1k,
                        pricing=dict(model_cfg.pricing),
                        is_available=True,
                        availability_state="eligible",
                        source="static_catalog",
                    )
                )
        return candidates

    def readiness(self) -> ReadinessReport:
        return ReadinessReport(
            status="ready",
            production_ready=True,
            profile=self.profile,
            managed_backend_status=self.managed_backend_status,
            degraded_mode=False,
            policy_version=self._policy_version,
            reason="ready",
            adapter_versions={},
        )

    async def route(
        self,
        task: str | dict[str, Any],
        criticality: str = "medium",
        context: dict[str, Any] | None = None,
        *,
        request_id: str | None = None,
    ) -> RoutingDecision:
        """Route ``task``. ``request_id`` (when the caller already owns one) is stamped
        on the decision *before* it is logged so post-execution outcome receipts
        (BOD-117) can join back to this row."""
        start_t = time.time()

        # Handle envelope input
        if isinstance(task, dict):
            # Extract fields from envelope
            task_str = task.get("task", "")
            # Allow overriding criticality and context from envelope
            if "criticality" in task:
                criticality = task["criticality"]
            if "context" in task:
                context = task["context"]
        else:
            task_str = task

        # BOD-104 / BOD-127: ExecutionPathDecision is sole strategy authority on
        # the serve path. Legacy free-tier/chooser/ranker paths are feeds or
        # explicit migration escapes only — never silent inventors.
        from verdict.execution_path import ExecutionPathError
        from verdict.serve_path import (
            LEGACY_NON_AUTHORITY_FLAG,
            SERVE_PATH_AUTHORITY_FLAG,
            require_serve_path_decision,
            resolve_execution_path_decision,
            selected_route_dispatch_identity,
            serve_path_authority_required,
        )

        if isinstance(context, dict) and "execution_path_request" in context:
            context = dict(context)
            context["execution_path_request"] = self._prepare_execution_path_request(
                task_str, criticality, context, context["execution_path_request"]
            )
        ep = resolve_execution_path_decision(context if isinstance(context, dict) else None)
        require_authority = serve_path_authority_required(
            profile=self.profile,
            context=context if isinstance(context, dict) else None,
            require_execution_path_authority=self.require_execution_path_authority,
        )
        if ep is not None:
            require_serve_path_decision(ep, surface="intelligence.route")
            identity = selected_route_dispatch_identity(ep)
            payload = ep.to_dict()
            elapsed = (time.time() - start_t) * 1000
            safety = [
                SERVE_PATH_AUTHORITY_FLAG,
                f"bod104_strategy:{payload.get('selected_strategy')}",
            ]
            if identity.get("gateway"):
                safety.append(f"bod104_gateway:{identity['gateway']}")
            if identity.get("route_id"):
                safety.append(f"bod104_route_id:{identity['route_id']}")
            dec = RoutingDecision(
                model=str(identity["model"]),
                provider=str(identity.get("provider") or "unknown"),
                tier=int(identity.get("capability_tier") or 2),
                reason=(f"bod104:{payload.get('selected_strategy')}:{payload.get('why_selected')}"),
                latency_ms=elapsed,
                logged=bool(self.log_path),
                request_id=request_id or "",
                decision="selected",
                safety_flags=safety,
            )
            if self.log_path:
                log_decision(self.log_path, task_str, 2, dec, self.log_full_task)
            return dec
        if require_authority:
            raise ExecutionPathError(
                "missing ExecutionPathDecision; strategy must come from "
                "execution_path.optimize_execution_path"
            )

        # Legacy feed path (explicit migration / non-production only).
        legacy_safety = [LEGACY_NON_AUTHORITY_FLAG]

        # Fallback to strict heuristic scan
        eff_tier, heuristic_reason = scan(task_str)

        # Planning estimates task capability needs. Criticality is retained as a
        # safety floor, not as a model selector: identical task semantics have
        # identical selection requirements unless a protected floor applies.
        try:
            task_spec = self.planner.plan(
                task_str, context=context, criticality=criticality
            ).task_spec
            task_tier = {"low": 3, "medium": 2, "high": 1}.get(task_spec.effort, 2)
        except Exception:
            task_tier = 2

        # Convert criticality string to required tier max
        tide_map = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        req_tier = tide_map.get(criticality.lower(), 2)

        esc_reason: str = ""
        escalated = False

        if eff_tier is not None and eff_tier < req_tier:
            req_tier = eff_tier
            esc_reason = heuristic_reason or ""
            escalated = True

        safety_floor = req_tier if req_tier <= 1 else 3
        final_tier = min(task_tier, safety_floor, eff_tier if eff_tier is not None else 3)

        if final_tier > 0 and not self.allow_offline:
            offload = self._offload_free_tier(
                task_str, final_tier, escalated, esc_reason, context=context
            )
            if offload is not None:
                elapsed = (time.time() - start_t) * 1000
                flags = list(offload.safety_flags or [])
                for flag in legacy_safety:
                    if flag not in flags:
                        flags.append(flag)
                dec = RoutingDecision(
                    **{
                        **offload.__dict__,
                        "latency_ms": elapsed,
                        "logged": bool(self.log_path),
                        "request_id": request_id or offload.request_id,
                        "safety_flags": flags,
                    }
                )
                if self.log_path:
                    log_decision(self.log_path, task_str, req_tier, dec, self.log_full_task)
                return dec

        if self.allow_offline:
            # Static catalog only: no /v1/models discovery, no probes.
            candidates = self.static_catalog()
        else:
            candidates = []
            for name, cfg in self.providers.items():
                candidates.extend(fetch_models(name, cfg, self.discovery_ttl))

        # Issue #57: filter candidates by live eligibility BEFORE any ranking.
        # The gate is the single source of truth shared with the explain
        # endpoint, so no downstream ranker can reintroduce an excluded model.
        eligibility = None
        if self.eligibility_gate is not None:
            eligibility = self.eligibility_gate.evaluate(
                candidates, protected=(final_tier == 0), dev_mode=(self.profile == "development")
            )
            candidates = eligibility.eligible

        # BOD-238 ADVISORY mode: collect signals, reorder the valid/admitted set,
        # then pick best_model from the advisory-ordered front.
        # Must not execute when task is protected (final_tier == 0), no provider,
        # ExecutionPathDecision present (already returned above), or mode != ADVISORY.
        _advisory_influence_flags: list[str] = []
        _advisory_signals: Any = None
        _advisory_privacy: str | None = None
        if final_tier != 0:
            try:
                import threading as _threading

                from verdict.decision_signals.advisory import _mode_from_env, advise_order
                from verdict.decision_signals.contracts import DecisionQuestionV1

                _adv_mode = _mode_from_env()
                _provider = self.decision_signal_provider
                if _adv_mode == "ADVISORY" and _provider is not None:
                    _task_privacy2: str | None = None
                    try:
                        _ts2 = self.planner.plan(task_str, context=context).task_spec
                        _task_privacy2 = getattr(_ts2, "privacy", None)
                    except Exception:
                        pass
                    _advisory_privacy = _task_privacy2
                    if _task_privacy2 not in ("restricted", "trusted_upstream"):
                        try:
                            from contextlib import suppress as _suppress
                            from datetime import datetime as _dt
                            from datetime import timezone as _tz

                            _timeout_ms = int(
                                os.environ.get("VERDICT_DECISION_SIGNALS_TIMEOUT_MS", "1500")
                            )
                            _question = DecisionQuestionV1(
                                purpose="route", task_summary=task_str[:500], complexity_hints={}
                            )
                            _result_holder: list[Any] = []
                            _bound_provider = _provider

                            def _fetch() -> None:
                                with _suppress(Exception):
                                    _result_holder.append(
                                        _bound_provider.signals(_question, now=_dt.now(_tz.utc))
                                    )

                            _t = _threading.Thread(target=_fetch, daemon=True)
                            _t.start()
                            _t.join(timeout=_timeout_ms / 1000.0)
                            if _result_holder:
                                _advisory_signals = _result_holder[0]
                            else:
                                _advisory_influence_flags.append("advisory:skipped:timeout")
                        except Exception:
                            _advisory_influence_flags.append("advisory:skipped:error")
            except Exception:
                _advisory_influence_flags.append("advisory:skipped:internal_error")

        best_model, _ = (
            select_best_eligible_model(eligibility, final_tier, self.providers)
            if eligibility is not None
            else select_best_model(candidates, final_tier, self.providers)
        )

        # Apply advisory reorder AFTER the baseline best_model is chosen.
        # Override best_model with the first advisory-ordered candidate from the
        # valid set (the set that actually passed select_best_model's tier/state
        # filter).  Membership is never changed; advisory only reorders.
        if _advisory_signals is not None and best_model is not None and final_tier != 0:
            try:
                from verdict.decision_signals.advisory import advise_order

                # Build the valid set in the same way select_best_model does.
                _raw_candidates = eligibility.eligible if eligibility is not None else candidates
                _valid = [
                    m
                    for m in _raw_candidates
                    if getattr(m, "capability_tier", 0) <= final_tier
                    and getattr(m, "is_available", True)
                    and getattr(m, "availability_state", "eligible") in {"eligible", "ready"}
                ]
                if not _valid:
                    _valid = _raw_candidates  # fallback: no filter match
                _ordered_valid, _influence = advise_order(
                    _valid, _advisory_signals, protected=False, privacy=_advisory_privacy
                )
                _prof = _influence.profile
                _b1 = _influence.baseline_first or ""
                if _prof not in ("inconclusive",) and not _prof.startswith("skipped"):
                    # Real reorder happened: pick first in advisory order
                    best_model = _ordered_valid[0] if _ordered_valid else best_model
                _advisory_influence_flags.append(f"advisory:{_prof}")
                if _b1:
                    _advisory_influence_flags.append(f"advisory_baseline:{_b1}")
            except Exception:
                _advisory_influence_flags.append("advisory:skipped:apply_error")

        eligibility_record: dict[str, Any] = {}
        if eligibility is not None:
            eligibility_record = eligibility.to_dict()
            excluded = [r for r in eligibility.records if not r.admitted]
            if excluded and final_tier == 0:
                # Protected work: some candidates were excluded by live truth.
                eligibility_record["protected_fail_closed"] = True

        if final_tier == 0 or not best_model:
            flags = list(legacy_safety)
            if eligibility_record.get("protected_fail_closed"):
                flags.append("eligibility_exclusions_applied")
            flags.extend(_advisory_influence_flags)
            dec = RoutingDecision(
                model=self.primary_model,
                provider="primary",
                tier=0,
                reason="critical — never offload"
                if final_tier == 0
                else "fallback — no offload match",
                escalated=escalated,
                escalation_reason=esc_reason or None,
                policy_version=self._policy_version,
                degraded_mode=False,
                managed_backend_status=self.managed_backend_status,
                protected=(final_tier == 0),
                decision="fallback" if best_model is None else "selected",
                transport_outcome="not_sent",
                quality_outcome="unknown",
                candidate_states=eligibility_record.get("records", []),
                safety_flags=flags,
            )
        else:
            flags = list(legacy_safety)
            if eligibility_record.get("protected_fail_closed"):
                flags.append("eligibility_exclusions_applied")
            flags.extend(_advisory_influence_flags)
            dec = RoutingDecision(
                model=best_model.id,
                provider=best_model.provider,
                tier=best_model.capability_tier,
                reason=f"tier {final_tier} routed",
                escalated=escalated,
                escalation_reason=esc_reason or None,
                policy_version=self._policy_version,
                degraded_mode=False,
                managed_backend_status=self.managed_backend_status,
                protected=(final_tier == 0),
                decision="selected",
                transport_outcome="not_sent",
                quality_outcome="unknown",
                candidate_states=eligibility_record.get("records", []),
                safety_flags=flags,
            )

        elapsed = (time.time() - start_t) * 1000
        dec = RoutingDecision(
            **{
                **dec.__dict__,
                "latency_ms": elapsed,
                "logged": bool(self.log_path),
                "request_id": request_id or dec.request_id,
            }
        )
        if self.log_path:
            log_decision(self.log_path, task_str, req_tier, dec, self.log_full_task)

        return dec

    def _load_metadata(self) -> tuple[MetadataSnapshot | None, IdentityMap | None]:
        snapshot = self.metadata_snapshot
        if snapshot is None:
            path = self.metadata_store_path or default_store_path()
            try:
                snapshot = load_store(path)
            except (OSError, ValueError, ModelMetadataError):
                snapshot = None
        mapping = self.identity_map
        if mapping is None:
            try:
                mapping = load_identity_map()
            except (OSError, ModelMetadataError):
                mapping = None
        return snapshot, mapping

    def _estimate_candidate_context_plans(
        self,
        task: str,
        *,
        profile: TaskProfile,
        candidates: Sequence[str],
        metadata: MetadataSnapshot | None,
        identity_map: IdentityMap | None,
        context: dict[str, Any] | None,
    ) -> tuple[tuple[ContextPlan, ...], frozenset[str]]:
        """Estimate candidate-capped plans before selection, without hydration."""
        contract = context if isinstance(context, dict) else {}
        criteria_count = sum(
            len(tuple(contract.get(name, ())))
            for name in ("acceptance_criteria", "proof_criteria")
            if isinstance(contract.get(name, ()), (list, tuple))
        )
        plans: list[ContextPlan] = []
        cannot_fit: set[str] = set()
        for candidate_id in candidates:
            context_window = DEFAULT_CHEAP_PATH_TOKEN_BUDGET
            if metadata is not None:
                lookup = lookup_omniroute_id(
                    metadata, candidate_id, identity_map=identity_map, now=self.admit_now
                )
                field = None if lookup.record is None else lookup.record.caps.context
                if field is not None and isinstance(field.value, (int, float)):
                    context_window = int(field.value)
            plan = ContextPlan.estimate_for_candidate(
                task=task,
                candidate_id=candidate_id,
                context_window=context_window,
                task_family=profile.task_family,
                required_capabilities=profile.required_capabilities,
                criteria_count=criteria_count,
                created_at=None if metadata is None else metadata.refreshed_at,
            )
            plans.append(plan)
            if not plan.estimated_fit:
                cannot_fit.add(candidate_id)
        return tuple(plans), frozenset(cannot_fit)

    def _apply_candidate_pool(
        self,
        receipt: FreeTierAdmitReceipt,
        *,
        profile: TaskProfile,
        metadata: MetadataSnapshot,
        identity_map: IdentityMap | None,
        candidate_universe: Sequence[str] | None = None,
        policy_allowed: Sequence[str] | None = None,
    ) -> tuple[FreeTierAdmitReceipt, CandidatePoolReceipt]:
        """Bound hard-admitted identities before any active confirmation."""

        passports = self.passports or {}
        advertised = tuple(candidate_universe or receipt.admitted)
        evidence: dict[str, RouteEvidence] = {}
        for model_id in advertised:
            passport = passports.get(model_id)
            if passport is None:
                continue
            cost = passport.token_cost_per_1k
            evidence[model_id] = RouteEvidence(
                latency_ms=passport.latency_p95,
                cost_per_million=None if cost is None else cost * 1000.0,
            )
        pool = build_candidate_pool(
            advertised,
            task_profile=profile,
            metadata=metadata,
            identity_map=identity_map,
            health={model_id: RouteHealth(state=HEALTH_OK) for model_id in advertised},
            evidence=evidence,
            policy_allowed=policy_allowed,
            top_k=self.candidate_top_k,
        )
        hard_admitted = set(receipt.admitted)
        shortlisted = tuple(
            item.route_id for item in pool.shortlist if item.route_id in hard_admitted
        )
        shortlisted_set = set(shortlisted)
        hard_drop_ids = {item.route_id for item in pool.hard_drops}
        existing_drops = {(item.model_id, item.reason) for item in receipt.exclusions}
        additions = [
            NamedDrop(item.route_id, item.reason, item.detail)
            for item in pool.hard_drops
            if (item.route_id, item.reason) not in existing_drops
        ]
        additions.extend(
            NamedDrop(
                model_id,
                REASON_CANDIDATE_POOL_NOT_SHORTLISTED,
                f"outside bounded Top-{self.candidate_top_k} candidate pool",
            )
            for model_id in receipt.admitted
            if model_id not in shortlisted_set and model_id not in hard_drop_ids
        )
        chosen = shortlisted[0] if shortlisted else None
        return (
            replace(
                receipt,
                admitted=shortlisted,
                exclusions=receipt.exclusions + tuple(additions),
                chosen=chosen,
                empty_intersection=chosen is None,
                free_admitted=tuple(
                    item for item in receipt.free_admitted if item in shortlisted_set
                ),
                paid_admitted=tuple(
                    item for item in receipt.paid_admitted if item in shortlisted_set
                ),
                candidate_pool=pool.to_dict(),
            ),
            pool,
        )

    def _prepare_execution_path_request(
        self, task: str, criticality: str, context: dict[str, Any], request: Any
    ) -> Any:
        """Qualify live offers, then hand the bounded set to BOD-104.

        Existing requests that already carry a pool receipt are immutable
        evidence and are not rebuilt. When no live snapshot is configured,
        compatibility callers retain their original request; production still
        fails closed later if BOD-104 cannot produce a valid decision.
        """
        from verdict.execution_path import ExecutionPathError, ExecutionPathRequest

        if not isinstance(request, ExecutionPathRequest) or request.pool_receipt is not None:
            return request
        snapshot, endpoint, live, _fetch_error = self._load_admit_snapshot()
        if snapshot is None:
            return request
        metadata, identity_map = self._load_metadata()
        if metadata is None:
            raise ExecutionPathError(
                "candidate pool requires Core metadata for a live execution-path request"
            )
        classification = classify_worthiness(task, criticality=criticality, context=context)
        planner_caps: tuple[str, ...] = ()
        try:
            planned = self.planner.plan(task, context=context).task_spec
            planner_caps = tuple(planned.required_capabilities)
        except Exception:
            planner_caps = ()
        requirements = derive_requirements(task, context, planner_capabilities=planner_caps)
        profile = profile_task(task, context=context, requirements=requirements)
        receipt = expand_admit_for_worthiness(
            admit_free_tier_active(snapshot),
            snapshot,
            task_class=classification.task_class,
            class_reasons=classification.class_reasons,
            frontier_allowlist=self.frontier_allowlist,
            spend_policy=profile.spend_policy,
            task_profile_digest=profile.digest,
        )
        policy_admitted = receipt.admitted
        receipt = gate_capability(
            receipt, requirements, snapshot=metadata, identity_map=identity_map, now=self.admit_now
        )
        offered_universe = tuple(dict.fromkeys(offer.route.route_id for offer in request.offers))
        offered_ids = set(offered_universe)
        offered_admitted = tuple(item for item in receipt.admitted if item in offered_ids)
        offered_set = set(offered_admitted)
        receipt = replace(
            receipt,
            admitted=offered_admitted,
            chosen=offered_admitted[0] if offered_admitted else None,
            empty_intersection=not offered_admitted,
            free_admitted=tuple(item for item in receipt.free_admitted if item in offered_set),
            paid_admitted=tuple(item for item in receipt.paid_admitted if item in offered_set),
        )
        try:
            receipt, pool = self._apply_candidate_pool(
                receipt,
                profile=profile,
                metadata=metadata,
                identity_map=identity_map,
                candidate_universe=offered_universe,
                policy_allowed=policy_admitted,
            )
        except CandidatePoolError as exc:
            raise ExecutionPathError(f"candidate pool rejected live offers: {exc}") from exc
        confirm_transport = self._resolve_confirm_transport(endpoint)
        confirm_live = bool(
            live and confirm_transport is not None and self.confirm_transport is None
        )
        confirmed = gate_admit_prove_confirm(
            receipt,
            passports=self.passports,
            passport_store_path=self.passport_store_path,
            confirm_transport=confirm_transport,
            now=self.admit_now,
            live=confirm_live,
            consented=confirm_live or self.confirm_transport is not None,
            max_confirm_candidates=self.candidate_top_k,
        )
        confirmed_ids = set(confirmed.admitted)
        confirmed = replace(confirmed, candidate_pool=pool.to_dict())
        pool = replace(
            pool,
            confirmation={
                "receipt_id": confirmed.receipt_id,
                "admitted": list(confirmed.admitted),
                "exclusions": [item.to_dict() for item in confirmed.exclusions],
                "passport": [
                    item.to_dict() if hasattr(item, "to_dict") else item
                    for item in confirmed.passport
                ],
                "confirm": [
                    item.to_dict() if hasattr(item, "to_dict") else item
                    for item in confirmed.confirm
                ],
            },
        )
        plans, cannot_fit = self._estimate_candidate_context_plans(
            task,
            profile=profile,
            candidates=tuple(confirmed.admitted),
            metadata=metadata,
            identity_map=identity_map,
            context=context,
        )
        plans_by_id = {plan.candidate_id: plan for plan in plans}
        confirmed_ids.difference_update(cannot_fit)
        filtered_offers_list = []
        for offer in request.offers:
            if offer.route.route_id not in confirmed_ids:
                continue
            plan = plans_by_id[offer.route.route_id]
            assistance = _bind_context_plan(offer.assistance_plan, plan)
            filtered_offers_list.append(
                replace(
                    offer,
                    assistance_plan=assistance,
                    expected_cost=_cost_with_context_plan(
                        offer.expected_cost, new_assistance=assistance.assistance_cost, plan=plan
                    ),
                )
            )
        filtered_offers = tuple(filtered_offers_list)
        excluded = set(request.hard_excluded_ids)
        excluded.update(offered_ids - confirmed_ids)
        assumptions = (
            *request.assumptions,
            f"task_profile={profile.digest}",
            f"candidate_pool={pool.shortlist_digest}",
            *(f"context_plan={plan.candidate_id}:{plan.digest}" for plan in plans),
            "live_confirm_scope=candidate_pool_shortlist",
        )
        return replace(
            request,
            offers=filtered_offers,
            pool_receipt=pool,
            hard_excluded_ids=frozenset(excluded),
            assumptions=assumptions,
        )

    def prepare_controller_execution_request(
        self, task: str, criticality: str, context: dict[str, Any], request: Any
    ) -> Any:
        """Strict controller-mode preparation around live eligibility + ContextPlan.

        Unlike the compatibility ``_prepare_execution_path_request`` path, this
        method fails closed when the admit snapshot or Core metadata is missing.
        It never accepts a trusted prebuilt pool escape and never invents offers.
        """
        from verdict.execution_path import ExecutionPathError, ExecutionPathRequest

        if not isinstance(request, ExecutionPathRequest):
            raise ExecutionPathError(
                "controller preparation requires an ExecutionPathRequest with seed offers"
            )
        if request.pool_receipt is not None:
            raise ExecutionPathError(
                "controller preparation rejects trusted prebuilt pool_receipt escape"
            )
        if not request.offers:
            raise ExecutionPathError(
                "controller preparation requires non-empty seed offers from live identities"
            )
        snapshot, _endpoint, _live, fetch_error = self._load_admit_snapshot()
        if snapshot is None:
            detail = "missing live admit snapshot"
            if fetch_error:
                detail = f"{detail}: {fetch_error}"
            raise ExecutionPathError(detail)
        metadata, _identity_map = self._load_metadata()
        if metadata is None:
            raise ExecutionPathError(
                "controller preparation requires Core metadata for live eligibility"
            )
        prepared = self._prepare_execution_path_request(task, criticality, context, request)
        if prepared is request and request.pool_receipt is None:
            # Compatibility escape must never succeed in controller mode.
            raise ExecutionPathError(
                "controller preparation failed to apply live eligibility/context plans"
            )
        return prepared

    def _offload_free_tier(
        self,
        task: str,
        final_tier: int,
        escalated: bool,
        esc_reason: str,
        context: dict[str, Any] | None = None,
    ) -> RoutingDecision | None:
        """Admit free-tier ∩ active-provider identities for offloadable work.

        Returns ``None`` when OmniRoute admit surfaces are not configured *or*
        cannot be loaded, so the historical catalog/fallback path still runs for
        offline unit tests and dead endpoints. Fail-closed empty-intersection
        applies only after a real (live or fixture) snapshot was consulted.
        """
        explicit_spend = isinstance(context, dict) and "spend_policy" in context
        authority_required = self.require_execution_path_authority is True or (
            isinstance(context, dict) and context.get("require_execution_path_authority") is True
        )
        explicit_omniroute = (
            self.admit_snapshot is not None
            or bool(os.getenv("OMNIROUTE_BASE_URL"))
            or "omniroute" in self.providers
        )
        if not explicit_omniroute and not explicit_spend and not authority_required:
            # The implicit localhost endpoint is not authority for legacy calls.
            return None
        snapshot, endpoint, live, _fetch_error = self._load_admit_snapshot()
        if snapshot is None:
            if explicit_spend:
                profile_task(task, context=context)
            if explicit_spend or authority_required:
                message = (
                    "cannot enforce explicit spend_policy without an admit snapshot"
                    if explicit_spend
                    else "cannot enforce authoritative eligibility without a valid OmniRoute snapshot"
                )
                raise TaskProfileError(message + (f" ({_fetch_error})" if _fetch_error else ""))
            # Compatibility-only offline callers may use the historical path.
            return None
        classification = classify_worthiness(
            task,
            criticality=(
                str(context.get("criticality"))
                if isinstance(context, dict) and isinstance(context.get("criticality"), str)
                else "medium"
            ),
            context=context,
        )
        planner_caps: tuple[str, ...] = ()
        try:
            planned = self.planner.plan(task, context=context).task_spec
            planner_caps = tuple(planned.required_capabilities)
        except Exception:
            planner_caps = ()
        requirements = derive_requirements(task, context, planner_capabilities=planner_caps)
        # BOD-S1: one deterministic profile before admission; the digest and
        # explicit spend policy ride the receipt so economic decisions are
        # replayable and never inferred from model names.
        profile = profile_task(task, context=context, requirements=requirements)
        receipt = admit_free_tier_active(snapshot)
        receipt = expand_admit_for_worthiness(
            receipt,
            snapshot,
            task_class=classification.task_class,
            class_reasons=classification.class_reasons,
            frontier_allowlist=self.frontier_allowlist,
            spend_policy=profile.spend_policy,
            task_profile_digest=profile.digest,
        )
        metadata_snapshot, identity_map = self._load_metadata()
        explicit_metadata = (
            self.metadata_snapshot is not None or self.metadata_store_path is not None
        )
        # Compatibility callers without BOD-104 authority retain the legacy
        # admit path. Production/authority mode must use the default Core store
        # as the live shortlist source when it is available.
        use_candidate_pool = explicit_metadata or self.require_execution_path_authority is True
        metadata_for_gate = metadata_snapshot if use_candidate_pool else None
        policy_admitted = receipt.admitted
        receipt = gate_capability(
            receipt,
            requirements,
            snapshot=metadata_for_gate,
            identity_map=identity_map if use_candidate_pool else None,
            now=self.admit_now,
        )
        if metadata_for_gate is not None:
            receipt, _pool = self._apply_candidate_pool(
                receipt,
                profile=profile,
                metadata=metadata_for_gate,
                identity_map=identity_map,
                candidate_universe=policy_admitted,
                policy_allowed=policy_admitted,
            )
        confirm_transport = self._resolve_confirm_transport(endpoint)
        # Live OmniRoute confirm requires consent; fixture/injected transports do not.
        confirm_live = bool(
            live and confirm_transport is not None and self.confirm_transport is None
        )
        receipt = gate_admit_prove_confirm(
            receipt,
            passports=self.passports,
            passport_store_path=self.passport_store_path,
            confirm_transport=confirm_transport,
            now=self.admit_now,
            live=confirm_live,
            consented=confirm_live or self.confirm_transport is not None,
            max_confirm_candidates=self.candidate_top_k,
        )
        plans, cannot_fit = self._estimate_candidate_context_plans(
            task,
            profile=profile,
            candidates=receipt.admitted,
            metadata=metadata_snapshot,
            identity_map=identity_map,
            context=context,
        )
        if cannot_fit:
            plan_admitted = tuple(item for item in receipt.admitted if item not in cannot_fit)
            receipt = replace(
                receipt,
                admitted=plan_admitted,
                chosen=plan_admitted[0] if plan_admitted else None,
                empty_intersection=not plan_admitted,
                exclusions=receipt.exclusions
                + tuple(
                    NamedDrop(
                        item,
                        "context_plan_budget_insufficient",
                        "estimated mandatory input plus reserves exceeds candidate window",
                    )
                    for item in sorted(cannot_fit)
                ),
                context_plans=plans,
            )
        else:
            receipt = replace(receipt, context_plans=plans)
        eligibility = receipt.as_eligibility_result(snapshot)
        if self.eligibility_gate is not None:
            gated = self.eligibility_gate.evaluate(
                eligibility.admitted, protected=False, dev_mode=(self.profile == "development")
            )
            kept = {model.id for model in gated.admitted}
            extra = tuple(
                NamedDrop(
                    record.model_id, getattr(record.verdict, "value", record.verdict), record.reason
                )
                for record in gated.exclusions
            )
            remaining = tuple(item for item in receipt.admitted if item in kept)
            chosen = (
                receipt.chosen if receipt.chosen in kept else (remaining[0] if remaining else None)
            )
            receipt = replace(
                receipt,
                admitted=remaining,
                exclusions=receipt.exclusions + extra,
                chosen=chosen,
                empty_intersection=chosen is None,
            )
            eligibility = gated
        if receipt.admitted:
            try:
                receipt = apply_best_of_admitted(
                    receipt,
                    passports=self.passports,
                    now=self.admit_now,
                    task_class=classification.protected_ranker_class,
                )
            except ChooserError:
                receipt = replace(
                    receipt, chosen=None, empty_intersection=True, selected_because=None
                )
        return self._decision_from_admit(
            task,
            final_tier,
            escalated,
            esc_reason,
            receipt,
            eligibility,
            endpoint=endpoint,
            live=live,
            task_class=classification.task_class,
            context=context,
        )

    def _load_admit_snapshot(
        self,
    ) -> tuple[OmniRouteAdmitSnapshot | None, tuple[str, str | None] | None, bool, str | None]:
        if self.admit_snapshot is not None:
            return (self.admit_snapshot, omniroute_endpoint_from_env(self.providers), False, None)
        endpoint = omniroute_endpoint_from_env(self.providers)
        if endpoint is None or not str(endpoint[0] or "").strip():
            return None, None, False, "omniroute_not_configured"
        now = time.monotonic()
        ttl = max(1, int(self.discovery_ttl))
        if (
            self._admit_snapshot_lkg is not None
            and self._admit_snapshot_endpoint == endpoint
            and now - self._admit_snapshot_loaded_at < ttl
        ):
            return self._admit_snapshot_lkg, endpoint, True, self._admit_snapshot_refresh_error
        try:
            snapshot = load_omniroute_admit_snapshot(endpoint[0], endpoint[1])
            if not snapshot.catalog or not snapshot.connections:
                raise LiveAdmitError("invalid_empty", "OmniRoute refresh returned incomplete state")
        except LiveAdmitError as exc:
            self._admit_snapshot_refresh_error = f"{exc.code}:{exc}"
            if self._admit_snapshot_lkg is not None and self._admit_snapshot_endpoint == endpoint:
                return self._admit_snapshot_lkg, endpoint, True, self._admit_snapshot_refresh_error
            return None, endpoint, True, self._admit_snapshot_refresh_error
        self._admit_snapshot_lkg = snapshot
        self._admit_snapshot_endpoint = endpoint
        self._admit_snapshot_loaded_at = now
        self._admit_snapshot_refresh_error = None
        return snapshot, endpoint, True, None

    def _persist_routing_receipt_from_admit(
        self,
        receipt: FreeTierAdmitReceipt,
        *,
        task_class: str,
        decision: str,
        transport_outcome: str,
        verification: dict[str, Any] | None = None,
        preview: str | None = None,
        context_pack: Any | None = None,
    ) -> str | None:
        """Default-on BOD-144 routing receipt persistence (never blocks routing)."""
        if not self.persist_routing_receipts:
            return None
        try:
            from verdict.routing_receipt import (
                attempt_scope,
                build_routing_receipt,
                default_receipt_store,
                finalize_routing_receipt,
                persist_routing_receipt,
            )

            store = self.receipt_store or default_receipt_store(
                Path(self.workspace_root) if self.workspace_root is not None else None
            )
            attempt_id = receipt.receipt_id or (
                "admit-"
                + hashlib.sha256((receipt.chosen or task_class or "task").encode()).hexdigest()[:16]
            )
            story_id = Path(self.workspace_root).name if self.workspace_root is not None else None
            selected = None
            if receipt.chosen:
                provider = receipt.chosen.split("/", 1)[0] if "/" in receipt.chosen else "omniroute"
                selected = {
                    "gateway": "omniroute",
                    "provider": provider,
                    "model": receipt.chosen,
                    "resource_pool": "default",
                    "route_id": receipt.chosen,
                }
            routing = build_routing_receipt(
                admit=receipt,
                context_pack=context_pack,
                selected_identity=selected,
                observed_identity=(
                    selected if transport_outcome not in {None, "", "not_sent"} else None
                ),
                execution_status=transport_outcome,
                verification=verification,
                story_id=story_id,
                work_unit_id=task_class,
                attempt_id=attempt_id,
                state="in_progress",
                extensions={"routing_decision": decision, "preview_len": len(preview or "")},
            )
            scope = attempt_scope(
                story_id=routing.story_id,
                work_unit_id=routing.work_unit_id,
                attempt_id=routing.attempt_id,
            )
            persist_routing_receipt(store, routing, scope=scope)
            if decision in {"selected", "denied"}:
                failed = decision == "denied" or (verification or {}).get("status") == "failed"
                finalize_routing_receipt(
                    store,
                    routing,
                    scope=scope,
                    outcome="failed" if failed else "success",
                    verification=verification,
                    observed_identity=selected,
                    execution_status=transport_outcome or "completed",
                )
            return routing.receipt_id
        except Exception:
            return None

    def _decision_from_admit(
        self,
        task: str,
        final_tier: int,
        escalated: bool,
        esc_reason: str,
        receipt: FreeTierAdmitReceipt,
        eligibility: Any,
        *,
        endpoint: tuple[str, str | None] | None,
        live: bool,
        task_class: str = "ordinary",
        context: dict[str, Any] | None = None,
    ) -> RoutingDecision:
        eligibility_record = eligibility.to_dict() if eligibility is not None else {}
        if receipt.empty_intersection or not receipt.chosen:
            self._persist_routing_receipt_from_admit(
                receipt, task_class=task_class, decision="denied", transport_outcome="not_sent"
            )
            return RoutingDecision(
                model=NO_ELIGIBLE_TARGET,
                provider="none",
                tier=final_tier,
                reason=FAIL_CLOSED_REASON,
                escalated=escalated,
                escalation_reason=esc_reason or None,
                policy_version=self._policy_version,
                degraded_mode=False,
                managed_backend_status=self.managed_backend_status,
                protected=False,
                task_class=task_class,
                decision="denied",
                transport_outcome="not_sent",
                quality_outcome="unknown",
                candidate_states=eligibility_record.get("records", []),
                safety_flags=["fail_closed_empty_free_active_intersection"],
                admit_receipt=receipt.to_dict(),
            )
        chosen = receipt.chosen
        provider = next(
            (model.provider for model in eligibility.admitted if model.id == chosen),
            chosen.split("/", 1)[0] if "/" in chosen else "omniroute",
        )
        contract = context if isinstance(context, dict) else {}
        acceptance_criteria = tuple(
            str(item) for item in contract.get("acceptance_criteria", ()) if str(item).strip()
        )
        proof_criteria = tuple(
            str(item) for item in contract.get("proof_criteria", ()) if str(item).strip()
        )
        errors = tuple(str(item) for item in contract.get("errors", ()) if str(item).strip())
        # Cheap path: gather real provenance units, compile under budget, then
        # execute. Digest + named omissions land on the admit receipt.
        chosen_plan = next(
            (plan for plan in receipt.context_plans if plan.candidate_id == chosen), None
        )
        context_pack = build_cheap_path_context_pack(
            task,
            candidate_id=chosen,
            context_plan=chosen_plan,
            workspace_root=self.workspace_root,
            workspace_roots=self.context_roots,
            mcp_root=self.mcp_root,
            acceptance_criteria=acceptance_criteria,
            proof_criteria=proof_criteria,
            errors=errors,
            use_context_fabric=True,
        )
        receipt = replace(
            receipt,
            pack_digest=context_pack.pack_digest,
            omissions=context_pack.omissions,
            included=context_pack.included,
            pack_state=context_pack.pack_state,
            task_complete=context_pack.task_complete,
            required_sources=context_pack.required_sources,
            prompt_digest=context_pack.prompt_digest,
            capability_coverage=context_pack.capability_coverage,
        )
        if not context_pack.task_complete:
            # BOD-110: the compiled pack no longer carries the task instructions.
            # Executing it would send the model context without the request, so
            # the decision is denied with a named reason instead of "hydrated".
            self._persist_routing_receipt_from_admit(
                receipt,
                task_class=task_class,
                decision="denied",
                transport_outcome="not_sent",
                context_pack=context_pack,
            )
            return RoutingDecision(
                model=NO_ELIGIBLE_TARGET,
                provider="none",
                tier=final_tier,
                reason=TASK_INSTRUCTIONS_OMITTED_REASON,
                escalated=escalated,
                escalation_reason=esc_reason or None,
                policy_version=self._policy_version,
                degraded_mode=False,
                managed_backend_status=self.managed_backend_status,
                protected=False,
                task_class=task_class,
                decision="denied",
                transport_outcome="not_sent",
                quality_outcome="unknown",
                candidate_states=eligibility_record.get("records", []),
                safety_flags=["cheap_path_context_pack", "task_instructions_omitted"],
                admit_receipt=receipt.to_dict(),
            )
        packed_task = context_pack.compiled_prompt
        should_execute = self.execute_offload
        if should_execute is None:
            should_execute = live and self.offload_executor is None
        transport_outcome = "not_sent"
        preview: str | None = None
        attempt: dict[str, Any] = {
            "attempt": 1,
            "selected_model": chosen,
            "executed_model": None,
            "transport_outcome": "not_sent",
        }
        if should_execute or self.offload_executor is not None:
            if self.offload_executor is not None:
                transport_outcome, preview = self.offload_executor(chosen, packed_task)
            elif endpoint is not None:
                transport_outcome, preview = execute_offload_chat(
                    endpoint[0], chosen, packed_task, api_key=endpoint[1]
                )
            attempt["transport_outcome"] = transport_outcome
            attempt["executed_model"] = chosen if transport_outcome == "sent" else None
        verification: dict[str, Any] = {
            "status": "unknown",
            "reason": "no verification strategy executed",
        }
        expected = contract.get("expected_output_contains")
        proof_criteria = tuple(
            str(item).strip()
            for item in contract.get("proof_criteria", ())
            if isinstance(item, str) and item.strip()
        )
        required_checks: list[str] = []
        if isinstance(expected, str) and expected.strip():
            required_checks.append(expected.strip())
        required_checks.extend(proof_criteria)
        if transport_outcome == "sent" and required_checks:
            output = preview if isinstance(preview, str) else ""
            failed_checks = [check for check in required_checks if check not in output]
            verification = {
                "status": "passed" if not failed_checks else "failed",
                "reason": "bounded_output_contains",
                "criteria": required_checks,
                "failed_criteria": failed_checks,
            }
        receipt_seed = "|".join(
            [chosen, context_pack.pack_digest, transport_outcome, context_pack.prompt_digest]
        )
        receipt = replace(
            receipt,
            execution_attempts=(attempt,),
            verification=verification,
            receipt_id="vrct_" + hashlib.sha256(receipt_seed.encode()).hexdigest()[:24],
        )
        chooser_owned = bool(receipt.selected_because) and receipt.chosen is not None
        safety_flags = ["free_tier_active_admit", "prove_confirm_admit", "cheap_path_context_pack"]
        if chooser_owned:
            safety_flags.append("chooser_ranked_admitted")
        if receipt.task_class:
            safety_flags.append(f"worthiness_{receipt.task_class}")
        if receipt.requirements:
            safety_flags.append("capability_hard_gate")
        self._persist_routing_receipt_from_admit(
            receipt,
            task_class=task_class,
            decision="selected",
            transport_outcome=transport_outcome,
            verification=verification,
            preview=preview,
            context_pack=context_pack,
        )
        return RoutingDecision(
            model=chosen,
            provider=provider,
            tier=final_tier,
            reason=f"free∩active ∩ fresh-passport ∩ confirmed admitted {chosen}",
            alternatives=list(receipt.admitted[:8]),
            escalated=escalated,
            escalation_reason=esc_reason or None,
            policy_version=self._policy_version,
            degraded_mode=False,
            managed_backend_status=self.managed_backend_status,
            protected=False,
            task_class=task_class,
            decision="selected",
            transport_outcome=transport_outcome,
            quality_outcome=(
                "verified"
                if verification["status"] == "passed"
                else "failed"
                if verification["status"] == "failed"
                else "unknown"
            ),
            candidate_states=eligibility_record.get("records", []),
            safety_flags=safety_flags,
            admit_receipt=receipt.to_dict(),
            execute_preview=preview,
            context_pack_prompt=packed_task,
        )

    def _resolve_confirm_transport(
        self, endpoint: tuple[str, str | None] | None
    ) -> ProbeTransport | None:
        """Prefer an injected confirm transport; else OmniRoute OpenAI probe."""
        if self.confirm_transport is not None:
            return self.confirm_transport
        if endpoint is None or not str(endpoint[0] or "").strip():
            return None
        origin = normalize_omniroute_origin(endpoint[0])
        return openai_probe_transport(f"{origin}/v1", api_key=endpoint[1])

    def execute_argv(self, argv: list[str]) -> dict[str, Any]:
        """Execute an argument vector via subprocess and return structured output.

        Args:
            argv: List of command and arguments to execute.

        Returns:
            Dictionary with keys: stdout, stderr, returncode.
        """
        try:
            result = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=5.0,  # reasonable default timeout
            )
            return {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode,
            }
        except subprocess.TimeoutExpired as e:
            return {
                "stdout": e.stdout or "",
                "stderr": e.stderr or "",
                "returncode": -1,
                "error": "timeout",
            }
        except Exception as e:
            return {"stdout": "", "stderr": str(e), "returncode": -2, "error": str(e)}
