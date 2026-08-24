"""
Subagent Role-Aware Model Selection via OmniRoute/Verdict Eligibility Pipeline

Implements dynamic model selection for pi-subagents roles using the existing
OmniRoute availability + Verdict eligibility gate + IntelligenceService ranking.

This does NOT create a parallel system - it reuses the existing:
- OmniRouteAvailabilityAdapter (catalog + runtime)
- AvailabilityCache (TTL caching)
- EligibilityGate (single-source-of-truth filter BEFORE ranking)
- IntelligenceService.rank() (advisory ordering)
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from dataclasses import dataclass, field

from verdict.availability import (
    AvailabilityReport,
    CandidateRequirements,
    OmniRouteAvailabilityAdapter,
    is_opaque_route_id,
)
from verdict.availability_cache import AvailabilityCache
from verdict.eligibility import EligibilityGate
from verdict.intelligence import IntelligenceService
from verdict.models import ModelInfo

# ─── Role Requirements Policy ──────────────────────────────────────────────
# Maps pi-subagent role → CandidateRequirements
# These are POLICY, not hardcoded models. The eligibility gate + ranking select actual models.
SUBAGENT_ROLE_REQUIREMENTS: dict[str, CandidateRequirements] = {
    "scout": CandidateRequirements(
        required=frozenset({"tools"}),  # fast recon needs tool calling
        budget_remaining=0.50,
        max_concurrency=8,
        allow_degraded=True,  # speed over perfection
        estimated_tokens=4000,
        estimated_cost=0.10,
    ),
    "worker": CandidateRequirements(
        required=frozenset({"tools", "reasoning"}),
        budget_remaining=5.00,
        max_concurrency=3,
        estimated_tokens=50000,
        estimated_cost=1.00,
    ),
    "reviewer": CandidateRequirements(
        required=frozenset({"tools", "reasoning"}),
        budget_remaining=10.00,
        max_concurrency=2,
        estimated_tokens=80000,
        estimated_cost=2.00,
    ),
    "oracle": CandidateRequirements(
        required=frozenset({"tools", "reasoning"}),
        budget_remaining=20.00,
        max_concurrency=1,
        estimated_tokens=100000,
        estimated_cost=5.00,
    ),
    "planner": CandidateRequirements(
        required=frozenset({"reasoning"}),
        budget_remaining=3.00,
        max_concurrency=2,
        estimated_tokens=30000,
        estimated_cost=0.50,
    ),
    "researcher": CandidateRequirements(
        required=frozenset({"tools", "reasoning"}),
        budget_remaining=15.00,
        max_concurrency=3,
        estimated_tokens=150000,
        estimated_cost=3.00,
    ),
    "context-builder": CandidateRequirements(
        required=frozenset({"reasoning"}),
        budget_remaining=10.00,
        max_concurrency=2,
        estimated_tokens=200000,
        estimated_cost=2.00,
    ),
    "delegate": CandidateRequirements(
        required=frozenset({"tools"}),
        budget_remaining=2.00,
        max_concurrency=4,
        allow_degraded=True,
        estimated_tokens=10000,
        estimated_cost=0.25,
    ),
}

# ─── Diversity Exclusion Policy ────────────────────────────────────────────
# Model families to exclude when selecting diverse models for parallel roles
SUBAGENT_DIVERSITY_EXCLUSIONS: dict[str, set[str]] = {
    "reviewer": {"worker", "oracle"},  # avoid same family as worker/oracle
    "oracle": {"worker", "reviewer"},
    "worker": {"reviewer", "oracle"},
}


def _model_family(model_id: str) -> str:
    """Family key used for diversity exclusion: the id namespace, lowercased.

    Derived from the id itself so it stays valid across gateways; it must not
    read a gateway-specific provider/``owned_by`` field.
    """
    return model_id.strip().lower().split("/", 1)[0]



@dataclass
class SubagentModelSelector:
    """
    Role-aware model selector using the canonical Verdict eligibility pipeline.

    Flow:
      role → CandidateRequirements
        → AvailabilityCache.source() → AvailabilityReport (all candidates + runtime)
        → EligibilityGate.evaluate() → admitted ModelInfo (filters BEFORE ranking)
        → IntelligenceService.rank() → advisory ordering
        → select_best() → single ModelInfo
    """

    availability_cache: AvailabilityCache
    eligibility_gate: EligibilityGate
    intelligence: IntelligenceService

    # Track recently selected models for diversity
    _recent_selections: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_environment(cls) -> SubagentModelSelector:
        """Build selector from environment (OmniRoute base URL, etc.)."""
        base_url = (
            os.getenv("OMNIROUTE_BASE_URL")
            or os.getenv("LLMGATE_UPSTREAM_BASE_URL")
            or "http://127.0.0.1:20128/v1"
        )
        api_key = os.getenv("OMNIROUTE_API_KEY")
        management_token = os.getenv("OMNIROUTE_MANAGEMENT_TOKEN")
        usage_api_key_id = os.getenv("OMNIROUTE_USAGE_API_KEY_ID")

        from verdict.omniroute import OmniRouteHTTPTransport

        transport = OmniRouteHTTPTransport(
            base_url,
            api_key=api_key,
            management_token=management_token,
            usage_api_key_id=usage_api_key_id,
            allow_private_hosts={"127.0.0.1", "::1"},
            max_response_bytes=16_777_216,
        )

        adapter: Callable[[CandidateRequirements], AvailabilityReport] = (
            OmniRouteAvailabilityAdapter(transport).evaluate
        )

        # Enable probe-enriched adapter in production
        probe_enabled = (
            os.getenv("LLMGATE_AVAILABILITY_PROFILE", "development").lower() == "production"
        )
        probe_base_url = os.getenv("LLMGATE_PROBE_BASE_URL")
        if probe_enabled and probe_base_url:
            from verdict.availability import ProbeEnrichedAdapter
            from verdict.probes import openai_probe_transport

            probe_consented = os.getenv("LLMGATE_ALLOW_LIVE_PROBES", "").lower() in {
                "1",
                "true",
                "yes",
            }
            probe_transport = openai_probe_transport(
                probe_base_url, api_key=os.getenv("LLMGATE_PROBE_API_KEY") or api_key
            )
            adapter = ProbeEnrichedAdapter(
                OmniRouteAvailabilityAdapter(transport),
                probe_transport=probe_transport,
                enabled=True,
                live=True,
                consented=probe_consented,
                provider="omniroute",
            ).evaluate

        cache = AvailabilityCache(source=adapter, ttl_seconds=60, stale_window_seconds=30)

        gate = EligibilityGate(cache.get, protected_fail_closed=True, allow_unverified_in_dev=True)

        intelligence = IntelligenceService(
            primary_model=os.getenv("LLMGATE_PRIMARY", "anthropic/claude-3-opus-20240229"),
            providers={},
            profile=os.getenv("LLMGATE_INTELLIGENCE_PROFILE", "development"),
            log_path=os.getenv("LLMGATE_LOG_PATH", "verdict-decisions.jsonl"),
            log_full_task=False,
            discovery_ttl=60,
            eligibility_gate=gate,
        )

        return cls(availability_cache=cache, eligibility_gate=gate, intelligence=intelligence)

    def select_for_role(
        self,
        role: str,
        *,
        protected: bool = False,
        dev_mode: bool = True,
        diversity_from: list[str] | None = None,
    ) -> ModelInfo | None:
        """
        Select the best model for a pi-subagents role.

        Args:
            role: One of "scout", "worker", "reviewer", "oracle", "planner",
                  "researcher", "context-builder", "delegate"
            protected: If True, fail-closed when OmniRoute unavailable
            dev_mode: If True, allow unverified candidates when not protected
            diversity_from: Model IDs to exclude for diversity (e.g., avoid same family as another role)

        Returns:
            ModelInfo or None if no eligible model
        """
        requirements = SUBAGENT_ROLE_REQUIREMENTS.get(role)
        if requirements is None:
            raise ValueError(
                f"Unknown role: {role}. Valid: {list(SUBAGENT_ROLE_REQUIREMENTS.keys())}"
            )

        # Diversity exclusion is a *narrowing* applied after eligibility, never a
        # rewrite of the requirements handed to the adapter. The previous form
        # injected synthetic "family/*" wildcards into deny_models, which is
        # matched exactly against model.id upstream and therefore never fired.
        excluded_families = {
            _model_family(model_id) for model_id in (diversity_from or []) if "/" in model_id
        }

        # 1. Get full availability report ONCE (adapter evaluates all models)
        report = self.availability_cache.source(requirements)

        # 2. The adapter's deterministic eligibility verdict is authoritative.
        #    AC-1.5: nothing excluded here may be restored by ranking, reputation,
        #    or a second locally re-derived admission test.
        eligible_ids = {c.model.id for c in report.eligible}
        candidates_by_id = {c.model.id: c for c in report.candidates}

        def fast_lookup(model_id: str) -> AvailabilityReport:
            c = candidates_by_id.get(model_id)
            if c is None:
                return AvailabilityReport(
                    candidates=(),
                    eligible=(),
                    source="lookup",
                    freshness_seconds=None,
                    errors=("not found",),
                )
            # Honour the report's own eligible set rather than re-deriving it
            # from the raw state. Re-deriving ignored required capabilities,
            # deny/allow policy, protected mode, and allow_degraded, and so
            # readmitted candidates the eligibility pass had already excluded.
            eligible = (c,) if c.model.id in eligible_ids else ()
            return AvailabilityReport(
                candidates=(c,),
                eligible=eligible,
                source=c.source,
                freshness_seconds=report.freshness_seconds,
                errors=report.errors,
            )

        # 3. Create a temporary EligibilityGate with fast O(1) lookup
        from verdict.eligibility import EligibilityGate

        fast_gate = EligibilityGate(
            fast_lookup,
            protected_fail_closed=self.eligibility_gate.protected_fail_closed,
            allow_unverified_in_dev=self.eligibility_gate.allow_unverified_in_dev,
        )

        # 3b. Narrow to concrete, diversity-permitted routes BEFORE admission.
        # A selection must be a concrete route the gateway actually serves;
        # `is_opaque_route_id` is the single shared, gateway-neutral rule also
        # used by autodev_routing, and tests id shape only (spec 272 D-005).
        concrete = [
            c
            for c in report.eligible
            if not is_opaque_route_id(c.model.id)
            and _model_family(c.model.id) not in excluded_families
        ]

        # 4. Apply eligibility gate with fast O(1) lookup
        eligible_result = fast_gate.evaluate(
            [c.model for c in concrete], protected=protected, dev_mode=dev_mode
        )

        admitted = eligible_result.admitted
        if not admitted:
            return None

        # 3. Advisory ranking (cannot reintroduce excluded candidates)
        estimated_tokens = requirements.estimated_tokens
        estimated_cost = requirements.estimated_cost
        budget_per_1k = (
            estimated_cost / max(1, estimated_tokens / 1000)
            if estimated_cost is not None and estimated_tokens
            else None
        )
        task_spec = type(
            "TaskSpec",
            (),
            {
                "prompt": f"Subagent role: {role}",
                "criticality": "medium" if role in {"worker", "reviewer", "oracle"} else "low",
                "context": {"role": role},
                "requirements": [],
                "budget_per_1k": budget_per_1k,
                "privacy_level": "standard",
            },
        )()

        ranking = asyncio.run(self.intelligence.rank(admitted, task_spec))

        if not ranking.ranked:
            return None

        # 4. Select best from ranking
        best_ranked = ranking.ranked[0]

        # Find the full ModelInfo
        for model in admitted:
            if model.id == best_ranked.model_id:
                self._recent_selections[role] = model.id
                return model

        # Fallback
        model = admitted[0]
        self._recent_selections[role] = model.id
        return model

    def select_for_parallel_roles(
        self, roles: list[str], *, protected: bool = False, dev_mode: bool = True
    ) -> dict[str, ModelInfo | None]:
        """
        Select models for multiple roles with diversity awareness.

        Selects in order of budget/importance, excluding already-selected models
        from subsequent role selections to ensure model family diversity.
        """
        # Sort by budget descending (most important roles first)
        role_order = sorted(
            roles, key=lambda r: SUBAGENT_ROLE_REQUIREMENTS[r].budget_remaining or 0, reverse=True
        )

        results: dict[str, ModelInfo | None] = {}
        selected_ids: list[str] = []

        for role in role_order:
            model = self.select_for_role(
                role, protected=protected, dev_mode=dev_mode, diversity_from=selected_ids
            )
            results[role] = model
            if model:
                selected_ids.append(model.id)

        return results


# ─── Convenience Functions ───────────────────────────────────────────────────

_default_selector: SubagentModelSelector | None = None


def get_subagent_selector() -> SubagentModelSelector:
    """Get or create the default selector (lazy initialization)."""
    global _default_selector
    if _default_selector is None:
        _default_selector = SubagentModelSelector.from_environment()
    return _default_selector


def select_model_for_role(
    role: str,
    *,
    protected: bool = False,
    dev_mode: bool = True,
    diversity_from: list[str] | None = None,
) -> ModelInfo | None:
    """
    Convenience function: select model for a subagent role.

    Usage:
        model = select_model_for_role("worker")
        if model:
            print(f"Selected: {model.id} ({model.provider})")
    """
    selector = get_subagent_selector()
    return selector.select_for_role(
        role, protected=protected, dev_mode=dev_mode, diversity_from=diversity_from
    )


def select_models_for_parallel_roles(
    roles: list[str], *, protected: bool = False, dev_mode: bool = True
) -> dict[str, ModelInfo | None]:
    """Select models for multiple roles with diversity."""
    selector = get_subagent_selector()
    return selector.select_for_parallel_roles(roles, protected=protected, dev_mode=dev_mode)
