from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields
from typing import Any

from verdict.contracts import ContractValidationError
from verdict.swarm_contracts import (
    SwarmTaskBudget,
    SwarmTaskEnvelope,
    capture_envelope_digest,
    validate_envelope_link,
)
from verdict.swarm_governance_base import ALLOWED_CONTROLS as _ALLOWED_CONTROLS
from verdict.swarm_governance_base import SLICE_STATES as _SLICE_STATES
from verdict.swarm_governance_base import (
    SWARM_SPEC_VERSION,
    VALIDATOR_VERSION,
    GovernanceContract,
    canonical_digest,
    canonical_json,
)
from verdict.swarm_governance_base import budget_cap as _budget_cap
from verdict.swarm_governance_base import canonical as _canonical
from verdict.swarm_governance_base import contains_sensitive as _contains_sensitive
from verdict.swarm_governance_base import require_items as _require_items
from verdict.swarm_governance_base import require_text as _require_text


@dataclass(frozen=True)
class GovernanceValidationError:
    code: str
    field_path: str
    reason: str
    swarm_id: str | None = None
    correlation_id: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "code": self.code,
            "field_path": self.field_path,
            "reason": self.reason,
            "swarm_id": self.swarm_id,
            "correlation_id": self.correlation_id,
        }


@dataclass(frozen=True)
class VerificationProfile(GovernanceContract):
    profile_id: str
    version: str
    required_checks: tuple[str, ...] = ()
    required_evidence: tuple[str, ...] = ()
    verification_command: str | None = None
    fail_closed: bool = True

    def __post_init__(self) -> None:
        _require_text(self.profile_id, "profile_id")
        _require_text(self.version, "version")
        _require_items(self.required_checks, "required_checks")
        _require_items(self.required_evidence, "required_evidence")
        if self.verification_command is not None:
            _require_text(self.verification_command, "verification_command")
        if not self.fail_closed:
            raise ContractValidationError("fail_closed must be true")


@dataclass(frozen=True)
class ConflictPolicy(GovernanceContract):
    policy_id: str
    version: str
    strategy: str = "priority_then_digest"
    tie_break: str = "lexical_digest"

    def __post_init__(self) -> None:
        _require_text(self.policy_id, "policy_id")
        _require_text(self.version, "version")
        if self.strategy != "priority_then_digest":
            raise ContractValidationError("strategy must be priority_then_digest")
        if self.tie_break != "lexical_digest":
            raise ContractValidationError("tie_break must be lexical_digest")

    def select(self, candidates: Sequence[tuple[int, Any]]) -> dict[str, Any]:
        if not candidates:
            raise ContractValidationError("conflict candidates must be non-empty")
        normalized = sorted(
            ((priority, canonical_digest(payload), payload) for priority, payload in candidates),
            key=lambda item: (-item[0], item[1]),
        )
        priority, digest, payload = normalized[0]
        decision = {
            "policy_id": self.policy_id,
            "version": self.version,
            "strategy": self.strategy,
            "tie_break": self.tie_break,
            "candidate_digests": sorted(item[1] for item in normalized),
            "selected_digest": digest,
            "selected_priority": priority,
            "selected": _canonical(payload),
        }
        decision["decision_digest"] = canonical_digest(decision)
        return decision


@dataclass(frozen=True)
class SupervisorPolicy(GovernanceContract):
    allowed_controls: tuple[str, ...] = ("pause", "resume", "cancel")
    allowed_actions: tuple[str, ...] | None = None
    cancellation_deadline_ms: int = 30_000
    max_control_retries: int = 0
    terminal_policy: str = "first_valid_terminal_wins"

    def __post_init__(self) -> None:
        controls = self.allowed_controls
        if self.allowed_actions is not None:
            controls = tuple(
                action for action in self.allowed_actions if action in _ALLOWED_CONTROLS
            )
            object.__setattr__(self, "allowed_controls", controls)
        _require_items(controls, "allowed_controls", non_empty=False)
        unknown = set(controls) - _ALLOWED_CONTROLS
        if unknown:
            raise ContractValidationError(
                f"unknown supervisor control(s): {', '.join(sorted(unknown))}"
            )
        if self.cancellation_deadline_ms <= 0:
            raise ContractValidationError("cancellation_deadline_ms must be positive")
        if self.max_control_retries < 0:
            raise ContractValidationError("max_control_retries must be non-negative")
        if self.terminal_policy != "first_valid_terminal_wins":
            raise ContractValidationError("terminal_policy must be first_valid_terminal_wins")


@dataclass(frozen=True)
class SwarmRole(GovernanceContract):
    role_id: str
    name: str = ""
    required_capabilities: tuple[str, ...] = ()
    optional_capabilities: tuple[str, ...] = ()
    forbidden_capabilities: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    resource_refs: tuple[str, ...] = ()
    model_floor: str = ""
    model_allowlist: tuple[str, ...] = ()
    max_parallelism: int = 1
    budget: Any = None
    verification: VerificationProfile | None = None

    def __post_init__(self) -> None:
        _require_text(self.role_id, "role_id")
        _require_text(self.name or self.role_id, "name")
        _require_text(self.model_floor, "model_floor")
        for name in (
            "required_capabilities",
            "optional_capabilities",
            "forbidden_capabilities",
            "allowed_tools",
            "resource_refs",
            "model_allowlist",
        ):
            _require_items(getattr(self, name), name, non_empty=False)
        if set(self.required_capabilities) & set(self.forbidden_capabilities):
            raise ContractValidationError("required and forbidden capabilities cannot overlap")
        if self.max_parallelism <= 0:
            raise ContractValidationError("max_parallelism must be positive")


@dataclass(frozen=True)
class SwarmAgentAssignment(GovernanceContract):
    agent_id: str
    role_id: str
    capabilities: tuple[str, ...]
    allowed_tools: tuple[str, ...] = ()
    resource_refs: tuple[str, ...] = ()
    model: str = ""
    slice_id: str = ""

    def __post_init__(self) -> None:
        _require_text(self.agent_id, "agent_id")
        _require_text(self.role_id, "role_id")
        _require_items(self.capabilities, "capabilities")
        _require_items(self.allowed_tools, "allowed_tools", non_empty=False)
        _require_items(self.resource_refs, "resource_refs", non_empty=False)
        _require_text(self.model, "model")
        _require_text(self.slice_id, "slice_id")


@dataclass(frozen=True)
class SwarmSpec(GovernanceContract):
    schema_version: str = SWARM_SPEC_VERSION
    swarm_id: str = ""
    objective: str = ""
    roles: tuple[SwarmRole, ...] = ()
    agents: tuple[SwarmAgentAssignment, ...] = ()
    context_refs: tuple[str, ...] = ()
    model_constraints: dict[str, Any] = field(default_factory=dict)
    budget: Any = None
    max_concurrency: int = 1
    conflict_policy: ConflictPolicy | None = None
    supervisor: SupervisorPolicy | None = None
    verification: VerificationProfile | None = None
    evidence_scope: str = ""

    def __post_init__(self) -> None:
        if self.schema_version != SWARM_SPEC_VERSION:
            raise ContractValidationError(f"schema_version must be {SWARM_SPEC_VERSION}")
        _require_text(self.swarm_id, "swarm_id")
        _require_text(self.objective, "objective", maximum=4096)
        _require_items(self.context_refs, "context_refs", non_empty=False)
        _require_text(self.evidence_scope, "evidence_scope")
        if not self.roles:
            raise ContractValidationError("roles must be non-empty")
        if not self.agents:
            raise ContractValidationError("agents must be non-empty")
        if len({role.role_id for role in self.roles}) != len(self.roles):
            raise ContractValidationError("role IDs must be unique")
        if len({agent.agent_id for agent in self.agents}) != len(self.agents):
            raise ContractValidationError("agent IDs must be unique")
        if self.max_concurrency <= 0:
            raise ContractValidationError("max_concurrency must be positive")
        if self.conflict_policy is None:
            raise ContractValidationError("conflict_policy is required")
        if self.supervisor is None:
            raise ContractValidationError("supervisor is required")
        if self.verification is None:
            raise ContractValidationError("verification is required")
        if self.budget is None:
            raise ContractValidationError("budget is required")
        if not any(
            _budget_cap(self.budget, key) > 0 for key in ("max_usd", "max_tokens", "max_latency_ms")
        ):
            raise ContractValidationError("at least one budget cap must be positive")
        role_map = {role.role_id: role for role in self.roles}
        if self.max_concurrency > sum(role.max_parallelism for role in self.roles):
            raise ContractValidationError("max_concurrency cannot exceed role parallelism")
        floor = str(self.model_constraints.get("model_floor", "")).strip()
        allowlist = tuple(self.model_constraints.get("allowlist", ()))
        if not floor and not allowlist:
            raise ContractValidationError("model_constraints require model_floor or allowlist")
        for agent in self.agents:
            role = role_map.get(agent.role_id)
            if role is None:
                raise ContractValidationError(f"unknown role_id: {agent.role_id}")
            capabilities = set(agent.capabilities)
            if not set(role.required_capabilities).issubset(capabilities):
                raise ContractValidationError("missing required capabilities")
            if capabilities & set(role.forbidden_capabilities):
                raise ContractValidationError("forbidden capabilities")
            granted = set(role.required_capabilities) | set(role.optional_capabilities)
            if not capabilities.issubset(granted):
                raise ContractValidationError("capabilities exceed role grants")
            if not set(agent.allowed_tools).issubset(role.allowed_tools):
                raise ContractValidationError("tool is not allowed for role")
            if role.resource_refs and not set(agent.resource_refs).issubset(role.resource_refs):
                raise ContractValidationError("resources exceed role grants")
            if role.model_allowlist and agent.model not in role.model_allowlist:
                raise ContractValidationError("model is outside role allowlist")
            if allowlist and agent.model not in allowlist:
                raise ContractValidationError("model is outside swarm allowlist")
            if role.verification is not None and not set(
                self.verification.required_checks
            ).issubset(role.verification.required_checks):
                raise ContractValidationError("role verification must include swarm checks")

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SwarmSpec:
        unknown = set(payload) - {item.name for item in fields(cls)}
        if unknown:
            raise ContractValidationError(f"unknown field(s): {', '.join(sorted(unknown))}")
        required = {item.name for item in fields(cls)}
        missing = required - set(payload)
        if missing:
            raise ContractValidationError(
                f"missing required field(s): {', '.join(sorted(missing))}"
            )
        if _contains_sensitive(canonical_json(payload)):
            raise ContractValidationError("governance input contains prohibited sensitive content")
        coerced = dict(payload)

        def _role(item: Any) -> SwarmRole:
            if isinstance(item, SwarmRole):
                return item
            data = dict(item)
            verification = data.get("verification")
            if isinstance(verification, Mapping):
                data["verification"] = VerificationProfile(**verification)
            for key in (
                "required_capabilities",
                "optional_capabilities",
                "forbidden_capabilities",
                "allowed_tools",
                "resource_refs",
                "model_allowlist",
            ):
                if isinstance(data.get(key), list):
                    data[key] = tuple(data[key])
            return SwarmRole(**data)

        coerced["roles"] = tuple(_role(item) for item in payload["roles"])
        coerced["agents"] = tuple(
            item if isinstance(item, SwarmAgentAssignment) else SwarmAgentAssignment(**item)
            for item in payload["agents"]
        )
        coerced["context_refs"] = tuple(payload["context_refs"])
        for name, contract_type in (
            ("conflict_policy", ConflictPolicy),
            ("supervisor", SupervisorPolicy),
            ("verification", VerificationProfile),
        ):
            if not isinstance(payload[name], contract_type):
                coerced[name] = contract_type(**payload[name])
        if isinstance(payload["budget"], Mapping):
            coerced["budget"] = SwarmTaskBudget(**payload["budget"])
        return cls(**coerced)

    def validation_result_id(self, errors: Sequence[GovernanceValidationError] = ()) -> str:
        return canonical_digest(
            {
                "swarm_id": self.swarm_id,
                "validator_version": VALIDATOR_VERSION,
                "errors": [item.to_dict() for item in errors],
            }
        )


@dataclass(frozen=True)
class SwarmSlice(GovernanceContract):
    slice_id: str
    swarm_id: str
    role_id: str
    agent_id: str
    envelope: SwarmTaskEnvelope
    verification: VerificationProfile
    evidence_root_id: str
    envelope_digest: str = ""
    swarm_spec_digest: str = ""
    spec: SwarmSpec | None = field(default=None, repr=False, compare=False)
    state: str = "pending"

    def __post_init__(self) -> None:
        if not self.envelope_digest:
            object.__setattr__(self, "envelope_digest", capture_envelope_digest(self.envelope))
        for value, name in (
            (self.slice_id, "slice_id"),
            (self.swarm_id, "swarm_id"),
            (self.role_id, "role_id"),
            (self.agent_id, "agent_id"),
            (self.envelope_digest, "envelope_digest"),
            (self.evidence_root_id, "evidence_root_id"),
        ):
            _require_text(value, name)
        if self.state not in _SLICE_STATES:
            raise ContractValidationError("invalid slice state")
        validate_envelope_link(self.envelope, self.envelope_digest)
        if self.swarm_spec_digest:
            _require_text(self.swarm_spec_digest, "swarm_spec_digest")
        if self.spec is not None and (
            self.swarm_id != self.spec.swarm_id or self.swarm_spec_digest != self.spec.digest()
        ):
            raise ContractValidationError("swarm_id must match spec")

    @staticmethod
    def digest_envelope(envelope: SwarmTaskEnvelope) -> str:
        return capture_envelope_digest(envelope)

    @classmethod
    def from_spec(
        cls,
        *,
        spec: SwarmSpec,
        assignment_id: str,
        envelope: SwarmTaskEnvelope,
        verification: VerificationProfile,
        evidence_root_id: str,
        slice_id: str | None = None,
        swarm_spec_digest: str | None = None,
    ) -> SwarmSlice:
        if swarm_spec_digest is not None and swarm_spec_digest != spec.digest():
            raise ContractValidationError("swarm_id must match spec digest")
        assignment = next((item for item in spec.agents if item.agent_id == assignment_id), None)
        if assignment is None:
            raise ContractValidationError("unknown assignment_id")
        role = next(item for item in spec.roles if item.role_id == assignment.role_id)
        if not set(envelope.required_capabilities).issubset(assignment.capabilities):
            raise ContractValidationError("envelope capabilities exceed assignment")
        approved_resources = set(role.resource_refs) or set(assignment.resource_refs)
        if not set(envelope.allowed_paths).issubset(approved_resources):
            raise ContractValidationError("cannot broaden allowed_paths")
        if envelope.max_parallelism > min(spec.max_concurrency, role.max_parallelism):
            raise ContractValidationError("cannot broaden max_parallelism")
        for key in ("max_usd", "max_tokens", "max_latency_ms"):
            spec_cap = _budget_cap(spec.budget, key)
            role_cap = _budget_cap(role.budget, key)
            envelope_cap = _budget_cap(envelope.budget, key)
            applicable = [cap for cap in (spec_cap, role_cap) if cap > 0]
            if applicable and envelope_cap > min(applicable):
                raise ContractValidationError(f"cannot broaden budget.{key}")
        return cls(
            slice_id=slice_id or envelope.task_id,
            swarm_id=spec.swarm_id,
            role_id=role.role_id,
            agent_id=assignment.agent_id,
            envelope=envelope,
            envelope_digest=capture_envelope_digest(envelope),
            verification=verification,
            evidence_root_id=evidence_root_id,
            swarm_spec_digest=spec.digest(),
            spec=spec,
        )

    def effective_bounds(self, spec: SwarmSpec, dispatcher_concurrency: int) -> dict[str, float]:
        role = next(item for item in spec.roles if item.role_id == self.role_id)
        concurrency = min(
            spec.max_concurrency,
            role.max_parallelism,
            self.envelope.max_parallelism,
            dispatcher_concurrency,
        )
        result: dict[str, float] = {
            "max_concurrency": float(concurrency),
            "timeout_ms": float(self.envelope.timeout_ms),
        }
        for key in ("max_usd", "max_tokens", "max_latency_ms"):
            caps = [
                cap
                for cap in (
                    _budget_cap(spec.budget, key),
                    _budget_cap(role.budget, key),
                    _budget_cap(self.envelope.budget, key),
                )
                if cap > 0
            ]
            result[key] = min(caps) if caps else 0.0
        return result


__all__ = [
    "SWARM_SPEC_VERSION",
    "VALIDATOR_VERSION",
    "ConflictPolicy",
    "GovernanceValidationError",
    "SupervisorPolicy",
    "SwarmAgentAssignment",
    "SwarmRole",
    "SwarmSlice",
    "SwarmSpec",
    "VerificationProfile",
    "canonical_digest",
    "canonical_json",
]
