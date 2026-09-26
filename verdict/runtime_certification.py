"""Runtime certification — evidence-only discovery→normalize→certify (passport).

This module certifies harnesses, gateways, MCP/intelligence providers, and
model pools from injected snapshots and optional plugin detectors.  It records
ready/degraded/unavailable/unsupported state, freshness/TTL, harness capability
parity (``supported|partial|unsupported``), and memory/docs authority classes.

**Not routing authority.** Reports never select routes, STAY/SWITCH, or
dispatch targets.  Consumers (BOD-104/119/122/124) fuse this evidence later.

Harness parity contract (feeds BOD-124 setup recommendations)
-------------------------------------------------------------
For each discovered harness, ``CertifiedComponent.parity`` lists facets from
``HARNESS_PARITY_FACETS``.  Binary/config *presence* alone never upgrades a
facet to ``supported``; each facet requires explicit evidence.  BOD-124 may
read parity to recommend install/config steps; BOD-80 may surface unsupported
hooks.  Unsupported ops and missing quota/cache remain explicit ``None`` /
limitations — never fabricated.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any

RUNTIME_CERTIFICATION_SCHEMA_VERSION = "1"
CERTIFICATION_TTL_SECONDS = 300

HARNESS_PARITY_FACETS: tuple[str, ...] = (
    "mcp",
    "hooks",
    "subagents",
    "resume",
    "tool_interception",
    "model_selection",
    "config_import",
    "structured_output",
)

# Canonical memory MCP wins; peers become redundant when it is present.
_MEMORY_AUTHORITY_IDS = frozenset({"codebase-memory-mcp", "verdict-memory"})
_MEMORY_REDUNDANT_IDS = frozenset({"basic-memory-pilot", "ecc-memory-vault"})
_DOCS_AUTHORITY_IDS = frozenset({"docs-mcp-server"})
_DOCS_ADVISORY_IDS = frozenset({"context7"})

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

_Detector = Callable[[Mapping[str, Any]], tuple["DetectedSnapshot", ...]]
_DETECTORS: dict[str, _Detector] = {}


class RuntimeCertificationError(ValueError):
    """Raised when certification input or reports violate the evidence contract."""


class CertificationState(str, Enum):
    """Observed certification outcome for one component."""

    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class ParityLevel(str, Enum):
    """Harness capability-parity levels consumed by capability bootstrap DX."""

    SUPPORTED = "supported"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"


class AuthorityClass(str, Enum):
    """How overlapping MCP/intelligence surfaces relate."""

    AUTHORITY = "authority"
    ADAPTER = "adapter"
    ADVISORY = "advisory"
    REDUNDANT = "redundant"
    UNKNOWN = "unknown"


class ComponentKind(str, Enum):
    HARNESS = "harness"
    GATEWAY = "gateway"
    MCP = "mcp"
    MODEL_POOL = "model_pool"
    INTELLIGENCE = "intelligence"
    PROVIDER = "provider"
    TOOLCHAIN = "toolchain"


@dataclass(frozen=True)
class ProbeBudget:
    """Bounded probe allowance — premium traffic stays minimal."""

    max_probes: int = 8
    max_premium_probes: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.max_probes, int) or self.max_probes < 0:
            raise RuntimeCertificationError("max_probes must be a non-negative int")
        if not isinstance(self.max_premium_probes, int) or self.max_premium_probes < 0:
            raise RuntimeCertificationError("max_premium_probes must be a non-negative int")


@dataclass(frozen=True)
class ParityFacet:
    """One harness parity facet with evidence source (never binary-presence alone)."""

    facet: str
    level: ParityLevel
    source: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "facet": self.facet,
            "level": self.level.value,
            "source": self.source,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ParityFacet:
        return cls(
            facet=_safe_text(value["facet"], "facet"),
            level=ParityLevel(value["level"]),
            source=_safe_text(value["source"], "source"),
            reason=_safe_text(value["reason"], "reason"),
        )


@dataclass(frozen=True)
class DetectedSnapshot:
    """Injected discovery evidence — detectors produce these; no live I/O here."""

    component_id: str
    kind: ComponentKind
    identity: str
    source: str
    health_claim: str = "unknown"
    version: str | None = None
    capabilities: frozenset[str] = frozenset()
    observed_at: datetime | None = None
    requires_probe: bool = False
    premium_probe: bool = False
    evidence: Mapping[str, Any] = field(default_factory=dict)
    models: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "component_id", _safe_text(self.component_id, "component_id"))
        object.__setattr__(self, "identity", _safe_text(self.identity, "identity"))
        object.__setattr__(self, "source", _safe_text(self.source, "source"))
        if not isinstance(self.kind, ComponentKind):
            raise RuntimeCertificationError("kind must be a ComponentKind")
        claim = self.health_claim.strip().lower() if isinstance(self.health_claim, str) else ""
        if claim not in {
            "ready",
            "degraded",
            "unavailable",
            "unsupported",
            "unknown",
            "configured",
            "catalog_claimed",
        }:
            raise RuntimeCertificationError("health_claim is invalid")
        object.__setattr__(self, "health_claim", claim)
        if self.version is not None:
            object.__setattr__(self, "version", _safe_text(self.version, "version"))
        caps = frozenset(_safe_text(item, "capability") for item in self.capabilities)
        object.__setattr__(self, "capabilities", caps)
        if self.observed_at is not None:
            object.__setattr__(self, "observed_at", _utc(self.observed_at, "observed_at"))
        object.__setattr__(self, "evidence", MappingProxyType(dict(self.evidence)))
        frozen_models = tuple(MappingProxyType(dict(item)) for item in self.models)
        object.__setattr__(self, "models", frozen_models)


@dataclass(frozen=True)
class CertifiedComponent:
    """Normalized certification evidence for one discovered component."""

    component_id: str
    kind: ComponentKind
    identity: str
    state: CertificationState
    source: str
    confidence: float
    freshness: str
    observed_at: datetime
    expires_at: datetime
    version: str | None = None
    authority_class: AuthorityClass | None = None
    capabilities: frozenset[str] = frozenset()
    parity: tuple[ParityFacet, ...] | None = None
    quota: Mapping[str, Any] | None = None
    cache: Mapping[str, Any] | None = None
    cooldown: Mapping[str, Any] | None = None
    limitations: tuple[str, ...] = ()
    evidence_digest: str = ""

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise RuntimeCertificationError("confidence must be between 0 and 1")
        if self.freshness not in {"fresh", "stale", "unknown"}:
            raise RuntimeCertificationError("freshness must be fresh, stale, or unknown")
        if not self.evidence_digest.startswith("sha256:"):
            digest = _digest(
                {
                    "component_id": self.component_id,
                    "kind": self.kind.value,
                    "identity": self.identity,
                    "state": self.state.value,
                    "source": self.source,
                    "observed_at": _format_datetime(self.observed_at),
                }
            )
            object.__setattr__(self, "evidence_digest", digest)

    def to_dict(self) -> dict[str, Any]:
        return {
            "component_id": self.component_id,
            "kind": self.kind.value,
            "identity": self.identity,
            "state": self.state.value,
            "source": self.source,
            "confidence": self.confidence,
            "freshness": self.freshness,
            "observed_at": _format_datetime(self.observed_at),
            "expires_at": _format_datetime(self.expires_at),
            "version": self.version,
            "authority_class": None if self.authority_class is None else self.authority_class.value,
            "capabilities": sorted(self.capabilities),
            "parity": None if self.parity is None else [item.to_dict() for item in self.parity],
            "quota": None if self.quota is None else dict(self.quota),
            "cache": None if self.cache is None else dict(self.cache),
            "cooldown": None if self.cooldown is None else dict(self.cooldown),
            "limitations": list(self.limitations),
            "evidence_digest": self.evidence_digest,
        }


@dataclass(frozen=True)
class RuntimeCertificationReport:
    """Machine-readable certification passport — evidence only."""

    certified_at: datetime
    expires_at: datetime
    ttl_seconds: int
    components: tuple[CertifiedComponent, ...]
    memory_authority: str | None
    conflicts: tuple[str, ...]
    probes_used: int
    premium_probes_used: int
    schema_version: str = RUNTIME_CERTIFICATION_SCHEMA_VERSION
    purpose: str = "evidence"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "purpose": self.purpose,
            "certified_at": _format_datetime(self.certified_at),
            "expires_at": _format_datetime(self.expires_at),
            "ttl_seconds": self.ttl_seconds,
            "memory_authority": self.memory_authority,
            "conflicts": list(self.conflicts),
            "probes_used": self.probes_used,
            "premium_probes_used": self.premium_probes_used,
            "components": [item.to_dict() for item in self.components],
        }


def register_detector(name: str, detector: _Detector) -> None:
    """Register a plugin detector. Adding adapters does not require editing certify()."""

    key = _safe_text(name, "detector name")
    if key in _DETECTORS:
        raise RuntimeCertificationError(f"detector already registered: {key}")
    _DETECTORS[key] = detector


def reset_detectors() -> None:
    """Clear the detector registry (tests / isolated runs)."""

    _DETECTORS.clear()


def harness_parity_from_evidence(
    *, harness_id: str, evidence: Mapping[str, Any], source: str
) -> tuple[ParityFacet, ...]:
    """Map harness evidence to parity facets.

    Binary presence never upgrades facets. Explicit config/capability keys do.
    """

    _ = harness_id  # identity reserved for future adapter-specific maps
    facets: list[ParityFacet] = []
    binary_only = bool(evidence.get("binary_present")) and not any(
        evidence.get(key)
        for key in (
            "mcp_config",
            "hooks_config",
            "hooks",
            "subagents",
            "resume",
            "tool_interception",
            "model_selection",
            "config_import",
            "structured_output",
        )
    )
    mapping = {
        "mcp": ("mcp_config", "mcp"),
        "hooks": ("hooks_config", "hooks"),
        "subagents": ("subagents",),
        "resume": ("resume",),
        "tool_interception": ("tool_interception",),
        "model_selection": ("model_selection",),
        "config_import": ("config_import",),
        "structured_output": ("structured_output",),
    }
    for facet in HARNESS_PARITY_FACETS:
        keys = mapping[facet]
        raw = next((evidence[k] for k in keys if k in evidence), None)
        if binary_only or raw is None or raw is False:
            level = ParityLevel.UNSUPPORTED
            reason = (
                "binary presence is not parity" if binary_only else "no explicit evidence for facet"
            )
        elif raw is True or raw == "supported":
            level = ParityLevel.SUPPORTED
            reason = f"explicit evidence via {keys[0]}"
        elif raw == "partial":
            level = ParityLevel.PARTIAL
            reason = f"partial evidence via {keys[0]}"
        else:
            # unknown / unexpected → unsupported (fail closed)
            level = ParityLevel.UNSUPPORTED
            reason = f"non-affirmative evidence for {facet}"
        facets.append(
            ParityFacet(
                facet=facet, level=level, source=_safe_text(source, "source"), reason=reason
            )
        )
    return tuple(facets)


def classify_memory_mcps(snapshots: Sequence[DetectedSnapshot]) -> dict[str, AuthorityClass]:
    """Classify overlapping memory MCPs; only one canonical authority."""

    memoryish = [
        item
        for item in snapshots
        if item.kind is ComponentKind.MCP
        and (
            item.component_id in _MEMORY_AUTHORITY_IDS
            or item.component_id in _MEMORY_REDUNDANT_IDS
            or "memory" in item.capabilities
            or "context" in item.capabilities
        )
    ]
    result: dict[str, AuthorityClass] = {}
    authority = next(
        (item.component_id for item in memoryish if item.component_id in _MEMORY_AUTHORITY_IDS),
        None,
    )
    if authority is None and memoryish:
        # Prefer first known memory-capable id deterministically when no hard-coded authority.
        authority = sorted(item.component_id for item in memoryish)[0]
    for item in memoryish:
        if item.component_id == authority:
            result[item.component_id] = AuthorityClass.AUTHORITY
        elif item.component_id in _MEMORY_REDUNDANT_IDS or authority is not None:
            result[item.component_id] = AuthorityClass.REDUNDANT
        else:
            result[item.component_id] = AuthorityClass.UNKNOWN
    return result


def certify_runtime(
    *,
    snapshots: Sequence[DetectedSnapshot] = (),
    now: datetime | None = None,
    ttl_seconds: int = CERTIFICATION_TTL_SECONDS,
    probe_budget: ProbeBudget | None = None,
    run_registered_detectors: bool = False,
    detector_context: Mapping[str, Any] | None = None,
) -> RuntimeCertificationReport:
    """Discover (optional detectors) → normalize → certify. Evidence only."""

    current = _utc(now or datetime.now(timezone.utc), "now")
    budget = probe_budget or ProbeBudget()
    if ttl_seconds <= 0:
        raise RuntimeCertificationError("ttl_seconds must be positive")

    collected: list[DetectedSnapshot] = list(snapshots)
    if run_registered_detectors:
        ctx = detector_context or {}
        for name in sorted(_DETECTORS):
            collected.extend(_DETECTORS[name](ctx))

    probes_used = 0
    premium_used = 0
    for snap in collected:
        if snap.requires_probe:
            probes_used += 1
            if probes_used > budget.max_probes:
                raise RuntimeCertificationError("probe budget exceeded")
        if snap.premium_probe:
            premium_used += 1
            if premium_used > budget.max_premium_probes:
                raise RuntimeCertificationError("probe budget exceeded: premium")

    memory_classes = classify_memory_mcps(collected)
    memory_authority = next(
        (cid for cid, cls in memory_classes.items() if cls is AuthorityClass.AUTHORITY), None
    )

    components: list[CertifiedComponent] = []
    conflicts: list[str] = []
    expires_at = current + timedelta(seconds=ttl_seconds)

    for snap in sorted(collected, key=lambda item: (item.kind.value, item.component_id)):
        observed_at = snap.observed_at or current
        age = (current - observed_at).total_seconds()
        freshness = "fresh" if age <= ttl_seconds else "stale"
        if freshness == "stale" and age > ttl_seconds:
            state = CertificationState.UNAVAILABLE
            limitations = ["evidence stale beyond TTL"]
            confidence = 0.2
        else:
            state, limitations, confidence = _state_from_claim(snap)

        authority = memory_classes.get(snap.component_id)
        if authority is None:
            authority = _docs_or_default_authority(snap)

        parity: tuple[ParityFacet, ...] | None = None
        if snap.kind is ComponentKind.HARNESS:
            parity = harness_parity_from_evidence(
                harness_id=snap.component_id, evidence=snap.evidence, source=snap.source
            )

        quota = None
        cache = None
        cooldown = None
        extra_limits = list(limitations)
        if snap.kind is ComponentKind.GATEWAY:
            if "quota" not in snap.evidence and "quota" not in snap.capabilities:
                extra_limits.append("quota unsupported (not fabricated)")
            else:
                quota = _optional_observed_map(snap.evidence.get("quota"), "quota")
            if "cache" not in snap.evidence:
                extra_limits.append("cache unsupported (not fabricated)")
            else:
                cache = _optional_observed_map(snap.evidence.get("cache"), "cache")
            if "cooldown" in snap.evidence:
                cooldown = _optional_observed_map(snap.evidence.get("cooldown"), "cooldown")

        if snap.component_id == "serena" and "symbols" in snap.capabilities:
            extra_limits.append(
                "serena owns semantic symbols/references; exact-text/AST tooling remains complementary"
            )

        components.append(
            CertifiedComponent(
                component_id=snap.component_id,
                kind=snap.kind,
                identity=snap.identity,
                state=state if freshness == "fresh" else CertificationState.UNAVAILABLE,
                source=snap.source,
                confidence=confidence if freshness == "fresh" else min(confidence, 0.2),
                freshness=freshness,
                observed_at=observed_at,
                expires_at=expires_at,
                version=snap.version,
                authority_class=authority,
                capabilities=snap.capabilities,
                parity=parity,
                quota=quota,
                cache=cache,
                cooldown=cooldown,
                limitations=tuple(extra_limits),
            )
        )

        # Model pool rows keep gateway+provider+model identity separate.
        for model in snap.models:
            components.append(_certify_model(model, snap, current, expires_at, ttl_seconds))

    # Surface duplicate memory conflicts for consumers.
    redundant = [cid for cid, cls in memory_classes.items() if cls is AuthorityClass.REDUNDANT]
    if memory_authority and redundant:
        conflicts.append(
            f"memory authority={memory_authority}; redundant={','.join(sorted(redundant))}"
        )

    components.sort(key=lambda item: (item.kind.value, item.component_id, item.identity))
    return RuntimeCertificationReport(
        certified_at=current,
        expires_at=expires_at,
        ttl_seconds=ttl_seconds,
        components=tuple(components),
        memory_authority=memory_authority,
        conflicts=tuple(conflicts),
        probes_used=probes_used,
        premium_probes_used=premium_used,
    )


def _certify_model(
    model: Mapping[str, Any],
    gateway: DetectedSnapshot,
    now: datetime,
    expires_at: datetime,
    ttl_seconds: int,
) -> CertifiedComponent:
    gateway_id = _safe_text(str(model.get("gateway") or gateway.component_id), "gateway")
    provider = _safe_text(str(model.get("provider") or "unknown"), "provider")
    model_id = _safe_text(str(model.get("model_id") or "unknown"), "model_id")
    identity = f"gateway/{gateway_id}/provider/{provider}/model/{model_id}"

    catalog_only = bool(model.get("catalog_claimed")) and model.get("invocation") not in {
        "ok",
        "ready",
        True,
    }
    invocation = model.get("invocation")
    assessment = model.get("assessment") or model.get("health")
    limitations: list[str] = []
    if catalog_only or (invocation is None and assessment is None):
        state = CertificationState.UNAVAILABLE
        limitations.append("catalog claim alone never marks a model healthy; invocation required")
        confidence = 0.1
    elif invocation in {"ok", "ready", True} or assessment in {"ready", "healthy"}:
        state = CertificationState.READY
        confidence = 0.85
    elif invocation == "degraded" or assessment == "degraded":
        state = CertificationState.DEGRADED
        confidence = 0.5
        limitations.append("model invocation degraded")
    else:
        state = CertificationState.UNAVAILABLE
        confidence = 0.2
        limitations.append("invocation/assessment evidence missing or negative")

    auth = model.get("auth_state")
    if auth == "unauthorized":
        state = CertificationState.UNAVAILABLE
        limitations.append("auth unauthorized")

    return CertifiedComponent(
        component_id=f"{gateway_id}:{provider}:{model_id}",
        kind=ComponentKind.MODEL_POOL,
        identity=identity,
        state=state,
        source=gateway.source,
        confidence=confidence,
        freshness="fresh",
        observed_at=now,
        expires_at=expires_at,
        version=None,
        authority_class=None,
        capabilities=frozenset(),
        limitations=tuple(limitations),
    )


def _state_from_claim(snap: DetectedSnapshot) -> tuple[CertificationState, list[str], float]:
    claim = snap.health_claim
    if claim == "ready":
        return CertificationState.READY, [], 0.9
    if claim == "degraded":
        return CertificationState.DEGRADED, ["reported degraded"], 0.55
    if claim == "unsupported":
        return CertificationState.UNSUPPORTED, ["explicitly unsupported"], 1.0
    if claim == "unavailable":
        return CertificationState.UNAVAILABLE, ["reported unavailable"], 0.8
    if claim == "configured":
        return (CertificationState.UNAVAILABLE, ["configured is not certified healthy"], 0.4)
    if claim == "catalog_claimed":
        return (CertificationState.UNAVAILABLE, ["catalog claim is not health"], 0.2)
    return CertificationState.UNKNOWN, ["health unknown"], 0.0


def _docs_or_default_authority(snap: DetectedSnapshot) -> AuthorityClass | None:
    if snap.kind is not ComponentKind.MCP:
        return None
    if snap.component_id in _DOCS_AUTHORITY_IDS:
        return AuthorityClass.AUTHORITY
    if snap.component_id in _DOCS_ADVISORY_IDS:
        return AuthorityClass.ADVISORY
    return None


def _optional_observed_map(value: Any, field: str) -> Mapping[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise RuntimeCertificationError(f"{field} must be an object when present")
    # Reject secret-like keys/values.
    for key, item in value.items():
        _safe_text(str(key), f"{field}.key")
        if isinstance(item, str):
            _safe_text(item, f"{field}.{key}")
    return MappingProxyType(dict(value))


def _safe_text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeCertificationError(f"{field} must be a non-empty string")
    lowered = value.lower()
    if any(marker in lowered for marker in (*_SECRET_MARKERS, *_PATH_MARKERS)):
        raise RuntimeCertificationError(f"{field} contains secret material or a private path")
    return value.strip()


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RuntimeCertificationError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _format_datetime(value: datetime) -> str:
    return _utc(value, "timestamp").isoformat().replace("+00:00", "Z")


def _digest(value: Mapping[str, Any]) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "CERTIFICATION_TTL_SECONDS",
    "HARNESS_PARITY_FACETS",
    "RUNTIME_CERTIFICATION_SCHEMA_VERSION",
    "AuthorityClass",
    "CertificationState",
    "CertifiedComponent",
    "ComponentKind",
    "DetectedSnapshot",
    "ParityFacet",
    "ParityLevel",
    "ProbeBudget",
    "RuntimeCertificationError",
    "RuntimeCertificationReport",
    "certify_runtime",
    "classify_memory_mcps",
    "harness_parity_from_evidence",
    "register_detector",
    "reset_detectors",
]
