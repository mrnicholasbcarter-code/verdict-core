import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

from verdict.classifier import classify
from verdict.discovery import fetch_models
from verdict.eligibility import EligibilityGate
from verdict.escalation import scan
from verdict.logger import log_decision
from verdict.models import ModelInfo, ProviderConfig, RoutingDecision
from verdict.planner import StructuredPlanner
from verdict.router import select_best_model

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
        ruflo_command: str = "ruflo",
        ruvector_command: str = "ruvector",
        timeout_ms: int = 1000,
        frontier_allowlist: tuple[str, ...] | None = None,
        allow_client_model_override: bool = False,
        planner: StructuredPlanner | None = None,
        eligibility_gate: EligibilityGate | None = None,
        allow_offline: bool = False,
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
        self.planner = planner or StructuredPlanner()
        # Issue #57: single-source-of-truth eligibility gate consulted before
        # any ranking.  When None, routing falls back to catalog truth only.
        self.eligibility_gate = eligibility_gate
        # Issue #265 (V1-002): allow_offline=True keeps every decision readable
        # without network or subprocess probes — static catalog truth only.
        self.allow_offline = allow_offline
        self.managed_backend_status = "offline" if allow_offline else self._probe_managed_backend()
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
        def _get_version(cmd: str) -> str:
            try:
                result = subprocess.run(
                    [cmd, "--version"], capture_output=True, text=True, timeout=0.5
                )
                if result.returncode == 0:
                    # Take first line of output
                    return result.stdout.splitlines()[0].strip() if result.stdout else "unknown"
                else:
                    return "unknown"
            except Exception:
                return "unknown"

        ruflo_version = _get_version(self.ruflo_command)
        ruvector_version = _get_version(self.ruvector_command)

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
            adapter_versions={"ruflo": ruflo_version, "ruvector": ruvector_version},
        )

    async def route(
        self,
        task: str | dict[str, Any],
        criticality: str = "medium",
        context: dict[str, Any] | None = None,
    ) -> RoutingDecision:
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

        # Hard deterministic floor logic here.
        if not self.allow_offline:
            redacted_task = self._redact(task_str)
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

        best_model, _ = select_best_model(candidates, final_tier, self.providers)

        eligibility_record: dict[str, Any] = {}
        if eligibility is not None:
            eligibility_record = eligibility.to_dict()
            excluded = [r for r in eligibility.records if not r.admitted]
            if excluded and final_tier == 0:
                # Protected work: some candidates were excluded by live truth.
                eligibility_record["protected_fail_closed"] = True

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
                candidate_states=eligibility_record.get("records", []),
                safety_flags=(
                    ["eligibility_exclusions_applied"]
                    if eligibility_record.get("protected_fail_closed")
                    else []
                ),
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
                candidate_states=eligibility_record.get("records", []),
                safety_flags=(
                    ["eligibility_exclusions_applied"]
                    if eligibility_record.get("protected_fail_closed")
                    else []
                ),
            )

        elapsed = (time.time() - start_t) * 1000
        dec = RoutingDecision(
            **{**dec.__dict__, "latency_ms": elapsed, "logged": bool(self.log_path)}
        )
        if self.log_path:
            log_decision(self.log_path, task_str, req_tier, dec, self.log_full_task)

        return dec

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
