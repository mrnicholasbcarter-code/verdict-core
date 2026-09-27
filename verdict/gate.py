"""Main Gate entry point."""

from dataclasses import dataclass
from datetime import datetime, timezone

from verdict.contracts import DEFAULT_PRIMARY_MODEL
from verdict.intelligence import DEGRADED_PROFILE, IntelligenceService
from verdict.models import ProviderConfig, RoutingDecision

COMPLEXITY_MAP = {"critical": 0.9, "high": 0.7, "medium": 0.5, "low": 0.2}

STRATEGY_DIRECT = "DIRECT"
STRATEGY_SWARM_AUTODEV = "SWARM_AUTODEV"


@dataclass(frozen=True)
class StrategySelection:
    """Deterministic execution-strategy record emitted with a route (issue #265).

    ``strategy`` is ``DIRECT`` (single frontier call) or ``SWARM_AUTODEV``
    (the twelve-stage delegation path).  ``strategy``, ``model``, and
    ``reasoning`` are pure functions of the routing decision; only
    ``timestamp`` varies between runs.
    """

    strategy: str
    model: str
    reasoning: str
    timestamp: str

    def to_dict(self) -> dict[str, str]:
        return {
            "strategy": self.strategy,
            "model": self.model,
            "reasoning": self.reasoning,
            "timestamp": self.timestamp,
        }


def strategy_from_decision(decision: RoutingDecision) -> StrategySelection:
    """Derive the deterministic strategy record from a routing decision.

    Protected or frontier-tier work (tier <= 1) is executed through the
    governed SWARM_AUTODEV pipeline; routine tiers run DIRECT.  The rule is
    a pure function of the decision so the same task always produces the
    same strategy, model, and reasoning.
    """
    swarm = decision.protected or decision.tier <= 1
    strategy = STRATEGY_SWARM_AUTODEV if swarm else STRATEGY_DIRECT
    reasoning = (
        f"tier={decision.tier} protected={str(decision.protected).lower()} "
        f"decision={decision.decision}: "
        + (
            "protected/frontier-tier work runs the twelve-stage swarm autodev path"
            if swarm
            else "routine-tier work executes directly against the routed model"
        )
    )
    return StrategySelection(
        strategy=strategy,
        model=decision.model,
        reasoning=reasoning,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


def resolve_default_providers(
    allow_offline: bool = False, *, require_authoritative: bool = False
) -> tuple[str, dict[str, ProviderConfig]]:
    """Load config via the shared bootstrap contract, else auto-detect.

    Malformed configuration is a named refusal from the bootstrap contract rather
    than a silently ignored file. The built-in local set remains the last resort
    for interactive use, and the bootstrap contract reports it as a ``default``
    source with an actionable diagnostic.

    A defaulted provider set is refused, exactly as the CLI refuses it, when the
    resolved profile is ``production`` or when the caller requires authoritative
    execution: the ``default_providers_forbidden`` ``BootstrapError`` propagates.
    That decision is taken before any provider detection runs, so a refused
    bootstrap never triggers a port scan.
    """
    import sys

    from verdict.provider_bootstrap import (
        DEFAULT_LOCAL_PROVIDERS,
        BootstrapError,
        BootstrapResult,
        resolve_provider_bootstrap,
    )

    primary_model = DEFAULT_PRIMARY_MODEL
    providers: dict[str, ProviderConfig] = {}

    def report(result: BootstrapResult) -> None:
        for note in result.notes():
            if note.code != "config_file_missing":
                print(f"verdict: {note.describe()}", file=sys.stderr)

    try:
        bootstrap = resolve_provider_bootstrap()
    except BootstrapError as exc:
        if exc.diagnostics[0].code != "no_provider_configuration":
            # Malformed or unreadable configuration must not be swallowed.
            raise
        bootstrap = None

    fallback: BootstrapResult | None = None
    if bootstrap is None:
        # Resolve the built-in local set through the contract before touching the
        # network. Under profile=production, or when the caller requires
        # authoritative execution, this raises default_providers_forbidden.
        fallback = resolve_provider_bootstrap(
            allow_default_providers=True,
            default_providers=DEFAULT_LOCAL_PROVIDERS,
            require_authoritative=require_authoritative,
        )
        primary_model = fallback.primary_model
    else:
        primary_model = bootstrap.primary_model
        providers = bootstrap.provider_configs()
        report(bootstrap)

    if not providers and not allow_offline:
        from verdict.provider_detection import detect_all_providers, generate_verdict_config

        det = detect_all_providers()
        cfg = generate_verdict_config(det)
        if cfg.get("primary_model"):
            primary_model = str(cfg["primary_model"])
        for k, v in (cfg.get("providers") or {}).items():
            if isinstance(v, dict):
                providers[k] = ProviderConfig(
                    base_url=v.get("base_url", ""), api_key_env=v.get("api_key_env")
                )

    if not providers and fallback is not None:
        providers = fallback.provider_configs()
        report(fallback)

    return primary_model, providers


class Gate:
    """The main router client."""

    COMPLEXITY_MAP = COMPLEXITY_MAP

    def __init__(
        self,
        primary_model: str = DEFAULT_PRIMARY_MODEL,
        providers: dict[str, ProviderConfig] | None = None,
        log_path: str = "verdict-decisions.jsonl",
        log_full_task: bool = False,
        discovery_ttl: int = 60,
        profile: str = DEGRADED_PROFILE,
        intelligence_service: IntelligenceService | None = None,
        allow_offline: bool = False,
    ):
        if providers is None:
            resolved_primary, resolved_providers = resolve_default_providers(
                allow_offline=allow_offline
            )
            if primary_model == DEFAULT_PRIMARY_MODEL and resolved_primary:
                primary_model = resolved_primary
            providers = resolved_providers

        self.primary_model = primary_model
        self.providers = providers
        self.log_path = log_path
        self.log_full_task = log_full_task
        self.discovery_ttl = discovery_ttl
        self.allow_offline = allow_offline
        self.intelligence = intelligence_service or IntelligenceService(
            primary_model=primary_model,
            providers=self.providers,
            profile=profile,
            log_path=log_path,
            log_full_task=log_full_task,
            discovery_ttl=discovery_ttl,
            allow_offline=allow_offline,
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

    def route_with_strategy(
        self, task: str, criticality: str = "medium", context: dict[str, object] | None = None
    ) -> tuple[RoutingDecision, StrategySelection]:
        """Route a task and return the decision with its strategy record.

        ``Gate.route()`` keeps its historical ``RoutingDecision`` contract;
        this entry point additionally emits the deterministic
        ``StrategySelection`` record required by issue #265.
        """
        decision = self.route(task, criticality=criticality, context=context)
        return decision, strategy_from_decision(decision)
