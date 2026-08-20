"""Strict version-1 shared JSON contracts for planning and routing."""

from __future__ import annotations

import json
import math
import re
import time
from dataclasses import MISSING, Field, dataclass, field, fields
from types import UnionType
from typing import Any, ClassVar, TypeVar, cast, get_args, get_origin, get_type_hints

from verdict.security import fingerprint_text, redact_text


class ContractValidationError(ValueError):
    """Raised for unknown fields, missing fields, or secret-bearing payloads."""


_SECRET_NAMES = {"api_key", "apikey", "authorization", "password", "secret", "token"}

# These are the safety-sensitive values that v1 intentionally freezes.  Task
# types, planner modes, provider identifiers, and metadata remain open strings
# so adding a new workflow integration does not require a contract version bump.
_TASK_EFFORTS = frozenset({"unknown", "low", "medium", "high"})
_TASK_REASONING_LEVELS = frozenset({"unknown", "low", "medium", "high"})
_TASK_TYPES = frozenset({"code", "review", "test", "doc", "analysis", "debug", "ops", "research"})
_PLANNER_MODES = frozenset({"auto", "sequential", "parallel", "hybrid"})
_PROVIDER_KINDS = frozenset({"anthropic", "openai", "google", "aws", "azure", "local", "omniroute"})
_VERIFICATION_CHECK_TYPES = frozenset(
    {"build", "test", "lint", "typecheck", "security", "policy", "custom"}
)
_VERIFICATION_STATUSES = frozenset({"passed", "failed", "skipped", "unknown"})
_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"


def _reject_secrets(value: Any, path: str = "") -> None:
    """Recursively reject any string that contains credential-like substrings."""
    if isinstance(value, str):
        lowered = value.lower()
        for secret in _SECRET_NAMES:
            if secret in lowered:
                raise ContractValidationError(f"secret-bearing field rejected: {path}")
    elif isinstance(value, dict):
        for k, v in value.items():
            _reject_secrets(v, f"{path}.{k}" if path else k)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _reject_secrets(v, f"{path}[{i}]")


def _coerce_field_value(field_type: Any, value: Any, field_name: str) -> Any:
    """Coerce a JSON-decoded value to the annotated Python type for Contract fields."""
    origin = get_origin(field_type)
    if origin is None:
        try:
            field_type = getattr(field_type, "__origin__", field_type)
            origin = get_origin(field_type)
        except AttributeError:
            origin = None

    if origin in (list, tuple, set, frozenset) or (
        isinstance(origin, type) and issubclass(origin, (list, tuple, set, frozenset))
    ):
        if not isinstance(value, list):
            raise ContractValidationError(f"{field_name} must be an array")
        args = get_args(field_type)
        item_type = args[0] if args else Any
        return [
            _coerce_field_value(item_type, v, f"{field_name}[{i}]") for i, v in enumerate(value)
        ]

    if origin is dict or (isinstance(origin, type) and issubclass(origin, dict)):
        if not isinstance(value, dict):
            raise ContractValidationError(f"{field_name} must be an object")
        args = get_args(field_type)
        key_type, val_type = (args[0], args[1]) if len(args) == 2 else (str, Any)
        return {
            _coerce_field_value(key_type, k, f"{field_name}.{k}"): _coerce_field_value(
                val_type, v, f"{field_name}.{k}"
            )
            for k, v in value.items()
        }

    if origin is UnionType or (
        hasattr(field_type, "__origin__") and field_type.__origin__ is Union
    ):
        args = get_args(field_type)
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return _coerce_field_value(non_none[0], value, field_name)
        for candidate in non_none:
            try:
                return _coerce_field_value(candidate, value, field_name)
            except ContractValidationError:
                continue
        raise ContractValidationError(f"{field_name} has invalid type")

    # Check if it's a Contract subclass
    try:
        if isinstance(field_type, type) and issubclass(field_type, Contract):
            if isinstance(value, dict):
                return field_type.from_dict(value)
            if isinstance(value, field_type):
                return value
            raise ContractValidationError(f"{field_name} must be an object")
    except TypeError:
        pass

    if field_type is Any:
        return value

    if isinstance(value, field_type):
        return value

    if field_type is int and isinstance(value, bool):
        raise ContractValidationError(f"{field_name} must be an integer, not boolean")

    if field_type is int and isinstance(value, float) and value.is_integer():
        return int(value)

    if field_type is float and isinstance(value, int):
        return float(value)

    if field_type is str:
        return str(value)

    if field_type is bool:
        if isinstance(value, str):
            if value.lower() in ("true", "1", "yes"):
                return True
            if value.lower() in ("false", "0", "no"):
                return False
        return bool(value)

    raise ContractValidationError(f"{field_name} has invalid type")


def _validate_field_value(field_type: Any, value: Any, field_name: str) -> None:
    """Validate a field value against its type annotation."""
    origin = get_origin(field_type)
    if origin is None:
        try:
            field_type = getattr(field_type, "__origin__", field_type)
            origin = get_origin(field_type)
        except AttributeError:
            origin = None

    if origin in (list, tuple, set, frozenset) or (
        isinstance(origin, type) and issubclass(origin, (list, tuple, set, frozenset))
    ):
        if not isinstance(value, list):
            raise ContractValidationError(f"{field_name} must be an array")
        args = get_args(field_type)
        item_type = args[0] if args else Any
        for i, v in enumerate(value):
            _validate_field_value(item_type, v, f"{field_name}[{i}]")
        return

    if origin is dict or (isinstance(origin, type) and issubclass(origin, dict)):
        if not isinstance(value, dict):
            raise ContractValidationError(f"{field_name} must be an object")
        args = get_args(field_type)
        key_type, val_type = (args[0], args[1]) if len(args) == 2 else (str, Any)
        for k, v in value.items():
            _validate_field_value(key_type, k, f"{field_name}.{k}")
            _validate_field_value(val_type, v, f"{field_name}.{k}")
        return

    if origin is UnionType or (
        hasattr(field_type, "__origin__") and field_type.__origin__ is Union
    ):
        args = get_args(field_type)
        non_none = [a for a in args if a is not type(None)]
        if value is None:
            return
        for candidate in non_none:
            try:
                _validate_field_value(candidate, value, field_name)
                return
            except ContractValidationError:
                continue
        raise ContractValidationError(f"{field_name} has invalid type")

    # Check if it's a Contract subclass
    try:
        if isinstance(field_type, type) and issubclass(field_type, Contract):
            if isinstance(value, dict):
                # Validate nested Contract dicts via their own from_dict
                field_type.from_dict(value)
            elif isinstance(value, field_type):
                return
            else:
                raise ContractValidationError(f"{field_name} must be an object")
            return
    except TypeError:
        pass

    if field_type is Any:
        return

    if isinstance(value, field_type):
        if field_type is int and value < 0:
            raise ContractValidationError(f"{field_name} must be a non-negative number")
        if field_type is float and not math.isfinite(value):
            raise ContractValidationError(f"{field_name} must be finite")
        return

    if field_type is int and isinstance(value, bool):
        raise ContractValidationError(f"{field_name} must be an integer, not boolean")

    if field_type is int and isinstance(value, float) and value.is_integer():
        return

    if field_type is float and isinstance(value, int):
        return

    if field_type is str and isinstance(value, str):
        return

    if field_type is bool and isinstance(value, bool):
        return

    raise ContractValidationError(f"{field_name} has invalid type")


class Contract:
    """Base class for all strict v1 contracts."""

    @classmethod
    def _contract_name(cls) -> str:
        return (
            cls.__name__.lower()
            .replace("result", "")
            .replace("link", "")
            .replace("plan", "")
            .replace("spec", "")
        )

    @classmethod
    def _contract_version(cls) -> str:
        return "1"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Contract":
        if not isinstance(data, dict):
            raise ContractValidationError("contract must be a JSON object")

        contract_name = cls._contract_name()
        expected_name = data.get("contract")
        if expected_name and expected_name != contract_name:
            raise ContractValidationError(f"expected contract {contract_name}, got {expected_name}")

        known_fields = {f.name for f in fields(cls) if f.init}
        unknown = set(data.keys()) - known_fields - {"contract", "schema_version"}
        if unknown:
            raise ContractValidationError(f"unknown field(s): {', '.join(sorted(unknown))}")

        missing = []
        for f in fields(cls):
            if f.init and f.name not in data:
                if f.default is MISSING and f.default_factory is MISSING:
                    missing.append(f.name)
        if missing:
            raise ContractValidationError(f"missing required field(s): {', '.join(missing)}")

        coerced = {}
        for f in fields(cls):
            if not f.init:
                continue
            raw = data.get(
                f.name,
                f.default_factory()
                if f.default_factory is not MISSING
                else f.default
                if f.default is not MISSING
                else MISSING,
            )
            if raw is MISSING:
                continue
            if f.name == "schema_version":
                coerced[f.name] = raw
                continue
            try:
                _validate_field_value(f.type, raw, f.name)
                coerced[f.name] = _coerce_field_value(f.type, raw, f.name)
            except ContractValidationError:
                raise
            except Exception as exc:
                raise ContractValidationError(f"{f.name} has invalid type") from exc

        instance = cls(**coerced)

        if hasattr(instance, "__post_init__"):
            instance.__post_init__()

        return instance

    @classmethod
    def from_json(cls, text: str) -> "Contract":
        return cls.from_dict(json.loads(text))

    def to_dict(self) -> dict[str, Any]:
        def convert(value: Any) -> Any:
            if isinstance(value, Contract):
                return value.to_dict()
            if isinstance(value, list):
                return [convert(v) for v in value]
            if isinstance(value, dict):
                return {k: convert(v) for k, v in value.items()}
            if isinstance(value, (str, int, float, bool)) or value is None:
                return value
            return str(value)

        result = {
            "contract": self._contract_name(),
            "schema_version": getattr(self, "schema_version", "1"),
        }
        for f in fields(self):
            if not f.init:
                continue
            value = getattr(self, f.name, None)
            if value is not None:
                if f.name == "schema_version":
                    result[f.name] = value
                else:
                    result[f.name] = convert(value)
        return result

    def to_json(self, **kwargs: Any) -> str:
        return json.dumps(self.to_dict(), **kwargs)


@dataclass(frozen=True)
class TaskSpec(Contract):
    """Version-1 task specification intake contract."""

    objective: str = ""
    task_type: str = ""
    effort: str = "unknown"
    reasoning_level: str = "unknown"
    constraints: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: str = "1"

    def __post_init__(self) -> None:
        if not self.objective:
            raise ContractValidationError("objective must not be empty")
        if not self.task_type:
            raise ContractValidationError("task_type must not be empty")
        if self.task_type not in _TASK_TYPES:
            raise ContractValidationError(f"invalid task_type: {self.task_type}")
        if self.effort not in _TASK_EFFORTS:
            raise ContractValidationError(f"invalid effort: {self.effort}")
        if self.reasoning_level not in _TASK_REASONING_LEVELS:
            raise ContractValidationError(f"invalid reasoning_level: {self.reasoning_level}")


@dataclass(frozen=True)
class SourceState(Contract):
    """Git + workspace state snapshot for a change."""

    repo: str = ""
    base_sha: str = ""
    head_sha: str = ""
    is_dirty: bool = False
    changed_files: list[str] = field(default_factory=list)
    schema_version: str = "1"

    def __post_init__(self) -> None:
        if not self.repo:
            raise ContractValidationError("repo must not be empty")
        if not self.base_sha:
            raise ContractValidationError("base_sha must not be empty")
        if not self.head_sha:
            raise ContractValidationError("head_sha must not be empty")
        if self.base_sha == self.head_sha and not self.is_dirty:
            raise ContractValidationError(
                "head_sha must differ from base_sha or is_dirty must be true"
            )
        for sha in (self.base_sha, self.head_sha):
            if not re.match(r"^[0-9a-f]{40}$", sha):
                raise ContractValidationError(f"invalid SHA: {sha}")


@dataclass(frozen=True)
class DiffSummary(Contract):
    """Structured diff summary for policy evaluation."""

    lines_added: int = 0
    lines_removed: int = 0
    files_changed: int = 0
    hunks: int = 0
    is_binary: bool = False
    schema_version: str = "1"

    def __post_init__(self) -> None:
        for v in (self.lines_added, self.lines_removed, self.files_changed, self.hunks):
            if v < 0:
                raise ContractValidationError("diff counters must be non-negative")


@dataclass(frozen=True)
class TrustedChangeMetrics(Contract):
    """Verifiable metrics for a proposed change."""

    test_count: int = 0
    test_delta: int = 0
    coverage_pct: float = 0.0
    coverage_delta: float = 0.0
    complexity_delta: float = 0.0
    security_findings: int = 0
    performance_ms: int | None = None
    schema_version: str = "1"

    def __post_init__(self) -> None:
        if self.test_count < 0:
            raise ContractValidationError("test_count must be non-negative")
        if not (0.0 <= self.coverage_pct <= 100.0):
            raise ContractValidationError("coverage_pct must be 0-100")
        if self.performance_ms is not None and self.performance_ms < 0:
            raise ContractValidationError("performance_ms must be non-negative")


@dataclass(frozen=True)
class AcceptanceDecision(Contract):
    """Policy acceptance decision with provenance."""

    accepted: bool = False
    reason: str = ""
    policy_version: str = ""
    evaluated_at: str = ""
    schema_version: str = "1"

    def __post_init__(self) -> None:
        if not self.reason:
            raise ContractValidationError("reason must not be empty")
        if not self.policy_version:
            raise ContractValidationError("policy_version must not be empty")
        if not self.evaluated_at:
            raise ContractValidationError("evaluated_at must not be empty")


@dataclass(frozen=True)
class RouteRecommendation(Contract):
    """Routing recommendation from the planner."""

    mode: str = ""
    model: str = ""
    rationale: str = ""
    estimated_cost_usd: float = 0.0
    estimated_latency_ms: int = 0
    schema_version: str = "1"

    def __post_init__(self) -> None:
        if not self.mode:
            raise ContractValidationError("mode must not be empty")
        if not self.model:
            raise ContractValidationError("model must not be empty")
        if self.estimated_cost_usd < 0:
            raise ContractValidationError("estimated_cost_usd must be non-negative")
        if self.estimated_latency_ms < 0:
            raise ContractValidationError("estimated_latency_ms must be non-negative")


@dataclass(frozen=True)
class RegressionObservation(Contract):
    """Observed regression signal."""

    metric: str = ""
    baseline: float = 0.0
    current: float = 0.0
    threshold: float = 0.0
    schema_version: str = "1"

    def __post_init__(self) -> None:
        if not self.metric:
            raise ContractValidationError("metric must not be empty")
        if self.threshold < 0:
            raise ContractValidationError("threshold must be non-negative")


@dataclass(frozen=True)
class VerificationPlan(Contract):
    """Ordered verification steps with on-failure policy."""

    steps: list[dict[str, Any]] = field(default_factory=list)
    on_failure: str = "deny"
    schema_version: str = "1"

    def __post_init__(self) -> None:
        if not self.steps:
            raise ContractValidationError("verification requires 'steps'")
        if self.on_failure not in {"deny", "replan_or_deny"}:
            raise ContractValidationError(
                "verification.on_failure must be 'deny' or 'replan_or_deny'"
            )
        for i, step in enumerate(self.steps):
            if not isinstance(step, dict):
                raise ContractValidationError(f"step[{i}] must be an object")
            action = step.get("action", "")
            if action not in _VERIFICATION_CHECK_TYPES:
                raise ContractValidationError(f"step[{i}].action is unsafe or unknown")
            if "parallel" in step and not isinstance(step["parallel"], bool):
                raise ContractValidationError(f"step[{i}].parallel must be boolean")
            if "required" in step and not isinstance(step["required"], bool):
                raise ContractValidationError(f"step[{i}].required must be boolean")


@dataclass(frozen=True)
class ExecutionEnvelope(Contract):
    """Universal immutable execution envelope for cross-runtime handoff."""

    task_spec: TaskSpec = field(default_factory=TaskSpec)
    source_state: SourceState | None = None
    diff_summary: DiffSummary | None = None
    metrics: TrustedChangeMetrics | None = None
    acceptance: AcceptanceDecision | None = None
    route_recommendation: RouteRecommendation | None = None
    regression_observation: RegressionObservation | None = None
    verification_requirements: VerificationPlan | None = None
    policy_digest: str = ""
    allowed_capabilities: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    budget_usd: float = 0.0
    budget_ms: int = 0
    schema_version: str = "1"

    def __post_init__(self) -> None:
        if not self.policy_digest:
            raise ContractValidationError("policy_digest must be a non-empty string")
        if self.budget_usd < 0:
            raise ContractValidationError("budget_usd must be non-negative")
        if self.budget_ms < 0:
            raise ContractValidationError("budget_ms must be non-negative")
        for name in self.allowed_capabilities:
            if not name:
                raise ContractValidationError("allowed_capabilities must contain non-empty strings")
        for eid in self.evidence_ids:
            if not eid:
                raise ContractValidationError("evidence_ids must contain non-empty strings")


@dataclass(frozen=True)
class VerificationResult(Contract):
    """A single verification check with the provenance needed to re-run it."""

    check_name: str = ""
    check_type: str = ""
    status: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    artifact_digests: list[str] = field(default_factory=list)
    duration_ms: int | None = None
    command: str = ""
    runtime: str = ""
    provenance: str = ""
    policy_requirement: str = ""
    raw_output: str = ""
    schema_version: str = "1"

    def __post_init__(self) -> None:
        if not self.check_name:
            raise ContractValidationError("check_name must not be empty")
        if self.check_type not in _VERIFICATION_CHECK_TYPES:
            raise ContractValidationError(f"invalid check_type: {self.check_type}")
        if self.status not in _VERIFICATION_STATUSES:
            raise ContractValidationError(f"invalid status: {self.status}")
        for digest in self.artifact_digests:
            if not re.match(_DIGEST_PATTERN, digest):
                raise ContractValidationError(f"invalid artifact digest: {digest}")
        if self.duration_ms is not None and self.duration_ms < 0:
            raise ContractValidationError("duration_ms must be non-negative")
        if self.raw_output and redact_text(self.raw_output) != self.raw_output:
            raise ContractValidationError("raw_output contains credential patterns")


@dataclass(frozen=True)
class EvidenceChainLink(Contract):
    """One append-only link binding a decision to the evidence that justifies it."""

    link_id: str = ""
    previous_hash: str = ""
    record_hash: str = ""
    timestamp: str = ""
    decision_type: str = ""
    outcome: str = ""
    task_spec: TaskSpec | None = None
    source_state: SourceState | None = None
    diff_summary: DiffSummary | None = None
    metrics: TrustedChangeMetrics | None = None
    acceptance: AcceptanceDecision | None = None
    route_recommendation: RouteRecommendation | None = None
    regression_observation: RegressionObservation | None = None
    verification: list[VerificationResult] = field(default_factory=list)
    schema_version: str = "1"

    def __post_init__(self) -> None:
        if not self.link_id:
            raise ContractValidationError("link_id must not be empty")
        if not self.previous_hash:
            raise ContractValidationError(
                "previous_hash must not be empty (genesis link uses empty string)"
            )
        if not self.record_hash:
            raise ContractValidationError("record_hash must not be empty")
        if not self.timestamp:
            raise ContractValidationError("timestamp must not be empty")
        if not self.decision_type:
            raise ContractValidationError("decision_type must not be empty")
        if self.outcome not in {"accepted", "rejected", "replan"}:
            raise ContractValidationError(f"invalid outcome: {self.outcome}")
        if self.previous_hash != "" and not re.match(_DIGEST_PATTERN, self.previous_hash):
            raise ContractValidationError(
                "previous_hash must be empty (genesis) or valid sha256 digest"
            )
        if not re.match(_DIGEST_PATTERN, self.record_hash):
            raise ContractValidationError("record_hash must be valid sha256 digest")
        if self.verification:
            for vr in self.verification:
                if isinstance(vr, dict):
                    VerificationResult.from_dict(vr)


# Export map for legacy / discovery helpers
_CONTRACT_REGISTRY: dict[str, type[Contract]] = {
    "task_spec": TaskSpec,
    "TaskSpec": TaskSpec,
    "source_state": SourceState,
    "SourceState": SourceState,
    "diff_summary": DiffSummary,
    "DiffSummary": DiffSummary,
    "trusted_change_metrics": TrustedChangeMetrics,
    "TrustedChangeMetrics": TrustedChangeMetrics,
    "acceptance_decision": AcceptanceDecision,
    "AcceptanceDecision": AcceptanceDecision,
    "route_recommendation": RouteRecommendation,
    "RouteRecommendation": RouteRecommendation,
    "regression_observation": RegressionObservation,
    "RegressionObservation": RegressionObservation,
    "verification_plan": VerificationPlan,
    "VerificationPlan": VerificationPlan,
    "execution_envelope": ExecutionEnvelope,
    "ExecutionEnvelope": ExecutionEnvelope,
    "evidence_chain_link": EvidenceChainLink,
    "EvidenceChainLink": EvidenceChainLink,
    "verification_result": VerificationResult,
    "VerificationResult": VerificationResult,
}


def get_contract(name: str) -> type[Contract] | None:
    """Look up a contract class by its canonical or PascalCase name."""
    try:
        return _CONTRACT_REGISTRY[name]
    except KeyError as exc:
        raise ContractValidationError(f"unknown contract: {name}") from exc


def list_contracts() -> list[str]:
    """Return the set of known contract canonical names (lower_snake_case)."""
    return sorted({k.lower() for k in _CONTRACT_REGISTRY if "_" in k})
