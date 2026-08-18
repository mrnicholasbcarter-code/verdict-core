"""Deterministic, provenance-rich, injection-safe ContextPack contracts.

The compiler is intentionally offline-first.  Retrieval systems may turn their
records into :class:`ContextUnit` values, but only the plan, unit metadata and
the compiler decide what reaches a model.  The legacy ``ContextPackSlot`` API
remains supported for existing memory hooks.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import time
from typing import Any, ClassVar, Literal

CONTEXT_SCHEMA_VERSION = "1"
SlotType = Literal[
    "system",
    "receipt",
    "memory",
    "dynamic",
    "instructions",
    "policy",
    "state",
    "evidence",
    "tools",
    "examples",
    "history",
]
DecisionAction = Literal["include", "exclude", "transform"]
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)\s*[:=]\s*\S+"),
    re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{16,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"(?i)https?://[^\s/@]+:[^\s/@]+@"),
)
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T")


class ContextContractError(ValueError):
    """Raised when a ContextPack artifact is malformed or unsafe."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except (TypeError, ValueError) as exc:
        raise ContextContractError("context artifact must be JSON-compatible") from exc


def _digest(value: Any) -> str:
    return f"sha256:{hashlib.sha256(_canonical(value).encode()).hexdigest()}"


def _require_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContextContractError(f"{name} must be a non-empty string")
    return value


def _require_digest(value: Any, name: str) -> str:
    result = _require_string(value, name)
    if _SHA256.fullmatch(result) is None:
        raise ContextContractError(f"{name} must be a sha256 digest")
    return result


def _timestamp(value: Any, name: str) -> str:
    result = _require_string(value, name)
    if _ISO.match(result) is None:
        raise ContextContractError(f"{name} must be an ISO-8601 timestamp")
    try:
        datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContextContractError(f"{name} must be an ISO-8601 timestamp") from exc
    return result


def _tuple_strings(value: Any, name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ContextContractError(f"{name} must be an array of strings")
    result = tuple(_require_string(item, f"{name}[]") for item in value)
    return result


def _strict(
    value: Mapping[str, Any], required: set[str], optional: set[str], name: str
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ContextContractError(f"{name} must be an object")
    keys = set(value)
    missing = required - keys
    unknown = keys - required - optional
    if missing:
        raise ContextContractError(f"{name} is missing: {', '.join(sorted(missing))}")
    if unknown:
        raise ContextContractError(f"{name} has unknown fields: {', '.join(sorted(unknown))}")
    return dict(value)


def _safe_content(content: str) -> tuple[bool, str]:
    for pattern in _SECRET_PATTERNS:
        if pattern.search(content):
            return False, "secret_or_private_data_detected"
    return True, "safe"


def sanitize_injection_patterns(text: str) -> str:
    """Quote common control structures while retaining source text faithfully."""
    patterns = [
        (r"<system>", "&lt;system&gt;"),
        (r"</system>", "&lt;/system&gt;"),
        (r"\[INST\]", r"\[INST\]"),
        (r"\[/INST\]", r"\[/INST\]"),
        (r"(?i)\bSystem:\s*", "System (quoted): "),
        (r"(?i)\bUser:\s*", "User (quoted): "),
        (r"(?i)\bAssistant:\s*", "Assistant (quoted): "),
    ]
    sanitized = text
    for pattern, replacement in patterns:
        sanitized = re.sub(pattern, replacement, sanitized)
    return sanitized


def estimate_tokens(text: str) -> int:
    """Conservative deterministic offline estimate (four characters per token)."""
    return max(1, (len(text) + 3) // 4)


@dataclass(frozen=True)
class ContextPlan:
    """Candidate-specific immutable compilation and budget policy."""

    plan_id: str
    candidate_id: str
    tenant_scope: str = "default"
    project_scope: str = "default"
    token_budget: int = 4096
    output_token_reserve: int = 0
    tool_token_reserve: int = 0
    required_slot_types: tuple[SlotType, ...] = ()
    retrieval_algorithm: str = "lexical-bm25"
    retrieval_version: str = "local-fts5-v1"
    created_at: str = field(default_factory=_now_iso)
    schema_version: str = CONTEXT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_string(self.plan_id, "plan_id")
        _require_string(self.candidate_id, "candidate_id")
        _require_string(self.tenant_scope, "tenant_scope")
        _require_string(self.project_scope, "project_scope")
        for name in ("token_budget", "output_token_reserve", "tool_token_reserve"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ContextContractError(f"{name} must be a non-negative integer")
        if self.token_budget < 1:
            raise ContextContractError("token_budget must be positive")
        if self.input_token_budget < 1:
            raise ContextContractError("input budget must remain positive after reserves")
        if self.schema_version != CONTEXT_SCHEMA_VERSION:
            raise ContextContractError("unsupported context plan schema version")
        _timestamp(self.created_at, "created_at")
        _tuple_strings(self.required_slot_types, "required_slot_types")
        object.__setattr__(self, "required_slot_types", tuple(self.required_slot_types))
        _require_string(self.retrieval_algorithm, "retrieval_algorithm")
        _require_string(self.retrieval_version, "retrieval_version")

    @property
    def input_token_budget(self) -> int:
        return self.token_budget - self.output_token_reserve - self.tool_token_reserve

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "candidate_id": self.candidate_id,
            "tenant_scope": self.tenant_scope,
            "project_scope": self.project_scope,
            "token_budget": self.token_budget,
            "input_token_budget": self.input_token_budget,
            "output_token_reserve": self.output_token_reserve,
            "tool_token_reserve": self.tool_token_reserve,
            "required_slot_types": list(self.required_slot_types),
            "retrieval_algorithm": self.retrieval_algorithm,
            "retrieval_version": self.retrieval_version,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ContextPlan:
        payload = _strict(
            value,
            {
                "schema_version",
                "plan_id",
                "candidate_id",
                "tenant_scope",
                "project_scope",
                "token_budget",
                "output_token_reserve",
                "tool_token_reserve",
                "required_slot_types",
                "retrieval_algorithm",
                "retrieval_version",
                "created_at",
            },
            {"input_token_budget"},
            "context_plan",
        )
        result = cls(**{key: payload[key] for key in payload if key != "input_token_budget"})
        if (
            payload.get("input_token_budget", result.input_token_budget)
            != result.input_token_budget
        ):
            raise ContextContractError("input_token_budget does not match reserves")
        return result

    @property
    def digest(self) -> str:
        return _digest(self.to_dict())


@dataclass(frozen=True)
class ContextUnit:
    """A source-attributed candidate unit before budgeted assembly."""

    unit_id: str
    slot_type: SlotType
    key: str
    content: str
    source_uri: str
    source_digest: str
    revision: str = "unknown"
    span: Mapping[str, int] | None = None
    observed_at: str = field(default_factory=_now_iso)
    valid_from: str | None = None
    valid_until: str | None = None
    trust: str = "unverified"
    authority: str = "unverified"
    sensitivity: str = "standard"
    tenant_scope: str = "default"
    project_scope: str = "default"
    raw: bool = True
    transform_lineage: tuple[str, ...] = ()
    token_count: int | None = None
    cache_key: str | None = None
    confidence: float = 1.0
    created_at: float = field(default_factory=time)
    schema_version: str = CONTEXT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in (
            "unit_id",
            "key",
            "content",
            "source_uri",
            "revision",
            "trust",
            "authority",
            "sensitivity",
            "tenant_scope",
            "project_scope",
        ):
            _require_string(getattr(self, name), name)
        if self.slot_type not in get_slot_types():
            raise ContextContractError("slot_type is invalid")
        _require_digest(self.source_digest, "source_digest")
        _timestamp(self.observed_at, "observed_at")
        for name in ("valid_from", "valid_until"):
            value = getattr(self, name)
            if value is not None:
                _timestamp(value, name)
        if self.span is not None and (
            not isinstance(self.span, Mapping)
            or any(
                not isinstance(key, str) or not isinstance(item, int) or item < 0
                for key, item in self.span.items()
            )
        ):
            raise ContextContractError("span must contain non-negative integer offsets")
        if self.token_count is not None and (
            isinstance(self.token_count, bool)
            or not isinstance(self.token_count, int)
            or self.token_count < 1
        ):
            raise ContextContractError("token_count must be positive")
        if self.cache_key is not None:
            _require_string(self.cache_key, "cache_key")
        if not isinstance(self.raw, bool):
            raise ContextContractError("raw must be boolean")
        if not 0 <= self.confidence <= 1:
            raise ContextContractError("confidence must be between 0 and 1")
        _tuple_strings(self.transform_lineage, "transform_lineage")
        if self.schema_version != CONTEXT_SCHEMA_VERSION:
            raise ContextContractError("unsupported context unit schema version")

    @property
    def effective_token_count(self) -> int:
        return self.token_count or estimate_tokens(self.content)

    @property
    def scope(self) -> str:
        return f"{self.tenant_scope}:{self.project_scope}"

    def to_dict(self, *, include_content: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "unit_id": self.unit_id,
            "slot_type": self.slot_type,
            "key": self.key,
            "source_uri": self.source_uri,
            "source_digest": self.source_digest,
            "revision": self.revision,
            "span": dict(self.span) if self.span is not None else None,
            "observed_at": self.observed_at,
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
            "trust": self.trust,
            "authority": self.authority,
            "sensitivity": self.sensitivity,
            "tenant_scope": self.tenant_scope,
            "project_scope": self.project_scope,
            "raw": self.raw,
            "transform_lineage": list(self.transform_lineage),
            "token_count": self.effective_token_count,
            "cache_key": self.cache_key,
            "confidence": self.confidence,
            "created_at": self.created_at,
        }
        if include_content:
            payload["content"] = self.content
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ContextUnit:
        payload = _strict(
            value,
            {
                "schema_version",
                "unit_id",
                "slot_type",
                "key",
                "content",
                "source_uri",
                "source_digest",
                "revision",
                "span",
                "observed_at",
                "valid_from",
                "valid_until",
                "trust",
                "authority",
                "sensitivity",
                "tenant_scope",
                "project_scope",
                "raw",
                "transform_lineage",
                "token_count",
                "cache_key",
                "confidence",
                "created_at",
            },
            set(),
            "context_unit",
        )
        return cls(**payload)


@dataclass(frozen=True)
class ContextPackSlot:
    """Legacy source-attributed prompt slot accepted by the v0 compiler."""

    slot_type: SlotType
    key: str
    content: str
    source: str
    confidence: float = 1.0
    sensitivity: str = "public"
    created_at: float = field(default_factory=time)
    source_uri: str | None = None
    source_digest: str | None = None
    revision: str = "unknown"
    tenant_scope: str = "default"
    project_scope: str = "default"
    valid_until: str | None = None

    def to_unit(self) -> ContextUnit:
        return ContextUnit(
            unit_id=f"slot:{self.slot_type}:{self.key}",
            slot_type=self.slot_type,
            key=self.key,
            content=self.content,
            source_uri=self.source_uri or f"urn:verdict:source:{self.source}",
            source_digest=self.source_digest
            or _digest({"source": self.source, "content": self.content}),
            revision=self.revision,
            sensitivity=self.sensitivity,
            tenant_scope=self.tenant_scope,
            project_scope=self.project_scope,
            valid_until=self.valid_until,
            confidence=self.confidence,
            created_at=self.created_at,
        )


@dataclass(frozen=True)
class ContextDecision:
    """Auditable include, exclude, or transform result for one unit."""

    unit_id: str
    action: DecisionAction
    reason: str
    input_tokens: int
    output_tokens: int
    fidelity: str = "verified"
    reversible_ref: str | None = None
    schema_version: str = CONTEXT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_string(self.unit_id, "decision.unit_id")
        if self.action not in {"include", "exclude", "transform"}:
            raise ContextContractError("decision.action is invalid")
        _require_string(self.reason, "decision.reason")
        for name in ("input_tokens", "output_tokens"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ContextContractError(f"decision.{name} must be non-negative")
        if self.reversible_ref is not None:
            _require_string(self.reversible_ref, "decision.reversible_ref")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "unit_id": self.unit_id,
            "action": self.action,
            "reason": self.reason,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "fidelity": self.fidelity,
            "reversible_ref": self.reversible_ref,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ContextDecision:
        return cls(
            **_strict(
                value,
                {
                    "schema_version",
                    "unit_id",
                    "action",
                    "reason",
                    "input_tokens",
                    "output_tokens",
                    "fidelity",
                    "reversible_ref",
                },
                set(),
                "context_decision",
            )
        )


@dataclass(frozen=True)
class ContextPack:
    """Compiled prompt plus provenance and deterministic assembly decisions."""

    pack_id: str
    compiled_prompt: str
    used_tokens: int
    token_budget: int
    slots: tuple[ContextPackSlot, ...]
    conflicts: tuple[dict[str, Any], ...]
    truncated_count: int
    created_at: float
    plan_id: str | None = None
    plan_digest: str | None = None
    candidate_id: str | None = None
    tenant_scope: str = "default"
    project_scope: str = "default"
    units: tuple[ContextUnit, ...] = ()
    decisions: tuple[ContextDecision, ...] = ()
    receipt_id: str | None = None
    schema_version: str = CONTEXT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "pack_id": self.pack_id,
            "plan_id": self.plan_id,
            "plan_digest": self.plan_digest,
            "candidate_id": self.candidate_id,
            "tenant_scope": self.tenant_scope,
            "project_scope": self.project_scope,
            "compiled_prompt": self.compiled_prompt,
            "used_tokens": self.used_tokens,
            "token_budget": self.token_budget,
            "slots": [slot.__dict__ for slot in self.slots],
            "units": [unit.to_dict() for unit in self.units],
            "conflicts": [dict(item) for item in self.conflicts],
            "decisions": [item.to_dict() for item in self.decisions],
            "truncated_count": self.truncated_count,
            "created_at": self.created_at,
            "receipt_id": self.receipt_id,
        }

    @property
    def digest(self) -> str:
        return _digest(self.to_dict())

    def canonical_json(self) -> str:
        return _canonical(self.to_dict())

    @property
    def receipt(self) -> ContextReceipt:
        return ContextReceipt.from_pack(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ContextPack:
        payload = _strict(
            value,
            {
                "schema_version",
                "pack_id",
                "plan_id",
                "plan_digest",
                "candidate_id",
                "tenant_scope",
                "project_scope",
                "compiled_prompt",
                "used_tokens",
                "token_budget",
                "slots",
                "units",
                "conflicts",
                "decisions",
                "truncated_count",
                "created_at",
                "receipt_id",
            },
            set(),
            "context_pack",
        )
        slots = tuple(ContextPackSlot(**item) for item in payload["slots"])
        units = tuple(ContextUnit.from_dict(item) for item in payload["units"])
        decisions = tuple(ContextDecision.from_dict(item) for item in payload["decisions"])
        return cls(
            **{
                **payload,
                "slots": slots,
                "units": units,
                "decisions": decisions,
                "conflicts": tuple(payload["conflicts"]),
            }
        )


@dataclass(frozen=True)
class ContextReceipt:
    """Portable, payload-free assembly receipt for checkpoint and audit use."""

    receipt_id: str
    plan_digest: str
    pack_digest: str
    decisions: tuple[ContextDecision, ...]
    unresolved_uncertainties: tuple[str, ...] = ()
    created_at: str = field(default_factory=_now_iso)
    schema_version: str = CONTEXT_SCHEMA_VERSION

    @classmethod
    def from_pack(cls, pack: ContextPack) -> ContextReceipt:
        return cls(
            receipt_id=pack.receipt_id or f"receipt:{pack.pack_id}",
            plan_digest=pack.plan_digest
            or _digest({"plan_id": pack.plan_id, "candidate_id": pack.candidate_id}),
            pack_digest=pack.digest,
            decisions=pack.decisions,
            unresolved_uncertainties=tuple(
                f"conflict:{item.get('key', 'unknown')}" for item in pack.conflicts
            ),
        )

    def __post_init__(self) -> None:
        _require_string(self.receipt_id, "receipt_id")
        _require_digest(self.plan_digest, "plan_digest")
        _require_digest(self.pack_digest, "pack_digest")
        _timestamp(self.created_at, "created_at")
        _tuple_strings(self.unresolved_uncertainties, "unresolved_uncertainties")
        if self.schema_version != CONTEXT_SCHEMA_VERSION:
            raise ContextContractError("unsupported context receipt schema version")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "plan_digest": self.plan_digest,
            "pack_digest": self.pack_digest,
            "decisions": [item.to_dict() for item in self.decisions],
            "unresolved_uncertainties": list(self.unresolved_uncertainties),
            "created_at": self.created_at,
        }

    @property
    def digest(self) -> str:
        return _digest(self.to_dict())

    def verify(self, pack: ContextPack) -> bool:
        return self.pack_digest == pack.digest and self.decisions == pack.decisions

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ContextReceipt:
        payload = _strict(
            value,
            {
                "schema_version",
                "receipt_id",
                "plan_digest",
                "pack_digest",
                "decisions",
                "unresolved_uncertainties",
                "created_at",
            },
            set(),
            "context_receipt",
        )
        return cls(
            **{
                **payload,
                "decisions": tuple(
                    ContextDecision.from_dict(item) for item in payload["decisions"]
                ),
                "unresolved_uncertainties": tuple(payload["unresolved_uncertainties"]),
            }
        )


def get_slot_types() -> frozenset[str]:
    return frozenset(
        {
            "system",
            "receipt",
            "memory",
            "dynamic",
            "instructions",
            "policy",
            "state",
            "evidence",
            "tools",
            "examples",
            "history",
        }
    )


class ContextPackCompiler:
    """Offline deterministic compiler with fail-closed scope and safety gates."""

    _PRECEDENCE: ClassVar[dict[str, int]] = {
        "system": 0,
        "instructions": 0,
        "policy": 1,
        "receipt": 2,
        "tools": 3,
        "evidence": 4,
        "memory": 5,
        "state": 6,
        "dynamic": 7,
        "examples": 8,
        "history": 9,
    }

    def __init__(self, default_token_budget: int = 4096) -> None:
        self.default_token_budget = default_token_budget

    def compile_units(
        self,
        units: list[ContextUnit] | tuple[ContextUnit, ...],
        plan: ContextPlan,
        pack_id: str | None = None,
    ) -> ContextPack:
        if not isinstance(plan, ContextPlan):
            raise ContextContractError("plan must be a ContextPlan")
        sorted_units = sorted(
            units,
            key=lambda unit: (
                self._PRECEDENCE.get(unit.slot_type, 99),
                -unit.confidence,
                -unit.created_at,
                unit.key,
                unit.unit_id,
            ),
        )
        conflicts: list[dict[str, Any]] = []
        seen: dict[str, str] = {}
        for unit in sorted_units:
            previous = seen.get(unit.key)
            if previous is not None and previous != unit.content:
                conflicts.append(
                    {
                        "key": unit.key,
                        "unit_id": unit.unit_id,
                        "reason": "duplicate_key_contradictory_content",
                        "existing_content_hash": _digest(previous)[:23],
                        "new_content_hash": _digest(unit.content)[:23],
                    }
                )
            else:
                seen[unit.key] = unit.content

        parts: list[str] = []
        included_units: list[ContextUnit] = []
        included_slots: list[ContextPackSlot] = []
        decisions: list[ContextDecision] = []
        current_tokens = 0
        truncated = 0
        now = datetime.now(timezone.utc)
        for unit in sorted_units:
            safe, safety_reason = _safe_content(unit.content)
            if not safe:
                decisions.append(
                    ContextDecision(
                        unit.unit_id,
                        "exclude",
                        safety_reason,
                        unit.effective_token_count,
                        0,
                        "not_applicable",
                        unit.source_uri,
                    )
                )
                truncated += 1
                continue
            if unit.tenant_scope not in {"*", plan.tenant_scope} or unit.project_scope not in {
                "*",
                plan.project_scope,
            }:
                decisions.append(
                    ContextDecision(
                        unit.unit_id,
                        "exclude",
                        "scope_mismatch",
                        unit.effective_token_count,
                        0,
                        "not_applicable",
                        unit.source_uri,
                    )
                )
                truncated += 1
                continue
            if (
                unit.valid_until is not None
                and datetime.fromisoformat(unit.valid_until.replace("Z", "+00:00")) <= now
            ):
                decisions.append(
                    ContextDecision(
                        unit.unit_id,
                        "exclude",
                        "source_expired",
                        unit.effective_token_count,
                        0,
                        "not_applicable",
                        unit.source_uri,
                    )
                )
                truncated += 1
                continue
            content = sanitize_injection_patterns(unit.content)
            header = f"[{unit.slot_type.upper()}:{unit.key} (source: {unit.source_uri})]"
            rendered = f"{header}\n{content}\n"
            cost = estimate_tokens(rendered)
            if current_tokens + cost > plan.input_token_budget:
                decisions.append(
                    ContextDecision(
                        unit.unit_id,
                        "exclude",
                        "input_budget_exhausted",
                        unit.effective_token_count,
                        0,
                        "not_applicable",
                        unit.source_uri,
                    )
                )
                truncated += 1
                continue
            parts.append(rendered)
            included_units.append(unit)
            included_slots.append(
                ContextPackSlot(
                    unit.slot_type,
                    unit.key,
                    content,
                    unit.source_uri,
                    unit.confidence,
                    unit.sensitivity,
                    unit.created_at,
                    unit.source_uri,
                    unit.source_digest,
                    unit.revision,
                    unit.tenant_scope,
                    unit.project_scope,
                    unit.valid_until,
                )
            )
            current_tokens += cost
            action: DecisionAction = "transform" if content != unit.content else "include"
            decisions.append(
                ContextDecision(
                    unit.unit_id,
                    action,
                    "injection_patterns_sanitized" if action == "transform" else "included",
                    unit.effective_token_count,
                    cost,
                    "verified",
                    unit.source_uri,
                )
            )

        compiled = "\n".join(parts)
        actual_id = pack_id or hashlib.sha256(compiled.encode()).hexdigest()[:16]
        return ContextPack(
            pack_id=actual_id,
            compiled_prompt=compiled,
            used_tokens=current_tokens,
            token_budget=plan.input_token_budget,
            slots=tuple(included_slots),
            conflicts=tuple(conflicts),
            truncated_count=truncated,
            created_at=time(),
            plan_id=plan.plan_id,
            plan_digest=plan.digest,
            candidate_id=plan.candidate_id,
            tenant_scope=plan.tenant_scope,
            project_scope=plan.project_scope,
            units=tuple(included_units),
            decisions=tuple(decisions),
            receipt_id=f"receipt:{actual_id}",
        )

    def compile(
        self,
        slots: list[ContextPackSlot],
        token_budget: int | None = None,
        pack_id: str | None = None,
        *,
        candidate_id: str = "legacy",
        tenant_scope: str = "default",
        project_scope: str = "default",
        output_token_reserve: int = 0,
        tool_token_reserve: int = 0,
    ) -> ContextPack:
        """Compile legacy slots, optionally reserving output and tool budget."""
        plan = ContextPlan(
            plan_id=f"legacy:{candidate_id}",
            candidate_id=candidate_id,
            tenant_scope=tenant_scope,
            project_scope=project_scope,
            token_budget=token_budget or self.default_token_budget,
            output_token_reserve=output_token_reserve,
            tool_token_reserve=tool_token_reserve,
        )
        return self.compile_units(tuple(slot.to_unit() for slot in slots), plan, pack_id)


__all__ = [
    "CONTEXT_SCHEMA_VERSION",
    "ContextContractError",
    "ContextDecision",
    "ContextPack",
    "ContextPackCompiler",
    "ContextPackSlot",
    "ContextPlan",
    "ContextReceipt",
    "ContextUnit",
    "DecisionAction",
    "SlotType",
    "estimate_tokens",
    "sanitize_injection_patterns",
]
