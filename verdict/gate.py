"""Main Gate entry point."""

from verdict.intelligence import DEGRADED_PROFILE, IntelligenceService
from verdict.models import ProviderConfig, RoutingDecision

TIER_MAP = {"critical": 0, "high": 1, "medium": 2, "low": 3}


class Gate:
    """The main router client."""

    TIER_MAP = TIER_MAP

    def __init__(
        self,
        primary_model: str = "anthropic/claude-3-opus-20240229",
        providers: dict[str, ProviderConfig] | None = None,
        log_path: str = "verdict-decisions.jsonl",
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
        # The HTTP API exposes the intelligence path asynchronously, while the
        # CLI and historical Python client have always offered a synchronous
        # Gate.route().  Keep both contracts without duplicating routing logic.
        import asyncio
        import inspect

        result = self.intelligence.route(task, criticality=criticality, context=context)
        if inspect.isawaitable(result):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return asyncio.run(result)
            # Gate.route is not called from the async API path.  If an embedder
            # does call it from a running loop, execute the tiny coroutine in a
            # helper thread rather than returning an unexpected coroutine.
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(asyncio.run, result).result()
        return result
