"""Main Gate entry point."""
import time
from dataclasses import replace

from llm_gate.models import ProviderConfig, RoutingDecision
from llm_gate.escalation import scan
from llm_gate.discovery import fetch_models
from llm_gate.router import select_best_model
from llm_gate.logger import log_decision
from llm_gate.headroom import check_headroom
from llm_gate.neural import LearnedRouter


TIER_MAP = {"critical": 0, "high": 1, "medium": 2, "low": 3}


class Gate:
    """The main router client."""
    
    def __init__(
        self,
        primary_model: str,
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


    def route(self, task: str, criticality: str = "medium", context: dict | None = None) -> RoutingDecision:
        """Route a task to the most effective LLM model based on criticality."""
        start_t = time.time()
        
        req_tier = TIER_MAP.get(criticality.lower(), 2)

        # 1. Escalate based on keywords
                # Execute Learned Routing Prediction
        predicted_tier, learned_reason = self.learned_router.predict_optimal_model(task, req_tier, [])
        if predicted_tier < req_tier:
            req_tier = predicted_tier
            esc_reason = learned_reason

        # Fallback to strict heuristic scan
        eff_tier, heuristic_reason = scan(task)
        if eff_tier is not None and eff_tier < req_tier:
            req_tier = eff_tier
            esc_reason = heuristic_reason
final_tier = min(req_tier, eff_tier) if eff_tier is not None else req_tier
        escalated = (eff_tier is not None and eff_tier < req_tier)

        # 2. Hard critical boundary
        if final_tier == 0:
            dec = RoutingDecision(
                model=self.primary_model, provider="primary", tier=0,
                reason="critical — never offload", escalated=escalated,
                escalation_reason=esc_reason
            )
            self._log(task, req_tier, dec)
            return replace(dec, logged=bool(self.log_path))

        # 3. Discover candidates
        candidates = []
        for name, cfg in self.providers.items():
            for m in fetch_models(name, cfg, self.discovery_ttl):
                is_avail, pct = check_headroom(m.id, name, cfg)
                if is_avail:
                    candidates.append(m)

        # 4. Filter and select
        chosen, alts = select_best_model(candidates, final_tier, self.providers)

        # 5. Build final decision
        if chosen:
            msg = f"escalated to tier {final_tier} ({esc_reason})" if escalated else f"standard offload tier {final_tier}"
            dec = RoutingDecision(
                model=chosen.id, provider=chosen.provider, tier=final_tier,
                reason=f"{msg}; selected {chosen.id}", alternatives=alts,
                headroom_pct=100.0, escalated=escalated, escalation_reason=esc_reason
            )
        else:
            dec = RoutingDecision(
                model=self.primary_model, provider="primary", tier=final_tier,
                reason="fail-open — no offload capacity", escalated=escalated,
                escalation_reason=esc_reason, alternatives=alts
            )

        dec = replace(dec, latency_ms=round((time.time() - start_t) * 1000, 2))
        self._log(task, req_tier, dec)
        return replace(dec, logged=bool(self.log_path))

    def _log(self, task: str, input_tier: int, decision: RoutingDecision) -> None:
        if self.log_path:
            log_decision(self.log_path, task, input_tier, decision, self.log_full_task)
