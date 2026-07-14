"""Main Gate entry point."""

from llm_gate.intelligence import DEGRADED_PROFILE, IntelligenceService
from llm_gate.models import ProviderConfig, RoutingDecision

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
        profile: str = DEGRADED_PROFILE,
        intelligence_service: IntelligenceService | None = None,
    ):
        self.primary_model = primary_model
        self.providers = providers or {}
        self.log_path = log_path
        self.log_full_task = log_full_task
        self.discovery_ttl = discovery_ttl
        self.intelligence = intelligence_service or IntelligenceService(
            primary_model=primary_model,
            providers=self.providers,
            profile=profile,
            log_path=log_path,
            log_full_task=log_full_task,
            discovery_ttl=discovery_ttl,
        )

    def route(
        self, task: str, criticality: str = "medium", context: dict[str, object] | None = None
    ) -> RoutingDecision:
        """Route a task to the most effective LLM model based on criticality."""
        return self.intelligence.route(task, criticality=criticality, context=context)
