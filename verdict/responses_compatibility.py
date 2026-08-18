"""Versioned, route-scoped compatibility rules for OpenAI Responses payloads."""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any

from verdict.capability_passports import RouteIdentity

RESPONSES_COMPATIBILITY_RULE_VERSION = "responses-compatibility-v1"
_SUPPORTED_FIELDS = frozenset({"client_metadata", "prompt_cache_key", "truncation"})
NVIDIA_RESPONSES_COMPATIBILITY_MODEL_IDS = (
    "nvidia/nvidia/nemotron-3-nano-30b-a3b",
    "nvidia/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
    "nvidia/nvidia/nemotron-3-super-120b-a12b",
    "nvidia/nvidia/nemotron-3-ultra-550b-a55b",
)


class ResponsesCompatibilityError(ValueError):
    """Raised when a compatibility rule is malformed or ambiguous."""


@dataclass(frozen=True)
class ResponsesCompatibilityRule:
    """One explicit provider/model/protocol payload transformation rule."""

    rule_id: str
    version: str
    provider: str
    model_patterns: tuple[str, ...]
    protocol: str
    strip_fields: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        for name in ("rule_id", "version", "provider", "protocol"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ResponsesCompatibilityError(f"{name} must be non-empty")
        if not self.model_patterns or any(
            not isinstance(pattern, str) or not pattern.strip() for pattern in self.model_patterns
        ):
            raise ResponsesCompatibilityError("model_patterns must contain non-empty patterns")
        unknown = set(self.strip_fields) - _SUPPORTED_FIELDS
        if unknown:
            raise ResponsesCompatibilityError(f"unsupported compatibility field: {sorted(unknown)}")

    def matches(self, route: RouteIdentity) -> bool:
        return (
            route.provider == self.provider
            and route.protocol == self.protocol
            and any(fnmatchcase(route.model_id, pattern) for pattern in self.model_patterns)
        )

    def apply(self, payload: dict[str, Any]) -> dict[str, Any]:
        adapted = dict(payload)
        for field in self.strip_fields:
            adapted.pop(field, None)
        return adapted

    def to_dict(self) -> dict[str, Any]:
        """Return the durable, secret-free representation of this rule."""

        return {
            "rule_id": self.rule_id,
            "version": self.version,
            "provider": self.provider,
            "model_patterns": list(self.model_patterns),
            "protocol": self.protocol,
            "strip_fields": sorted(self.strip_fields),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ResponsesCompatibilityRule:
        """Reload a rule from its versioned service/config representation."""

        if not isinstance(value, dict):
            raise ResponsesCompatibilityError("compatibility rule must be a JSON object")
        required = {"rule_id", "version", "provider", "model_patterns", "protocol", "strip_fields"}
        missing = required - value.keys()
        if missing:
            raise ResponsesCompatibilityError(
                f"missing compatibility rule fields: {sorted(missing)}"
            )
        model_patterns = value["model_patterns"]
        strip_fields = value["strip_fields"]
        if not isinstance(model_patterns, list) or not all(
            isinstance(pattern, str) for pattern in model_patterns
        ):
            raise ResponsesCompatibilityError("model_patterns must be a JSON string array")
        if not isinstance(strip_fields, list) or not all(
            isinstance(field, str) for field in strip_fields
        ):
            raise ResponsesCompatibilityError("strip_fields must be a JSON string array")
        return cls(
            rule_id=value["rule_id"],
            version=value["version"],
            provider=value["provider"],
            model_patterns=tuple(model_patterns),
            protocol=value["protocol"],
            strip_fields=frozenset(strip_fields),
        )


NVIDIA_RESPONSES_COMPATIBILITY_RULE = ResponsesCompatibilityRule(
    rule_id="nvidia-http-responses",
    version=RESPONSES_COMPATIBILITY_RULE_VERSION,
    provider="nvidia",
    model_patterns=NVIDIA_RESPONSES_COMPATIBILITY_MODEL_IDS,
    protocol="openai.responses",
    strip_fields=frozenset({"prompt_cache_key"}),
)
RESPONSES_COMPATIBILITY_RULES = (NVIDIA_RESPONSES_COMPATIBILITY_RULE,)


def adapt_responses_payload(
    payload: dict[str, Any],
    route: RouteIdentity,
    *,
    rules: tuple[ResponsesCompatibilityRule, ...] = RESPONSES_COMPATIBILITY_RULES,
) -> tuple[dict[str, Any], str | None]:
    """Apply at most one matching rule without mutating the caller payload."""

    matches = tuple(rule for rule in rules if rule.matches(route))
    if len(matches) > 1:
        raise ResponsesCompatibilityError("multiple Responses compatibility rules matched")
    if not matches:
        return dict(payload), None
    rule = matches[0]
    return rule.apply(payload), rule.version


__all__ = [
    "NVIDIA_RESPONSES_COMPATIBILITY_MODEL_IDS",
    "NVIDIA_RESPONSES_COMPATIBILITY_RULE",
    "RESPONSES_COMPATIBILITY_RULES",
    "RESPONSES_COMPATIBILITY_RULE_VERSION",
    "ResponsesCompatibilityError",
    "ResponsesCompatibilityRule",
    "adapt_responses_payload",
]
