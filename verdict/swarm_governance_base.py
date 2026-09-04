from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from typing import Any, ClassVar, cast

from verdict.contracts import Contract, ContractValidationError

SWARM_SPEC_VERSION = "swarm-spec/v1"
VALIDATOR_VERSION = "swarm-validator/v1"
ALLOWED_CONTROLS = frozenset({"pause", "resume", "cancel"})
SLICE_STATES = frozenset(
    {
        "pending",
        "submitted",
        "running",
        "paused",
        "cancelling",
        "cancelled",
        "completed",
        "timeout",
        "rejected",
        "failed",
    }
)
TERMINAL_STATES = frozenset({"completed", "failed", "cancelled", "timeout", "rejected"})
_SENSITIVE_PATTERN = re.compile(
    r"(?i)(authorization\s*:|bearer\s+[A-Za-z0-9._-]+|api[_-]?key|secret|password|token\s*[=:])"
)


def contains_sensitive(value: str) -> bool:
    return _SENSITIVE_PATTERN.search(value) is not None


def require_text(value: str, name: str, *, maximum: int | None = None) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ContractValidationError(f"{name} must be a non-empty string")
    if maximum is not None and len(value) > maximum:
        raise ContractValidationError(f"{name} must be at most {maximum} characters")
    if _SENSITIVE_PATTERN.search(value):
        raise ContractValidationError(f"{name} contains prohibited sensitive content")


def require_items(values: Sequence[str], name: str, *, non_empty: bool = True) -> None:
    if non_empty and not values:
        raise ContractValidationError(f"{name} must be non-empty")
    if len(values) != len(set(values)):
        raise ContractValidationError(f"{name} contains duplicate values")
    for value in values:
        require_text(value, name)


def budget_dict(budget: Any) -> dict[str, Any]:
    if budget is None:
        return {"max_usd": 0.0, "max_tokens": 0, "max_latency_ms": 0}
    if hasattr(budget, "to_dict"):
        return dict(budget.to_dict())
    return dict(budget)


def budget_cap(budget: Any, key: str) -> float:
    raw = budget_dict(budget).get(key, 0)
    if raw is None:
        return 0.0
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ContractValidationError(f"budget.{key} must be finite") from exc
    if not math.isfinite(value) or value < 0:
        raise ContractValidationError(f"budget.{key} must be finite and non-negative")
    return value


def canonical(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return canonical(value.to_dict())
    if isinstance(value, Mapping):
        return {str(key): canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [canonical(item) for item in value]
    if isinstance(value, set | frozenset):
        return sorted(canonical(item) for item in value)
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(canonical(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class GovernanceContract(Contract):
    contract_version: ClassVar[str] = SWARM_SPEC_VERSION

    def to_dict(self) -> dict[str, Any]:
        if not is_dataclass(self):
            raise TypeError("governance contracts must be dataclasses")
        return {
            item.name: canonical(getattr(self, item.name)) if item.name != "spec" else None
            for item in fields(cast(Any, self))
            if item.name != "allowed_actions"
        }

    def digest(self) -> str:
        return canonical_digest(self.to_dict())


__all__ = [
    "ALLOWED_CONTROLS",
    "SLICE_STATES",
    "SWARM_SPEC_VERSION",
    "TERMINAL_STATES",
    "VALIDATOR_VERSION",
    "GovernanceContract",
    "budget_cap",
    "budget_dict",
    "canonical",
    "canonical_digest",
    "canonical_json",
    "contains_sensitive",
    "require_items",
    "require_text",
]
