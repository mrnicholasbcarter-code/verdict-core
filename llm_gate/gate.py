"""Main Gate entry point."""

import time
from dataclasses import replace
from typing import Any

from llm_gate.discovery import fetch_models
from llm_gate.escalation import scan
from llm_gate.logger import log_decision
from llm_gate.models import ProviderConfig, RoutingDecision
from llm_gate.neural import LearnedRouter
from llm_gate.router import select_best_model

TIER_MAP = {"critical": 0, "high": 1, "medium": 2, "low": 3}


class Gate:
    """The main router client."""

    def __init__(
        self,
        primary_model: str = "anthropic/claude-3-opus-20240229",
        providers: dict[str, ProviderConfig] | None = None,
        log_path: str = "llm-gate-decisions.jsonl",
        log_full_task: bool = False,
        discovery_ttl: int = 60,
    ):
        self.primary_model = primary_model
        self.providers = providers or {}
        self.log_path = log_path
        self.log_full_task = log_full_task
        self.discovery_ttl = discovery_ttl
        self.learned_router = LearnedRouter(log_path)

    def route(
        self, task: str, criticality: str = "medium", context: dict[str, Any] | None = None
    ) -> RoutingDecision:
        """Route a task to the most effective LLM model based on criticality."""
        start_t = time.time()

        req_tier = TIER_MAP.get(criticality.lower(), 2)
        esc_reason: str = ""
        escalated = False

        # 1. Execute Learned Routing Prediction
        predicted_tier, learned_reason = self.learned_router.predict_optimal_model(
            task, req_tier, []
        )
        if predicted_tier < req_tier:
            req_tier = predicted_tier
            esc_reason = learned_reason
            escalated = True

        # 2. Fallback to strict heuristic scan
        eff_tier, heuristic_reason = scan(task)
        if eff_tier is not None and eff_tier < req_tier:
            req_tier = eff_tier
            esc_reason = heuristic_reason or ""
            escalated = True

        final_tier = min(req_tier, eff_tier) if eff_tier is not None else req_tier

        # 3. Hard critical boundary
        if final_tier == 0:
            dec = RoutingDecision(
                model=self.primary_model,
                provider="primary",
                tier=0,
                reason="critical — never offload",
                escalated=escalated,
                escalation_reason=esc_reason,
            )
            log_decision(self.log_path, task, req_tier, dec, self.log_full_task)
            elapsed = (time.time() - start_t) * 1000
            return replace(dec, latency_ms=elapsed, logged=bool(self.log_path))

        # 4. Discover candidates from providers
        candidates = []
        provider_configs = {}
        for name, cfg in self.providers.items():
            for m in fetch_models(name, cfg, self.discovery_ttl):
                candidates.append(m)
                provider_configs[name] = cfg

        # 5. Select best model that fits the tier
        if candidates:
            best_model, _ = select_best_model(candidates, final_tier, provider_configs)
            if best_model:
                dec = RoutingDecision(
                    model=best_model.id,
                    provider=best_model.provider,
                    tier=best_model.capability_tier,
                    reason=f"tier {final_tier} routed",
                    escalated=escalated,
                    escalation_reason=esc_reason,
                )
                log_decision(self.log_path, task, req_tier, dec, self.log_full_task)
                elapsed = (time.time() - start_t) * 1000
                return replace(dec, latency_ms=elapsed, logged=bool(self.log_path))

        # 6. Fallback to primary
        dec = RoutingDecision(
            model=self.primary_model,
            provider="primary",
            tier=0,
            reason="fallback — no offload match",
            escalated=escalated,
            escalation_reason=esc_reason,
        )
        log_decision(self.log_path, task, req_tier, dec, self.log_full_task)
        elapsed = (time.time() - start_t) * 1000
        return replace(dec, latency_ms=elapsed, logged=bool(self.log_path))
