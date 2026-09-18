import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from verdict.admit_prove_confirm import gate_admit_prove_confirm
from verdict.chooser import ChooserError, apply_best_of_admitted
from verdict.classifier import classify
from verdict.discovery import fetch_models
from verdict.eligibility import EligibilityGate
from verdict.escalation import scan
from verdict.free_tier_admit import (
    FAIL_CLOSED_REASON,
    NO_ELIGIBLE_TARGET,
    FreeTierAdmitReceipt,
    LiveAdmitError,
    NamedDrop,
    OmniRouteAdmitSnapshot,
    admit_free_tier_active,
    build_cheap_path_context_pack,
    execute_offload_chat,
    load_omniroute_admit_snapshot,
    normalize_omniroute_origin,
    omniroute_endpoint_from_env,
)
from verdict.logger import log_decision
from verdict.model_passports import ModelPassport
from verdict.models import ModelInfo, ProviderConfig, RoutingDecision
from verdict.planner import StructuredPlanner
from verdict.probes import ProbeTransport, openai_probe_transport
from verdict.router import select_best_eligible_model, select_best_model

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

        if final_tier > 0 and not self.allow_offline:
            offload = self._offload_free_tier(task_str, final_tier, escalated, esc_reason)
            if offload is not None:
                elapsed = (time.time() - start_t) * 1000
                dec = RoutingDecision(
                    **{**offload.__dict__, "latency_ms": elapsed, "logged": bool(self.log_path)}
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

        best_model, _ = (
            select_best_eligible_model(eligibility, final_tier, self.providers)
            if eligibility is not None
            else select_best_model(candidates, final_tier, self.providers)
        )

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

    def _offload_free_tier(
        self, task: str, final_tier: int, escalated: bool, esc_reason: str
    ) -> RoutingDecision | None:
        """Admit free-tier ∩ active-provider identities for offloadable work.

        Returns ``None`` when OmniRoute admit surfaces are not configured *or*
        cannot be loaded, so the historical catalog/fallback path still runs for
        offline unit tests and dead endpoints. Fail-closed empty-intersection
        applies only after a real (live or fixture) snapshot was consulted.
        """
        snapshot, endpoint, live, _fetch_error = self._load_admit_snapshot()
        if snapshot is None:
            # Not configured, or live surfaces unavailable: do not starve ranking.
            return None
        receipt = admit_free_tier_active(snapshot)
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
        )
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
            receipt = FreeTierAdmitReceipt(
                admitted=remaining,
                exclusions=receipt.exclusions + extra,
                chosen=chosen,
                empty_intersection=chosen is None,
                active_providers=receipt.active_providers,
                free_tier_providers=receipt.free_tier_providers,
                pack_digest=receipt.pack_digest,
                omissions=receipt.omissions,
                passport=receipt.passport,
                confirm=receipt.confirm,
                selected_because=receipt.selected_because,
            )
            eligibility = gated
        if receipt.admitted:
            try:
                receipt = apply_best_of_admitted(
                    receipt, passports=self.passports, now=self.admit_now
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
        )

    def _load_admit_snapshot(
        self,
    ) -> tuple[OmniRouteAdmitSnapshot | None, tuple[str, str | None] | None, bool, str | None]:
        if self.admit_snapshot is not None:
            return (self.admit_snapshot, omniroute_endpoint_from_env(self.providers), False, None)
        endpoint = omniroute_endpoint_from_env(self.providers)
        if endpoint is None or not str(endpoint[0] or "").strip():
            return None, None, False, None
        try:
            snapshot = load_omniroute_admit_snapshot(endpoint[0], endpoint[1])
        except LiveAdmitError as exc:
            # Dead/misconfigured OmniRoute must not fail-closed the offline path.
            return None, None, False, str(exc)
        return snapshot, endpoint, True, None

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
    ) -> RoutingDecision:
        eligibility_record = eligibility.to_dict() if eligibility is not None else {}
        if receipt.empty_intersection or not receipt.chosen:
            return RoutingDecision(
                model=NO_ELIGIBLE_TARGET,
                provider="none",
                tier=final_tier,
                reason=FAIL_CLOSED_REASON,
                escalated=escalated,
                escalation_reason=esc_reason or None,
                policy_version=self._policy_version,
                degraded_mode=(self.managed_backend_status == "unavailable"),
                managed_backend_status=self.managed_backend_status,
                protected=False,
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
        # Cheap path: gather real provenance units, compile under budget, then
        # execute. Digest + named omissions land on the admit receipt.
        context_pack = build_cheap_path_context_pack(
            task,
            candidate_id=chosen,
            workspace_root=self.workspace_root,
            workspace_roots=self.context_roots,
            mcp_root=self.mcp_root,
        )
        receipt = replace(
            receipt, pack_digest=context_pack.pack_digest, omissions=context_pack.omissions
        )
        packed_task = context_pack.compiled_prompt
        should_execute = self.execute_offload
        if should_execute is None:
            should_execute = live and self.offload_executor is None
        transport_outcome = "not_sent"
        preview: str | None = None
        if should_execute or self.offload_executor is not None:
            if self.offload_executor is not None:
                transport_outcome, preview = self.offload_executor(chosen, packed_task)
            elif endpoint is not None:
                transport_outcome, preview = execute_offload_chat(
                    endpoint[0], chosen, packed_task, api_key=endpoint[1]
                )
        chooser_owned = bool(receipt.selected_because) and receipt.chosen is not None
        safety_flags = ["free_tier_active_admit", "prove_confirm_admit", "cheap_path_context_pack"]
        if chooser_owned:
            safety_flags.append("chooser_ranked_admitted")
        return RoutingDecision(
            model=chosen,
            provider=provider,
            tier=final_tier,
            reason=f"free∩active ∩ fresh-passport ∩ confirmed admitted {chosen}",
            alternatives=list(receipt.admitted[:8]),
            escalated=escalated,
            escalation_reason=esc_reason or None,
            policy_version=self._policy_version,
            degraded_mode=(self.managed_backend_status == "unavailable") or chooser_owned,
            managed_backend_status=self.managed_backend_status,
            protected=False,
            decision="selected",
            transport_outcome=transport_outcome,
            quality_outcome="unknown",
            candidate_states=eligibility_record.get("records", []),
            safety_flags=safety_flags,
            admit_receipt=receipt.to_dict(),
            execute_preview=preview,
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
