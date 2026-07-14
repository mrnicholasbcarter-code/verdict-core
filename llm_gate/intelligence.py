import time
from dataclasses import dataclass
from typing import Any

from llm_gate.discovery import fetch_models
from llm_gate.escalation import scan
from llm_gate.logger import log_decision
from llm_gate.models import ProviderConfig, RoutingDecision
from llm_gate.router import select_best_model

DEFAULT_PROFILE = "development"
DEGRADED_PROFILE = "degraded"
DEFAULT_TIMEOUT_MS = 1000


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


class IntelligenceService:
    def __init__(
        self,
        primary_model: str,
        providers: dict[str, ProviderConfig],
        profile: str,
        log_path: str,
        log_full_task: bool,
        discovery_ttl: int,
        ruflo_command: str = "ruflo",
        ruvector_command: str = "ruvector",
        timeout_ms: int = 1000,
        frontier_allowlist: tuple[str, ...] | None = None,
        allow_client_model_override: bool = False,
    ):
        self.primary_model = primary_model
        self.providers = providers
        self.profile = profile
        self.log_path = log_path
        self.log_full_task = log_full_task
        self.discovery_ttl = discovery_ttl
        self.ruflo_command = ruflo_command
        self.ruvector_command = ruvector_command
        self.timeout_ms = timeout_ms
        self.frontier_allowlist = frontier_allowlist
        self.allow_client_model_override = allow_client_model_override
        self.managed_backend_status = self._probe_managed_backend()
        self._policy_version = "policy-2026-07-13.1"

    def _redact(self, text: str) -> str:
        import re

        # Basic redaction before CLI execution (sk-...)
        return re.sub(r"sk-[a-zA-Z0-9]{10,}", "[REDACTED]", text)

    def _probe_managed_backend(self) -> str:
        try:
            import subprocess

            result = subprocess.run(
                [self.ruflo_command, "guidance", "gates", "--version"],
                capture_output=True,
                text=True,
                timeout=0.5,
            )
            return "healthy" if result.returncode == 0 else "unavailable"
        except Exception:
            return "unavailable"

    def readiness(self) -> ReadinessReport:
        status = (
            "ready"
            if self.profile != "production" or self.managed_backend_status != "unavailable"
            else "not_ready"
        )
        degraded = self.managed_backend_status == "unavailable"
        return ReadinessReport(
            status=status,
            production_ready=(not degraded),
            profile=self.profile,
            managed_backend_status=self.managed_backend_status,
            degraded_mode=degraded,
            policy_version=self._policy_version,
            reason="ready" if not degraded else "managed intelligence unavailable",
            adapter_versions={},
        )

    def route(
        self, task: str, criticality: str = "medium", context: dict[str, Any] | None = None
    ) -> RoutingDecision:
        start_t = time.time()

        # Hard deterministic floor logic here.
        redacted_task = self._redact(task)
        # Attempt an async call or subprocess with timeout to Ruflo
        try:
            import subprocess

            subprocess.run(
                [self.ruflo_command, "hooks", "model-route", "--context", redacted_task],
                capture_output=True,
                timeout=0.2,
            )
        except Exception:
            pass

        # Fallback to strict heuristic scan
        eff_tier, heuristic_reason = scan(task)

        # Convert criticality string to required tier max
        tier_map = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        req_tier = tier_map.get(criticality.lower(), 2)

        esc_reason: str = ""
        escalated = False

        if eff_tier is not None and eff_tier < req_tier:
            req_tier = eff_tier
            esc_reason = heuristic_reason or ""
            escalated = True

        final_tier = eff_tier if eff_tier is not None and eff_tier < req_tier else req_tier

        candidates = []
        for name, cfg in self.providers.items():
            candidates.extend(fetch_models(name, cfg, self.discovery_ttl))

        best_model, _ = select_best_model(candidates, final_tier, self.providers)

        if final_tier == 0 or not best_model:
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
                degraded_mode=(self.managed_backend_status == "unavailable"),
                managed_backend_status=self.managed_backend_status,
                protected=(final_tier == 0),
                decision="fallback" if best_model is None else "selected",
                transport_outcome="not_sent",
                quality_outcome="unknown",
            )
        else:
            dec = RoutingDecision(
                model=best_model.id,
                provider=best_model.provider,
                tier=best_model.capability_tier,
                reason=f"tier {final_tier} routed",
                escalated=escalated,
                escalation_reason=esc_reason or None,
                policy_version=self._policy_version,
                degraded_mode=(self.managed_backend_status == "unavailable"),
                managed_backend_status=self.managed_backend_status,
                protected=(final_tier == 0),
                decision="selected",
                transport_outcome="not_sent",
                quality_outcome="unknown",
            )

        elapsed = (time.time() - start_t) * 1000
        dec = RoutingDecision(
            **{**dec.__dict__, "latency_ms": elapsed, "logged": bool(self.log_path)}
        )
        if self.log_path:
            log_decision(self.log_path, task, req_tier, dec, self.log_full_task)

        return dec
