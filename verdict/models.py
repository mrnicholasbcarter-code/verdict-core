"""Core immutable data models used by Verdict routing.

The public models intentionally remain small dataclasses.  They are consumed by
the discovery, availability, router, proxy, and API layers, so fields from both
the original legacy contract and the newer catalog contract are kept here for
backwards compatibility during the rebrand.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RoutingDecision:
    """Result of a deterministic routing decision."""

    model: str
    provider: str
    tier: int
    reason: str
    alternatives: list[str] = field(default_factory=list)
    headroom_pct: float = -1.0
    latency_ms: float = 0.0
    escalated: bool = False
    escalation_reason: str | None = None
    logged: bool = False
    decision: str = "selected"
    request_id: str = ""
    policy_version: str = "policy-2026-07-13.1"
    event_version: str = "1"
    task_class: str = "unknown"
    protected: bool = False
    degraded_mode: bool = False
    managed_backend_status: str = "unknown"
    transport_outcome: str = "not_sent"
    quality_outcome: str = "unknown"
    quality_score: float | None = None
    candidate_states: list[dict[str, Any]] = field(default_factory=list)
    safety_flags: list[str] = field(default_factory=list)
    # Compatibility with the brief advisory-ranking contract.  The canonical
    # names remain ``model`` and ``tier``.
    confidence: float = 0.0

    @property
    def selected_model(self) -> str:
        """Compatibility alias used by early Verdict CLI clients."""
        return self.model

    @property
    def effective_tier(self) -> int:
        """Compatibility alias for the policy tier."""
        return self.tier


@dataclass
class ModelConfig:
    """Optional per-model configuration supplied by older config files."""

    capabilities: list[str] = field(default_factory=list)
    pricing: dict[str, float] = field(default_factory=dict)
    max_tokens: int = -1
    cost_per_1k: float = 0.0


@dataclass
class ProviderConfig:
    """Configuration for one OpenAI-compatible provider."""

    base_url: str = ""
    api_key: str | None = None
    api_key_env: str | None = None
    models_endpoint: str = "/models"
    headroom_endpoint: str | None = None
    priority: int = 0
    models: dict[str, ModelConfig] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelInfo:
    """Discovered model with normalized availability metadata.

    ``model`` and pricing fields are optional compatibility attributes accepted
    by the newer config schema; catalog rows only need ``id`` and ``provider``.
    """

    id: str
    provider: str
    capability_tier: int = 2
    model: str | None = None
    context_window: int = -1
    is_available: bool = True
    capabilities: frozenset[str] = field(default_factory=frozenset)
    availability_state: str = "eligible"
    observed_at: str | None = None
    expires_at: str | None = None
    source: str = "unknown"
    confidence: float | None = None
    quality_confidence: float | None = None
    pricing: dict[str, float] = field(default_factory=dict)
    max_tokens: int = -1
    cost_per_1k: float = 0.0

    def __post_init__(self) -> None:
        # Normalize list-shaped catalog capabilities once at the boundary so
        # downstream availability and dispatcher code can rely on a set.
        object.__setattr__(self, "capabilities", frozenset(self.capabilities))
        if self.model is None:
            object.__setattr__(self, "model", self.id.split("/", 1)[-1])


@dataclass(frozen=True)
class EscalationPattern:
    """Regex pattern that bumps task criticality upward."""

    pattern: str
    min_tier: int
    label: str


@dataclass(frozen=True)
class TaskSpec:
    """Small task specification kept for compatibility with Gate clients."""

    prompt: str
    criticality: str = "medium"
    context: dict[str, Any] = field(default_factory=dict)
    requirements: list[str] = field(default_factory=list)
    budget_per_1k: float | None = None
    privacy_level: str = "standard"
