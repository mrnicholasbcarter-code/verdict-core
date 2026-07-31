"""Pure contracts for safe environment discovery and recommendations.

This module intentionally accepts observations from an injected adapter. It
does not inspect the host, read credential stores, contact endpoints, or write
configuration. Those boundaries keep deterministic offline recommendations
separate from potentially unsafe discovery mechanisms.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class DiscoveryState(str, Enum):
    """Normalized states an adapter may report for one capability."""

    ABSENT = "absent"
    INSTALLED = "installed"
    CONFIGURED = "configured"
    REACHABLE = "reachable"
    AUTHENTICATED = "authenticated"
    CATALOG_CLAIMED = "catalog_claimed"
    PROTOCOL_COMPATIBLE = "protocol_compatible"
    QUOTA_AVAILABLE = "quota_available"
    OBSERVED_WORKING = "observed_working"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"


class RecommendationStatus(str, Enum):
    """Whether a profile recommendation is authoritative or needs review."""

    RECOMMENDED = "recommended"
    UNKNOWN = "unknown"


_SECRET_MARKERS = (
    "api_key",
    "apikey",
    "access_token",
    "authorization",
    "bearer ",
    "ghp_",
    "github_pat_",
    "oauth_",
    "password",
    "secret",
    "private_key",
    "sk-",
)
_PATH_MARKERS = ("/home/", "/users/", "\\users\\", "~/.", "\\.config\\", "/.config/")


def _safe_text(value: str, field: str) -> str:
    """Reject secret material and machine-specific paths at the boundary."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    lowered = value.lower()
    if any(marker in lowered for marker in (*_SECRET_MARKERS, *_PATH_MARKERS)):
        raise ValueError(f"{field} contains secret material or a private path")
    return value.strip()


def _safe_optional_text(value: str | None, field: str) -> str | None:
    return None if value is None else _safe_text(value, field)


@dataclass(frozen=True)
class EnvironmentObservation:
    """One redacted, source-attributed observation from a discovery adapter."""

    evidence_id: str
    component: str
    state: DiscoveryState
    source: str
    version: str | None = None
    capabilities: frozenset[str] = frozenset()
    confidence: float = 0.0
    freshness: str = "unknown"
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_id", _safe_text(self.evidence_id, "evidence_id"))
        object.__setattr__(self, "component", _safe_text(self.component, "component"))
        object.__setattr__(self, "source", _safe_text(self.source, "source"))
        object.__setattr__(self, "version", _safe_optional_text(self.version, "version"))
        if not isinstance(self.state, DiscoveryState):
            raise ValueError("state must be a DiscoveryState")
        if type(self.confidence) is not float or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be a float between 0 and 1")
        if self.freshness not in {"fresh", "stale", "unknown"}:
            raise ValueError("freshness must be fresh, stale, or unknown")
        capabilities = frozenset(_safe_text(item, "capability") for item in self.capabilities)
        limitations = tuple(_safe_text(item, "limitation") for item in self.limitations)
        object.__setattr__(self, "capabilities", capabilities)
        object.__setattr__(self, "limitations", limitations)

    def to_dict(self) -> dict[str, Any]:
        """Return a stable JSON-compatible representation."""

        return {
            "evidence_id": self.evidence_id,
            "component": self.component,
            "state": self.state.value,
            "source": self.source,
            "version": self.version,
            "capabilities": sorted(self.capabilities),
            "confidence": self.confidence,
            "freshness": self.freshness,
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True)
class ProfileRecommendation:
    """Deterministic profile recommendation derived only from observations."""

    profile: str
    status: RecommendationStatus
    reason: str
    evidence_ids: tuple[str, ...] = ()
    alternatives: tuple[str, ...] = ()
    questions: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return a stable JSON-compatible representation."""

        return {
            "profile": self.profile,
            "status": self.status.value,
            "reason": self.reason,
            "evidence_ids": list(self.evidence_ids),
            "alternatives": list(self.alternatives),
            "questions": list(self.questions),
        }


@dataclass(frozen=True)
class DiscoveryReport:
    """Normalized observations and recommendations for offline inspection."""

    observations: tuple[EnvironmentObservation, ...]
    recommendations: tuple[ProfileRecommendation, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a stable report with sorted observations and profiles."""

        return {
            "schema_version": "1",
            "mode": "offline",
            "network_access": "disabled",
            "credential_access": "disabled",
            "observations": [item.to_dict() for item in self.observations],
            "recommendations": [item.to_dict() for item in self.recommendations],
        }


def _conflict(observations: tuple[EnvironmentObservation, ...], component: str) -> bool:
    states = {item.state for item in observations if item.component == component}
    conflict_pairs = {
        frozenset({DiscoveryState.ABSENT, state})
        for state in DiscoveryState
        if state not in {DiscoveryState.ABSENT, DiscoveryState.UNKNOWN}
    }
    conflict_pairs.update(
        {
            frozenset({DiscoveryState.DEGRADED, DiscoveryState.OBSERVED_WORKING}),
            frozenset({DiscoveryState.UNKNOWN, DiscoveryState.OBSERVED_WORKING}),
        }
    )
    return any(pair.issubset(states) for pair in conflict_pairs)


def _best(
    observations: tuple[EnvironmentObservation, ...],
    *,
    states: frozenset[DiscoveryState],
    capabilities: frozenset[str] = frozenset(),
) -> tuple[EnvironmentObservation, ...]:
    matches = tuple(
        item
        for item in observations
        if item.state in states
        and (not capabilities or capabilities.issubset(item.capabilities))
        and item.freshness == "fresh"
    )
    return tuple(sorted(matches, key=lambda item: (-item.confidence, item.evidence_id)))


def _recommendation(
    profile: str,
    matches: tuple[EnvironmentObservation, ...],
    reason: str,
    *,
    questions: tuple[str, ...] = (),
    alternatives: tuple[str, ...] = (),
) -> ProfileRecommendation:
    if not matches:
        return ProfileRecommendation(
            profile=profile,
            status=RecommendationStatus.UNKNOWN,
            reason=reason,
            alternatives=alternatives,
            questions=questions or (f"What evidence should qualify the {profile} profile?",),
        )
    return ProfileRecommendation(
        profile=profile,
        status=RecommendationStatus.RECOMMENDED,
        reason=reason,
        evidence_ids=tuple(item.evidence_id for item in matches),
        alternatives=alternatives,
        questions=questions,
    )


def recommend_profiles(
    observations: tuple[EnvironmentObservation, ...],
) -> tuple[ProfileRecommendation, ...]:
    """Build deterministic profile recommendations without authorizing changes."""

    ordered = tuple(sorted(observations, key=lambda item: item.evidence_id))
    components = {item.component for item in ordered}
    conflict_components = sorted(
        component for component in components if _conflict(ordered, component)
    )
    conflict_question = (
        (f"Resolve conflicting evidence for: {', '.join(conflict_components)}.",)
        if conflict_components
        else ()
    )

    local = _best(
        ordered,
        states=frozenset({DiscoveryState.OBSERVED_WORKING}),
        capabilities=frozenset({"local"}),
    )
    private = _recommendation(
        "local/private",
        local,
        "A fresh local observation proves the requested capability without a hosted route.",
        alternatives=("coding-agent", "custom"),
        questions=conflict_question,
    )
    if conflict_components:
        private = ProfileRecommendation(
            profile=private.profile,
            status=RecommendationStatus.UNKNOWN,
            reason="Conflicting observations cannot authorize a profile.",
            alternatives=private.alternatives,
            questions=conflict_question,
        )

    coding = _best(
        ordered,
        states=frozenset({DiscoveryState.OBSERVED_WORKING}),
        capabilities=frozenset({"coding"}),
    )
    cost = _best(
        ordered, states=frozenset({DiscoveryState.QUOTA_AVAILABLE, DiscoveryState.OBSERVED_WORKING})
    )
    gateway = _best(
        ordered,
        states=frozenset({DiscoveryState.AUTHENTICATED, DiscoveryState.PROTOCOL_COMPATIBLE}),
    )
    custom = _best(
        ordered, states=frozenset({DiscoveryState.CONFIGURED, DiscoveryState.PROTOCOL_COMPATIBLE})
    )
    return (
        private,
        _recommendation(
            "cost-conscious",
            cost,
            "Fresh quota or working evidence supports a cost-conscious profile.",
            alternatives=("local/private", "team-gateway"),
            questions=conflict_question,
        ),
        _recommendation(
            "coding-agent",
            coding,
            "Fresh observed-working evidence includes the coding capability.",
            alternatives=("local/private", "custom"),
            questions=conflict_question,
        ),
        _recommendation(
            "team-gateway",
            gateway,
            "Fresh authenticated or protocol-compatible evidence supports a gateway.",
            alternatives=("local/private", "custom"),
            questions=conflict_question,
        ),
        _recommendation(
            "custom",
            custom,
            "Fresh configured or protocol-compatible evidence supports a custom endpoint.",
            alternatives=("local/private", "team-gateway"),
            questions=conflict_question,
        ),
    )


def build_discovery_report(observations: tuple[EnvironmentObservation, ...]) -> DiscoveryReport:
    """Normalize ordering and produce an offline report from injected evidence."""

    ordered = tuple(sorted(observations, key=lambda item: item.evidence_id))
    if len({item.evidence_id for item in ordered}) != len(ordered):
        raise ValueError("evidence_id values must be unique")
    return DiscoveryReport(ordered, recommend_profiles(ordered))


__all__ = [
    "DiscoveryReport",
    "DiscoveryState",
    "EnvironmentObservation",
    "ProfileRecommendation",
    "RecommendationStatus",
    "build_discovery_report",
    "recommend_profiles",
]
