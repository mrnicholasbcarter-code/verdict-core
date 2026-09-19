"""Capability bootstrap / installer DX (BOD-124).

Staged flow: discover → normalize → recommend → preflight → consent → apply → certify.

Setup/DX only — not Core routing, cost ledger, or BOD-104. Third-party software is
never silently installed. APPLY authorization is unified in
``authorize_bootstrap_actions``:

* interactive: final ``consent=True`` authorizes the shown plan, or an allowlist
  may authorize a subset without blanket consent;
* non-interactive / CI: only an explicit provider (or action) allowlist authorizes
  mutations — ``consent=True`` alone is insufficient.

When APPLY is authorized, a bounded default ``InstallRunner`` may execute the
documented OmniRoute install (``npm install -g omniroute``) only — other providers
still require an explicit runner and otherwise fail closed.

Binary presence is not health, authentication, or qualification. Recommendations are
capability-first (providers second). OmniRoute may be recommended but is never a hard
dependency or routing authority.

Certification consumes BOD-92 passport/certification helpers when available via a narrow
``Certifier`` Protocol seam; this module does not own evidence-type definitions.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from verdict.bootstrap_install import (
    InstallCommandRunner,
    build_default_install_runner,
    run_default_provider_install,
)
from verdict.capability_registry import (
    ResolveDecision,
    SemanticCapabilityRegistry,
    build_default_registry,
    resolve_capability,
)
from verdict.setup_plan import SetupAction

SCHEMA_VERSION = "capability-bootstrap/v1"
_DIGEST_PREFIX = "sha256:"
_OWNERSHIP_SCHEMA = "bootstrap-ownership/v1"

# High-value capabilities setup cares about (brand-free). Providers satisfy these.
_BOOTSTRAP_CAPABILITIES: tuple[str, ...] = (
    "harness.execute",
    "gateway.inventory",
    "gateway.execute",
    "code.symbols",
    "code.definitions",
    "code.references",
    "code.graph",
    "docs.lookup",
    "memory.search",
    "security.secrets",
)


class BootstrapMode(str, Enum):
    PLAN = "plan"
    RECOMMENDED = "recommended"
    APPLY = "apply"


class BootstrapScope(str, Enum):
    ALL = "all"
    INTELLIGENCE = "intelligence"
    GATEWAYS = "gateways"
    HARNESSES = "harnesses"


class ProviderKind(str, Enum):
    HARNESS = "harness"
    GATEWAY = "gateway"
    INTELLIGENCE = "intelligence"
    DOCS = "docs"
    RUNTIME = "runtime"
    SECURITY = "security"
    OTHER = "other"


class ProviderLifecycle(str, Enum):
    """Coarse lifecycle; never collapse installed into healthy/qualified."""

    NOT_INSTALLED = "not_installed"
    INSTALLED = "installed"
    CONFIGURED = "configured"
    AUTHENTICATED = "authenticated"
    REACHABLE = "reachable"
    HEALTHY = "healthy"
    QUALIFIED = "qualified"


class StageName(str, Enum):
    DISCOVER = "discover"
    NORMALIZE = "normalize"
    RECOMMEND = "recommend"
    PREFLIGHT = "preflight"
    CONSENT = "consent"
    APPLY = "apply"
    CERTIFY = "certify"
    ROLLBACK = "rollback"


@dataclass(frozen=True)
class DiscoveredProvider:
    """Normalized provider record feeding BOD-87 / BOD-92 consumers."""

    provider_id: str
    provider_kind: ProviderKind
    capabilities: frozenset[str]
    lifecycle: ProviderLifecycle = ProviderLifecycle.NOT_INSTALLED
    version: str | None = None
    path_or_endpoint: str | None = None
    config_sources: tuple[str, ...] = ()
    auth_state: str = "unknown"
    health_state: str = "unknown"
    qualification_state: str = "unqualified"
    authority: int = 0
    freshness: str = "unknown"
    latency_cost: str | None = None
    managed_by_verdict: bool = False
    install_source: str = ""
    install_command: str = ""
    last_probe: str | None = None
    failure_reason: str | None = None
    hard_dependency: bool = False
    optional: bool = True

    def __post_init__(self) -> None:
        if self.lifecycle is ProviderLifecycle.INSTALLED and self.health_state == "healthy":
            raise ValueError("installed lifecycle cannot claim healthy without promotion")
        if self.hard_dependency and self.provider_id.startswith("gateway.omniroute"):
            raise ValueError("OmniRoute must never be a hard dependency")

    def to_dict(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id,
            "provider_kind": self.provider_kind.value,
            "capabilities": sorted(self.capabilities),
            "lifecycle": self.lifecycle.value,
            "version": self.version,
            "path_or_endpoint": self.path_or_endpoint,
            "config_sources": list(self.config_sources),
            "auth_state": self.auth_state,
            "health_state": self.health_state,
            "qualification_state": self.qualification_state,
            "authority": self.authority,
            "freshness": self.freshness,
            "latency_cost": self.latency_cost,
            "managed_by_verdict": self.managed_by_verdict,
            "install_source": self.install_source,
            "install_command": self.install_command,
            "last_probe": self.last_probe,
            "failure_reason": self.failure_reason,
            "hard_dependency": self.hard_dependency,
            "optional": self.optional,
        }


@dataclass(frozen=True)
class CapabilityRecommendation:
    """Capability-first recommendation; providers are candidates, not authority."""

    capability_id: str
    status: str  # covered | missing | degraded
    candidate_providers: tuple[str, ...]
    reason: str
    selected_provider_id: str | None = None
    optional: bool = True
    hard_dependency: bool = False
    parity: str | None = None  # supported | partial | unsupported (BOD-92 seam)

    def to_dict(self) -> dict[str, object]:
        # Capability fields first for machine + human consumers.
        return {
            "capability_id": self.capability_id,
            "status": self.status,
            "selected_provider_id": self.selected_provider_id,
            "candidate_providers": list(self.candidate_providers),
            "reason": self.reason,
            "optional": self.optional,
            "hard_dependency": self.hard_dependency,
            "parity": self.parity,
        }


@dataclass(frozen=True)
class BootstrapAction:
    """One proposed mutation with consent / rollback metadata."""

    action_id: str
    kind: str
    target: str
    description: str
    reason: str
    security_impact: str
    postcondition: str
    undo: str
    reversible: bool
    requires_consent: bool
    provider_id: str | None = None
    capability_ids: tuple[str, ...] = ()
    install_command: str = ""
    install_source: str = ""
    files_changed: tuple[str, ...] = ()
    privilege: str = "user"
    trust_warning: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "action_id": self.action_id,
            "kind": self.kind,
            "target": self.target,
            "description": self.description,
            "reason": self.reason,
            "security_impact": self.security_impact,
            "postcondition": self.postcondition,
            "undo": self.undo,
            "reversible": self.reversible,
            "requires_consent": self.requires_consent,
            "provider_id": self.provider_id,
            "capability_ids": list(self.capability_ids),
            "install_command": self.install_command,
            "install_source": self.install_source,
            "files_changed": list(self.files_changed),
            "privilege": self.privilege,
            "trust_warning": self.trust_warning,
        }

    def to_setup_action(self) -> SetupAction:
        return SetupAction(
            action_id=self.action_id,
            kind=self.kind,
            target=self.target,
            description=self.description,
            reason=self.reason,
            security_impact=self.security_impact,
            postcondition=self.postcondition,
            undo=self.undo,
            reversible=self.reversible,
            requires_consent=self.requires_consent,
        )


@dataclass(frozen=True)
class UnifiedBootstrapPlan:
    """Single preflight plan shown before final consent."""

    actions: tuple[BootstrapAction, ...]
    mutation_free: bool
    scope: BootstrapScope
    mode: BootstrapMode

    def _content_dict(self) -> dict[str, object]:
        return {
            "kind": "capability_bootstrap_plan",
            "schema_version": SCHEMA_VERSION,
            "status": "planned",
            "mode": self.mode.value,
            "scope": self.scope.value,
            "mutation_free": self.mutation_free,
            "network_access": "disabled",
            "credential_access": "disabled",
            "actions": [action.to_dict() for action in self.actions],
            "next": (
                "review this plan; APPLY requires interactive consent (--yes) "
                "or an explicit allowlist (--allow); non-interactive APPLY "
                "requires --allow"
            ),
        }

    @property
    def digest(self) -> str:
        canonical = json.dumps(
            self._content_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return _DIGEST_PREFIX + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, object]:
        content = self._content_dict()
        digest = self.digest
        content["plan_digest"] = digest
        content["plan_id"] = digest
        return content


@dataclass(frozen=True)
class StageResult:
    stage: str
    status: str  # ok | skipped | blocked | failed
    summary: str
    details: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "status": self.status,
            "summary": self.summary,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class BootstrapReport:
    stages: tuple[StageResult, ...]
    providers: tuple[DiscoveredProvider, ...]
    recommendations: tuple[CapabilityRecommendation, ...]
    plan: UnifiedBootstrapPlan
    apply: Mapping[str, object]
    certification: Mapping[str, object]
    mutation_free: bool
    network_access: str = "disabled"
    credential_access: str = "disabled"
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "kind": "capability_bootstrap_report",
            "mutation_free": self.mutation_free,
            "network_access": self.network_access,
            "credential_access": self.credential_access,
            "stages": [stage.to_dict() for stage in self.stages],
            "providers": [provider.to_dict() for provider in self.providers],
            "recommendations": [item.to_dict() for item in self.recommendations],
            "plan": self.plan.to_dict(),
            "apply": dict(self.apply),
            "certification": dict(self.certification),
        }


class Certifier(Protocol):
    """Narrow seam for BOD-92 runtime certification (evidence only)."""

    def __call__(self, providers: tuple[DiscoveredProvider, ...]) -> Mapping[str, Any]: ...


InstallRunner = Callable[[BootstrapAction], Mapping[str, Any]]
PathResolver = Callable[[str], str | None]
GatewayProbe = Callable[[str], Mapping[str, Any]]


@dataclass(frozen=True)
class _CatalogEntry:
    provider_id: str
    provider_kind: ProviderKind
    capabilities: frozenset[str]
    binaries: tuple[str, ...] = ()
    install_source: str = ""
    install_command: str = ""
    optional: bool = True
    hard_dependency: bool = False
    authority: int = 0
    default_endpoint: str | None = None


# Catalog for discovery — brands here; recommendations stay capability-first.
_CATALOG: tuple[_CatalogEntry, ...] = (
    _CatalogEntry(
        "harness.claude_code",
        ProviderKind.HARNESS,
        frozenset({"harness.execute"}),
        binaries=("claude",),
        install_source="https://docs.anthropic.com/claude-code",
        install_command="Follow upstream Claude Code install docs",
        authority=40,
    ),
    _CatalogEntry(
        "harness.codex",
        ProviderKind.HARNESS,
        frozenset({"harness.execute"}),
        binaries=("codex",),
        install_source="https://github.com/openai/codex",
        install_command="Follow upstream Codex CLI install docs",
        authority=40,
    ),
    _CatalogEntry(
        "harness.hermes",
        ProviderKind.HARNESS,
        frozenset({"harness.execute"}),
        binaries=("hermes",),
        install_source="https://github.com/NousResearch/hermes-agent",
        install_command="Follow upstream Hermes installer",
        authority=35,
    ),
    _CatalogEntry(
        "harness.prime",
        ProviderKind.HARNESS,
        frozenset({"harness.execute"}),
        binaries=("prime-agent", "prime"),
        install_source="prime-agent",
        install_command="Follow upstream Prime Agent install docs",
        authority=30,
    ),
    _CatalogEntry(
        "harness.cursor",
        ProviderKind.HARNESS,
        frozenset({"harness.execute"}),
        binaries=("cursor", "cursor-agent"),
        install_source="https://cursor.com",
        install_command="Follow upstream Cursor CLI install docs",
        authority=35,
    ),
    _CatalogEntry(
        "harness.opencode",
        ProviderKind.HARNESS,
        frozenset({"harness.execute"}),
        binaries=("opencode", "opencode-go"),
        install_source="opencode",
        install_command="Follow upstream OpenCode install docs",
        authority=30,
    ),
    _CatalogEntry(
        "harness.cline",
        ProviderKind.HARNESS,
        frozenset({"harness.execute"}),
        binaries=("cline",),
        install_source="https://github.com/cline/cline",
        install_command="npm install -g cline  # or install the VS Code Cline extension",
        authority=30,
    ),
    _CatalogEntry(
        "gateway.omniroute",
        ProviderKind.GATEWAY,
        frozenset({"gateway.inventory", "gateway.execute", "gateway.quota"}),
        binaries=("omniroute",),
        install_source="https://github.com/NeuronZero/omniroute",
        install_command="npm install -g omniroute",
        authority=50,
        default_endpoint="http://127.0.0.1:20128/v1",
    ),
    _CatalogEntry(
        "gateway.litellm",
        ProviderKind.GATEWAY,
        frozenset({"gateway.inventory", "gateway.execute"}),
        binaries=("litellm",),
        install_source="https://github.com/BerriAI/litellm",
        install_command="pip install litellm",
        authority=40,
        default_endpoint="http://127.0.0.1:4000/v1",
    ),
    _CatalogEntry(
        "gateway.9router",
        ProviderKind.GATEWAY,
        frozenset({"gateway.inventory", "gateway.execute"}),
        binaries=("9router",),
        install_source="https://github.com/1jehuang/9router",
        install_command="npm install -g 9router",
        authority=35,
        default_endpoint="http://127.0.0.1:20129/v1",
    ),
    _CatalogEntry(
        "adapter.serena_lsp",
        ProviderKind.INTELLIGENCE,
        frozenset(
            {
                "code.symbols",
                "code.definitions",
                "code.references",
                "code.callers",
                "code.callees",
                "code.imports",
                "code.graph",
            }
        ),
        binaries=("serena",),
        install_source="serena-mcp",
        install_command="Follow Serena MCP install docs",
        authority=80,
    ),
    _CatalogEntry(
        "adapter.codebase_memory",
        ProviderKind.INTELLIGENCE,
        frozenset({"code.graph", "code.changed-neighborhood", "memory.search"}),
        binaries=("codebase-memory-mcp", "codebase-memory"),
        install_source="codebase-memory-mcp",
        install_command="Follow Codebase Memory MCP install docs",
        authority=90,
    ),
    _CatalogEntry(
        "adapter.context7",
        ProviderKind.DOCS,
        frozenset({"docs.lookup", "docs.library"}),
        binaries=("context7",),
        install_source="context7-mcp",
        install_command="Follow Context7 MCP install docs",
        authority=70,
    ),
    _CatalogEntry(
        "native.verdict.ast",
        ProviderKind.INTELLIGENCE,
        frozenset({"code.symbols", "code.graph"}),
        install_source="verdict-native",
        optional=False,
        hard_dependency=False,
        authority=10,
    ),
    _CatalogEntry(
        "tool.ripgrep",
        ProviderKind.OTHER,
        frozenset({"git.diff", "repo.state"}),
        binaries=("rg",),
        install_source="ripgrep",
        install_command="Install ripgrep via OS package manager",
        authority=5,
    ),
    _CatalogEntry(
        "security.gitleaks",
        ProviderKind.SECURITY,
        frozenset({"security.secrets"}),
        binaries=("gitleaks",),
        install_source="https://github.com/gitleaks/gitleaks",
        install_command="Follow Gitleaks install docs",
        authority=20,
    ),
    _CatalogEntry(
        "security.semgrep",
        ProviderKind.SECURITY,
        frozenset({"security.agent_config"}),
        binaries=("semgrep",),
        install_source="https://semgrep.dev",
        install_command="Follow Semgrep install docs",
        authority=20,
    ),
    _CatalogEntry(
        "security.agentshield",
        ProviderKind.SECURITY,
        frozenset({"security.prompt_injection", "security.mcp_config"}),
        binaries=("agentshield",),
        install_source="agentshield",
        install_command="Follow AgentShield install docs",
        authority=20,
    ),
)


def _scope_kinds(scope: BootstrapScope) -> frozenset[ProviderKind] | None:
    if scope is BootstrapScope.ALL:
        return None
    if scope is BootstrapScope.INTELLIGENCE:
        return frozenset({ProviderKind.INTELLIGENCE, ProviderKind.DOCS, ProviderKind.OTHER})
    if scope is BootstrapScope.GATEWAYS:
        return frozenset({ProviderKind.GATEWAY})
    if scope is BootstrapScope.HARNESSES:
        return frozenset({ProviderKind.HARNESS})
    return None


def _default_path_resolver(name: str) -> str | None:
    return shutil.which(name)


def _default_gateway_probe(provider_id: str) -> Mapping[str, Any]:
    """Bounded local check — presence of env URL ≠ health."""
    if provider_id == "gateway.omniroute":
        base = os.getenv("OMNIROUTE_BASE_URL", "").strip()
        if base:
            return {
                "reachable": False,
                "health_ok": False,
                "reason": "endpoint configured but not live-certified",
                "endpoint": base.rstrip("/"),
            }
    return {"reachable": False, "health_ok": False, "reason": "no live probe in setup plan mode"}


def _provider_kind_to_component_kind(kind: ProviderKind) -> Any:
    from verdict.runtime_certification import ComponentKind

    mapping = {
        ProviderKind.HARNESS: ComponentKind.HARNESS,
        ProviderKind.GATEWAY: ComponentKind.GATEWAY,
        ProviderKind.INTELLIGENCE: ComponentKind.INTELLIGENCE,
        ProviderKind.DOCS: ComponentKind.MCP,
        ProviderKind.RUNTIME: ComponentKind.TOOLCHAIN,
        ProviderKind.SECURITY: ComponentKind.TOOLCHAIN,
        ProviderKind.OTHER: ComponentKind.PROVIDER,
    }
    return mapping.get(kind, ComponentKind.PROVIDER)


def _health_claim_for_provider(provider: DiscoveredProvider) -> str:
    """Map bootstrap lifecycle/health into BOD-92 health_claim vocabulary."""

    if provider.lifecycle is ProviderLifecycle.NOT_INSTALLED:
        return "unsupported"
    if (
        provider.lifecycle is ProviderLifecycle.HEALTHY
        and provider.health_state == "healthy"
        and provider.qualification_state == "qualified"
    ):
        return "ready"
    if provider.health_state == "unhealthy":
        return "degraded"
    if provider.lifecycle is ProviderLifecycle.INSTALLED and provider.health_state != "healthy":
        # Binary/config presence is never certification — matches installed != healthy.
        return "configured"
    if provider.lifecycle in {
        ProviderLifecycle.CONFIGURED,
        ProviderLifecycle.AUTHENTICATED,
        ProviderLifecycle.REACHABLE,
    }:
        return "configured"
    return "unknown"


def _safe_snapshot_capabilities(capabilities: frozenset[str]) -> frozenset[str]:
    """Drop capability ids that BOD-92 ``_safe_text`` rejects (e.g. ``security.secrets``)."""

    from verdict.runtime_certification import RuntimeCertificationError, _safe_text

    safe: set[str] = set()
    for item in capabilities:
        try:
            safe.add(_safe_text(item, "capability"))
        except RuntimeCertificationError:
            continue
    return frozenset(safe)


def _discovered_to_snapshot(provider: DiscoveredProvider) -> Any:
    from verdict.runtime_certification import DetectedSnapshot

    # Never put filesystem paths into identity — BOD-92 rejects private-path text.
    identity = provider.provider_id
    evidence: dict[str, object] = {
        "lifecycle": provider.lifecycle.value,
        "health_state": provider.health_state,
        "qualification_state": provider.qualification_state,
        "auth_state": provider.auth_state,
        "binary_present": bool(provider.path_or_endpoint)
        and provider.lifecycle is not ProviderLifecycle.NOT_INSTALLED,
        "has_endpoint": bool(provider.path_or_endpoint),
    }
    if provider.failure_reason:
        # Keep reason free of path/secret material by truncating to a short token.
        reason = provider.failure_reason.strip().split("/")[0][:120]
        if reason:
            evidence["failure_reason"] = reason
    return DetectedSnapshot(
        component_id=provider.provider_id,
        kind=_provider_kind_to_component_kind(provider.provider_kind),
        identity=identity,
        source="capability_bootstrap",
        health_claim=_health_claim_for_provider(provider),
        version=provider.version,
        capabilities=_safe_snapshot_capabilities(provider.capabilities),
        requires_probe=False,
        premium_probe=False,
        evidence=evidence,
    )


def _parity_from_component(state: str, parity_facets: object) -> str:
    """Collapse BOD-92 state + optional facets into bootstrap's single parity string."""

    if state == "ready":
        level = "supported"
    elif state == "degraded":
        level = "partial"
    elif state in {"unavailable", "unsupported"}:
        level = "unsupported"
    else:
        level = "partial"
    if not isinstance(parity_facets, list) or not parity_facets:
        return level
    facet_levels = {
        str(item.get("level"))
        for item in parity_facets
        if isinstance(item, dict) and item.get("level")
    }
    if "unsupported" in facet_levels and level == "supported":
        return "partial"
    return level


def _default_certifier(
    providers: tuple[DiscoveredProvider, ...], *, now: datetime | None = None
) -> Mapping[str, Any]:
    """Certify bootstrap providers via BOD-92 ``certify_runtime`` (evidence only)."""

    from datetime import timezone

    from verdict.runtime_certification import CertificationState, certify_runtime

    snapshots = tuple(_discovered_to_snapshot(provider) for provider in providers)
    report = certify_runtime(
        snapshots=snapshots, now=now or datetime.now(timezone.utc), run_registered_detectors=False
    )
    report_dict = report.to_dict()
    by_id = {component.component_id: component for component in report.components}

    results: list[dict[str, object]] = []
    for provider in providers:
        component = by_id.get(provider.provider_id)
        if component is None:
            results.append(
                {
                    "provider_id": provider.provider_id,
                    "certified": False,
                    "parity": "unsupported",
                    "reason": "missing from runtime certification report",
                    "passport_seam": "runtime_certification",
                }
            )
            continue
        certified = component.state is CertificationState.READY
        reason = (
            "provider certified ready by BOD-92 runtime certification"
            if certified
            else (
                "; ".join(component.limitations)
                if component.limitations
                else f"state={component.state.value}"
            )
        )
        if (
            provider.lifecycle is ProviderLifecycle.INSTALLED
            and provider.health_state != "healthy"
            and not certified
        ):
            reason = "installed != healthy"
        results.append(
            {
                "provider_id": provider.provider_id,
                "certified": certified,
                "parity": _parity_from_component(
                    component.state.value,
                    [facet.to_dict() for facet in component.parity] if component.parity else None,
                ),
                "reason": reason,
                "passport_seam": "runtime_certification",
                "state": component.state.value,
                "confidence": component.confidence,
                "freshness": component.freshness,
            }
        )

    summary_bits = [item["reason"] for item in results if item["reason"] == "installed != healthy"]
    return {
        "schema_version": "runtime-certification/v1",
        "passport_seam": "runtime_certification",
        "results": results,
        "summary": "; ".join(str(bit) for bit in summary_bits) or "certification complete",
        "runtime_report": report_dict,
    }


def discover_providers(
    *,
    path_resolver: PathResolver | None = None,
    probe_gateway: GatewayProbe | None = None,
    scope: BootstrapScope = BootstrapScope.ALL,
) -> tuple[DiscoveredProvider, ...]:
    """Discover catalog providers without promoting binary presence to healthy."""

    which = path_resolver or _default_path_resolver
    probe = probe_gateway or _default_gateway_probe
    kinds = _scope_kinds(scope)
    found: list[DiscoveredProvider] = []

    for entry in _CATALOG:
        if kinds is not None and entry.provider_kind not in kinds:
            continue
        if entry.provider_id == "native.verdict.ast":
            found.append(
                DiscoveredProvider(
                    provider_id=entry.provider_id,
                    provider_kind=entry.provider_kind,
                    capabilities=entry.capabilities,
                    lifecycle=ProviderLifecycle.HEALTHY,
                    health_state="healthy",
                    auth_state="not_required",
                    qualification_state="qualified",
                    authority=entry.authority,
                    freshness="fresh",
                    install_source=entry.install_source,
                    optional=entry.optional,
                    hard_dependency=entry.hard_dependency,
                )
            )
            continue

        binary_path: str | None = None
        for binary in entry.binaries:
            binary_path = which(binary)
            if binary_path:
                break

        if entry.provider_kind is ProviderKind.GATEWAY:
            probe_result = dict(probe(entry.provider_id))
            endpoint = str(probe_result.get("endpoint") or entry.default_endpoint or "")
            omniroute_env = entry.provider_id == "gateway.omniroute" and bool(
                os.getenv("OMNIROUTE_BASE_URL")
            )
            if binary_path or probe_result.get("reachable") or omniroute_env:
                health_ok = bool(probe_result.get("health_ok"))
                if health_ok:
                    lifecycle = ProviderLifecycle.HEALTHY
                    health_state = "healthy"
                    qualification = "qualified"
                    auth = "authenticated"
                    failure = None
                else:
                    lifecycle = (
                        ProviderLifecycle.INSTALLED if binary_path else ProviderLifecycle.CONFIGURED
                    )
                    health_state = "unhealthy" if binary_path or endpoint else "unknown"
                    qualification = "unqualified"
                    auth = "unknown"
                    failure = str(probe_result.get("reason") or "installed != healthy")
                found.append(
                    DiscoveredProvider(
                        provider_id=entry.provider_id,
                        provider_kind=entry.provider_kind,
                        capabilities=entry.capabilities,
                        lifecycle=lifecycle,
                        path_or_endpoint=endpoint or binary_path,
                        auth_state=auth,
                        health_state=health_state,
                        qualification_state=qualification,
                        authority=entry.authority,
                        freshness="fresh",
                        install_source=entry.install_source,
                        install_command=entry.install_command,
                        failure_reason=failure,
                        optional=entry.optional,
                        hard_dependency=False,
                    )
                )
            else:
                found.append(
                    DiscoveredProvider(
                        provider_id=entry.provider_id,
                        provider_kind=entry.provider_kind,
                        capabilities=entry.capabilities,
                        lifecycle=ProviderLifecycle.NOT_INSTALLED,
                        install_source=entry.install_source,
                        install_command=entry.install_command,
                        authority=entry.authority,
                        optional=entry.optional,
                        hard_dependency=False,
                    )
                )
            continue

        if binary_path:
            # Presence only — never healthy/qualified without separate certification.
            found.append(
                DiscoveredProvider(
                    provider_id=entry.provider_id,
                    provider_kind=entry.provider_kind,
                    capabilities=entry.capabilities,
                    lifecycle=ProviderLifecycle.INSTALLED,
                    path_or_endpoint=binary_path,
                    auth_state="unknown",
                    health_state="unknown",
                    qualification_state="unqualified",
                    authority=entry.authority,
                    freshness="unknown",
                    install_source=entry.install_source,
                    install_command=entry.install_command,
                    failure_reason="binary present; not live-certified",
                    optional=entry.optional,
                    hard_dependency=entry.hard_dependency,
                )
            )
        else:
            found.append(
                DiscoveredProvider(
                    provider_id=entry.provider_id,
                    provider_kind=entry.provider_kind,
                    capabilities=entry.capabilities,
                    lifecycle=ProviderLifecycle.NOT_INSTALLED,
                    install_source=entry.install_source,
                    install_command=entry.install_command,
                    authority=entry.authority,
                    optional=entry.optional,
                    hard_dependency=entry.hard_dependency,
                )
            )

    return tuple(sorted(found, key=lambda item: item.provider_id))


def _filter_scope(
    providers: Sequence[DiscoveredProvider], scope: BootstrapScope
) -> tuple[DiscoveredProvider, ...]:
    kinds = _scope_kinds(scope)
    if kinds is None:
        return tuple(providers)
    return tuple(item for item in providers if item.provider_kind in kinds)


def _catalog_placeholders(scope: BootstrapScope) -> tuple[DiscoveredProvider, ...]:
    """Ensure missing catalog providers appear as not_installed recommendation candidates."""

    kinds = _scope_kinds(scope)
    placeholders: list[DiscoveredProvider] = []
    for entry in _CATALOG:
        if kinds is not None and entry.provider_kind not in kinds:
            continue
        placeholders.append(
            DiscoveredProvider(
                provider_id=entry.provider_id,
                provider_kind=entry.provider_kind,
                capabilities=entry.capabilities,
                lifecycle=ProviderLifecycle.NOT_INSTALLED,
                install_source=entry.install_source,
                install_command=entry.install_command,
                authority=entry.authority,
                optional=entry.optional,
                hard_dependency=(
                    False if entry.provider_kind is ProviderKind.GATEWAY else entry.hard_dependency
                ),
            )
        )
    return tuple(placeholders)


def _merge_with_catalog(
    providers: Sequence[DiscoveredProvider], scope: BootstrapScope
) -> tuple[DiscoveredProvider, ...]:
    by_id = {item.provider_id: item for item in _catalog_placeholders(scope)}
    for item in providers:
        by_id[item.provider_id] = item
    return tuple(sorted(by_id.values(), key=lambda item: item.provider_id))


def _is_usable(provider: DiscoveredProvider) -> bool:
    return (
        provider.lifecycle is ProviderLifecycle.HEALTHY
        and provider.health_state == "healthy"
        and provider.qualification_state == "qualified"
    )


def _candidates_for_capability(
    capability_id: str, providers: Sequence[DiscoveredProvider]
) -> tuple[DiscoveredProvider, ...]:
    matched = [item for item in providers if capability_id in item.capabilities]
    return tuple(sorted(matched, key=lambda item: (-item.authority, item.provider_id)))


def recommend_capabilities(
    providers: Sequence[DiscoveredProvider],
    *,
    registry: SemanticCapabilityRegistry | None = None,
    parity_by_provider: Mapping[str, str] | None = None,
) -> tuple[CapabilityRecommendation, ...]:
    """Recommend by capability first; rank provider candidates second."""

    active = registry
    recommendations: list[CapabilityRecommendation] = []
    parity = parity_by_provider or {}

    for capability_id in _BOOTSTRAP_CAPABILITIES:
        candidates = _candidates_for_capability(capability_id, providers)
        usable = tuple(item for item in candidates if _is_usable(item))
        selected: DiscoveredProvider | None = usable[0] if usable else None

        # Prefer BOD-87 resolution when vocabulary knows the capability.
        decision: ResolveDecision | None = None
        try:
            decision = resolve_capability(capability_id, registry=active)
        except Exception:
            decision = None

        candidate_ids_extra: tuple[str, ...] = ()
        if selected is None and decision is not None and decision.selected is not None:
            # Map registry selection onto discovered providers when present.
            for item in candidates:
                if item.provider_id == decision.selected.provider_id and _is_usable(item):
                    selected = item
                    break
            # Healthy registry natives (memory/security) may not appear in PATH catalog.
            if (
                selected is None
                and decision.selected.provider_id.startswith("native.")
                and decision.selected.health == "healthy"
            ):
                if (
                    "memory" in decision.selected.provider_id
                    or "ast" in decision.selected.provider_id
                ):
                    kind = ProviderKind.INTELLIGENCE
                elif "security" in decision.selected.provider_id:
                    kind = ProviderKind.SECURITY
                else:
                    kind = ProviderKind.OTHER
                selected = DiscoveredProvider(
                    provider_id=decision.selected.provider_id,
                    provider_kind=kind,
                    capabilities=frozenset({capability_id}),
                    lifecycle=ProviderLifecycle.HEALTHY,
                    auth_state="not_required",
                    health_state="healthy",
                    qualification_state="qualified",
                    authority=decision.selected.authority_rank,
                    freshness="fresh",
                    install_source="verdict-native",
                    optional=False,
                    hard_dependency=False,
                )
                candidate_ids_extra = (selected.provider_id,)

        candidate_ids = tuple(
            dict.fromkeys((*candidate_ids_extra, *(item.provider_id for item in candidates)))
        )
        # Prefer recommending OmniRoute among missing gateway providers, never hard-dep.
        if capability_id.startswith("gateway.") and "gateway.omniroute" in candidate_ids:
            candidate_ids = (
                "gateway.omniroute",
                *(item for item in candidate_ids if item != "gateway.omniroute"),
            )

        if selected is not None:
            status = "covered"
            reason = f"Satisfied by healthy provider {selected.provider_id}."
        elif any(item.lifecycle is not ProviderLifecycle.NOT_INSTALLED for item in candidates):
            status = "degraded"
            reason = (
                "Provider present but not healthy/qualified "
                "(installed != healthy); prefer reuse after repair or choose an alternative."
            )
        else:
            status = "missing"
            reason = "No healthy provider covers this capability; optional enrichments available."

        recommendations.append(
            CapabilityRecommendation(
                capability_id=capability_id,
                status=status,
                selected_provider_id=selected.provider_id if selected else None,
                candidate_providers=candidate_ids,
                reason=reason,
                optional=True,
                hard_dependency=False,
                parity=parity.get(selected.provider_id) if selected else None,
            )
        )

    return tuple(recommendations)


def _action_for_missing(
    capability_id: str, providers: Sequence[DiscoveredProvider]
) -> BootstrapAction | None:
    candidates = _candidates_for_capability(capability_id, providers)
    unhealthy = [
        item
        for item in candidates
        if item.lifecycle is not ProviderLifecycle.NOT_INSTALLED and not _is_usable(item)
    ]
    # Prefer repairing a present-but-unhealthy provider over installing a duplicate.
    if unhealthy:
        target = unhealthy[0]
        return BootstrapAction(
            action_id=f"bootstrap:repair:{target.provider_id}",
            kind="repair_provider",
            target=target.provider_id,
            description=f"Repair {target.provider_id} for capability {capability_id}.",
            reason=target.failure_reason or "installed != healthy",
            security_impact="No new install; diagnoses existing provider without secret access.",
            postcondition="Provider either becomes healthy/qualified or remains explicitly degraded.",
            undo="No mutation in repair planning; stop probing.",
            reversible=True,
            requires_consent=False,
            provider_id=target.provider_id,
            capability_ids=(capability_id,),
            trust_warning="Do not treat binary presence as health.",
        )

    missing = [
        item
        for item in candidates
        if item.lifecycle is ProviderLifecycle.NOT_INSTALLED and item.install_command
    ]
    if not missing:
        return None

    # Prefer OmniRoute as recommended gateway option when relevant.
    preferred = next(
        (item for item in missing if item.provider_id == "gateway.omniroute"), missing[0]
    )
    return BootstrapAction(
        action_id=f"bootstrap:install:{preferred.provider_id}",
        kind="install_provider",
        target=preferred.provider_id,
        description=(
            f"Install {preferred.provider_id} to cover capability {capability_id} "
            f"(source: {preferred.install_source or 'upstream'})."
        ),
        reason=f"Capability {capability_id} is missing a healthy provider.",
        security_impact=(
            "Third-party install; runs upstream package/command after explicit consent. "
            "Credentials are never copied."
        ),
        postcondition=(
            f"{preferred.provider_id} is installed; health/qualification still require certify."
        ),
        undo="Uninstall via upstream docs; remove Verdict ownership markers for this action.",
        reversible=True,
        requires_consent=True,
        provider_id=preferred.provider_id,
        capability_ids=(capability_id,),
        install_command=preferred.install_command,
        install_source=preferred.install_source,
        files_changed=(),
        privilege="user",
        trust_warning="Review upstream source before install; OmniRoute is optional.",
    )


def build_bootstrap_plan(
    providers: Sequence[DiscoveredProvider],
    recommendations: Sequence[CapabilityRecommendation],
    *,
    mode: BootstrapMode,
    scope: BootstrapScope,
    recommended_only: bool = False,
) -> UnifiedBootstrapPlan:
    """Build one unified mutation plan from recommendations."""

    actions: list[BootstrapAction] = []
    seen: set[str] = set()
    for recommendation in recommendations:
        if recommendation.status == "covered":
            continue
        if recommended_only and recommendation.capability_id not in {
            "code.graph",
            "code.symbols",
            "code.definitions",
            "docs.lookup",
            "gateway.inventory",
            "gateway.execute",
            "harness.execute",
            "memory.search",
        }:
            continue
        action = _action_for_missing(recommendation.capability_id, providers)
        if action is None or action.action_id in seen:
            continue
        # Deduplicate installs by provider.
        if action.provider_id:
            provider_key = f"provider:{action.provider_id}:{action.kind}"
            if provider_key in seen:
                continue
            seen.add(provider_key)
        seen.add(action.action_id)
        actions.append(action)

    actions_sorted = tuple(sorted(actions, key=lambda item: item.action_id))
    return UnifiedBootstrapPlan(
        actions=actions_sorted,
        mutation_free=True,
        scope=scope,
        mode=mode if mode is not BootstrapMode.APPLY else BootstrapMode.PLAN,
    )


def _ownership_path(state_dir: Path) -> Path:
    return state_dir / "ownership.json"


def _load_ownership(state_dir: Path) -> dict[str, Any]:
    path = _ownership_path(state_dir)
    if not path.is_file():
        return {"schema_version": _OWNERSHIP_SCHEMA, "managed_actions": [], "backups": {}}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return {"schema_version": _OWNERSHIP_SCHEMA, "managed_actions": [], "backups": {}}
    return dict(raw)


def _save_ownership(state_dir: Path, payload: Mapping[str, Any]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    _ownership_path(state_dir).write_text(
        json.dumps(dict(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _successful_apply_result(result: Mapping[str, Any]) -> bool:
    """True only when an installer actually completed — not record-only stubs."""

    status = str(result.get("status") or "").lower()
    return status in {"installed", "ok", "applied", "success", "configured"}


def _already_applied(ownership: Mapping[str, Any], action_id: str) -> bool:
    managed = ownership.get("managed_actions", [])
    for item in managed:
        if not isinstance(item, dict) or item.get("action_id") != action_id:
            continue
        result = item.get("result")
        if isinstance(result, Mapping) and _successful_apply_result(result):
            return True
    return False


def _allowlist_matches(action: BootstrapAction, allow: frozenset[str]) -> bool:
    """True when allowlist explicitly names this action's provider_id or action_id."""

    if action.provider_id is not None and action.provider_id in allow:
        return True
    return action.action_id in allow


@dataclass(frozen=True)
class ConsentAuthorization:
    """Result of the unified APPLY consent / allowlist gate."""

    authorized: tuple[BootstrapAction, ...]
    blocked: tuple[str, ...]
    allowlist: tuple[str, ...]
    non_interactive: bool
    consent: bool

    @property
    def fully_blocked(self) -> bool:
        return bool(self.blocked) and not self.authorized


def authorize_bootstrap_actions(
    actions: Sequence[BootstrapAction],
    *,
    consent: bool,
    allowlist: Sequence[str],
    non_interactive: bool,
) -> ConsentAuthorization:
    """Authorize pending APPLY actions with one shared consent/allowlist policy.

    Interactive (``non_interactive=False``): ``consent=True`` authorizes every
    remaining action (final yes for the shown plan). An allowlist may also
    authorize a subset without blanket consent.

    Non-interactive: only an explicit allowlist entry (``provider_id`` or
    ``action_id``) authorizes a mutation. ``consent=True`` alone never opens
    the gate — CI/headless flows must name providers via ``--allow``.
    """

    allow = frozenset(allowlist)
    authorized: list[BootstrapAction] = []
    blocked: list[str] = []
    # Blanket interactive consent applies only when not headless/CI.
    interactive_consent = bool(consent) and not non_interactive
    for action in actions:
        if _allowlist_matches(action, allow) or interactive_consent:
            authorized.append(action)
        else:
            blocked.append(action.action_id)
    return ConsentAuthorization(
        authorized=tuple(authorized),
        blocked=tuple(blocked),
        allowlist=tuple(sorted(allow)),
        non_interactive=non_interactive,
        consent=consent,
    )


def _consent_blocked_hint(*, non_interactive: bool, consent: bool) -> str:
    if non_interactive:
        return (
            "Non-interactive APPLY requires --allow provider ids "
            "(e.g. gateway.omniroute); --yes alone is insufficient."
        )
    if consent:
        return "Pass allowlist provider ids to authorize a subset without full-plan consent."
    return (
        "Pass consent=True (interactive --yes) or allowlist provider ids (e.g. gateway.omniroute)."
    )


def apply_bootstrap_actions(
    plan: UnifiedBootstrapPlan,
    *,
    state_dir: Path,
    consent: bool,
    allowlist: Sequence[str],
    non_interactive: bool,
    decline_optional: bool = False,
    install_runner: InstallRunner | None = None,
    install_command_runner: InstallCommandRunner | None = None,
    path_resolver: PathResolver | None = None,
) -> tuple[StageResult, StageResult, dict[str, object]]:
    """Consent gate + idempotent apply with backups and ownership tracking."""

    pending = [action for action in plan.actions if action.requires_consent]
    noop = not pending or decline_optional

    if noop:
        consent_stage = StageResult(
            stage=StageName.CONSENT.value,
            status="skipped",
            summary="No consent required for empty/declined optional plan.",
            details={"declined_optional": decline_optional},
        )
        apply_stage = StageResult(
            stage=StageName.APPLY.value, status="ok", summary="No mutations applied.", details={}
        )
        return consent_stage, apply_stage, {"mutated": False, "actions": []}

    ownership = _load_ownership(state_dir)
    remaining = [action for action in pending if not _already_applied(ownership, action.action_id)]
    if not remaining:
        consent_stage = StageResult(
            stage=StageName.CONSENT.value,
            status="skipped",
            summary="Idempotent re-run: ownership shows actions already applied.",
            details={"already_applied": [action.action_id for action in pending]},
        )
        apply_stage = StageResult(
            stage=StageName.APPLY.value,
            status="ok",
            summary="No new mutations; ownership unchanged.",
            details={},
        )
        return consent_stage, apply_stage, {"mutated": False, "actions": [], "idempotent": True}

    decision = authorize_bootstrap_actions(
        remaining, consent=consent, allowlist=allowlist, non_interactive=non_interactive
    )
    authorized = list(decision.authorized)
    blocked = list(decision.blocked)

    if decision.fully_blocked:
        summary = (
            "APPLY blocked: non-interactive allowlist required."
            if non_interactive
            else "APPLY blocked: consent or allowlist required."
        )
        if non_interactive and consent:
            summary = (
                "APPLY blocked: non-interactive mode ignores blanket consent; "
                "pass an explicit allowlist."
            )
        consent_stage = StageResult(
            stage=StageName.CONSENT.value,
            status="blocked",
            summary=summary,
            details={
                "blocked_actions": blocked,
                "non_interactive": non_interactive,
                "consent": consent,
                "allowlist": list(decision.allowlist),
                "hint": _consent_blocked_hint(non_interactive=non_interactive, consent=consent),
            },
        )
        apply_stage = StageResult(
            stage=StageName.APPLY.value,
            status="skipped",
            summary="No mutations; consent gate blocked APPLY.",
            details={},
        )
        return consent_stage, apply_stage, {"mutated": False, "actions": [], "blocked": blocked}

    consent_stage = StageResult(
        stage=StageName.CONSENT.value,
        status="ok",
        summary="Final consent granted for authorized actions.",
        details={
            "authorized": [action.action_id for action in authorized],
            "blocked": blocked,
            "allowlist": list(decision.allowlist),
            "non_interactive": non_interactive,
            "consent": consent,
            "policy": ("interactive_consent" if consent and not non_interactive else "allowlist"),
        },
    )

    backup_dir = state_dir / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    applied: list[dict[str, object]] = []
    # Consent/allowlist already enforced above. Default runner only executes
    # documented safe providers (gateway.omniroute); others stay blocked.
    runner = install_runner or build_default_install_runner(
        state_dir=state_dir, command_runner=install_command_runner, path_resolver=path_resolver
    )

    for action in authorized:
        backup_path = backup_dir / f"{action.action_id.replace(':', '__')}.json"
        backup_payload = {
            "action": action.to_dict(),
            "note": "pre-apply snapshot of planned mutation",
        }
        backup_path.write_text(
            json.dumps(backup_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        ownership.setdefault("backups", {})[action.action_id] = str(backup_path)

        result: Mapping[str, Any]
        if action.kind == "install_provider":
            result = runner(action)
        else:
            result = {"status": "recorded", "kind": action.kind}

        if action.kind == "install_provider" and not _successful_apply_result(result):
            applied.append({"action_id": action.action_id, "result": dict(result)})
            continue

        ownership.setdefault("managed_actions", []).append(
            {
                "action_id": action.action_id,
                "provider_id": action.provider_id,
                "kind": action.kind,
                "result": dict(result),
            }
        )
        applied.append({"action_id": action.action_id, "result": dict(result)})

    _save_ownership(state_dir, ownership)
    succeeded: list[dict[str, object]] = []
    runner_blocked: list[dict[str, object]] = []
    runner_failed: list[dict[str, object]] = []
    for item in applied:
        result_obj = item.get("result")
        if not isinstance(result_obj, dict):
            continue
        result_map: Mapping[str, Any] = result_obj
        if _successful_apply_result(result_map):
            succeeded.append(item)
            continue
        status = str(result_map.get("status") or "").lower()
        if status == "blocked":
            runner_blocked.append(item)
        elif status in {"failed", "error"}:
            runner_failed.append(item)
    if (runner_blocked or runner_failed) and not succeeded:
        if runner_failed and not runner_blocked:
            summary = "APPLY failed: install command did not prove success."
            status = "failed"
        else:
            summary = (
                "APPLY blocked: no install runner for unauthorized/unsupported "
                "third-party installs."
            )
            status = "blocked"
        apply_stage = StageResult(
            stage=StageName.APPLY.value,
            status=status,
            summary=summary,
            details={
                "blocked": [item["action_id"] for item in runner_blocked],
                "failed": [item["action_id"] for item in runner_failed],
                "applied": [],
            },
        )
        return consent_stage, apply_stage, {"mutated": False, "actions": applied}

    apply_stage = StageResult(
        stage=StageName.APPLY.value,
        status="ok" if not runner_blocked and not runner_failed else "partial",
        summary=(
            f"Applied {len(succeeded)} Verdict-owned action(s) with backups."
            if succeeded
            else "No successful mutations applied."
        ),
        details={
            "applied": [item["action_id"] for item in succeeded],
            "blocked": [item["action_id"] for item in runner_blocked],
            "failed": [item["action_id"] for item in runner_failed],
        },
    )
    return consent_stage, apply_stage, {"mutated": bool(succeeded), "actions": applied}


UninstallRunner = Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]]


def _remove_verdict_owned_marker(result: Mapping[str, Any], *, state_dir: Path) -> bool:
    """Delete a marker file recorded under state_dir during APPLY (Verdict-owned only)."""

    marker = result.get("marker")
    if not isinstance(marker, str) or not marker:
        return False
    path = Path(marker)
    try:
        path.resolve().relative_to(state_dir.resolve())
    except ValueError:
        return False
    if path.is_file():
        path.unlink()
        return True
    return False


def rollback_bootstrap_actions(
    *,
    state_dir: Path,
    action_ids: Sequence[str] | None = None,
    uninstall_runner: UninstallRunner | None = None,
) -> StageResult:
    """Reverse Verdict-owned bootstrap APPLY records using ownership + backups.

    Presentation-free: returns a semantic StageResult for doctor/CLI/TUI consumers.
    Third-party package uninstall only runs when ``uninstall_runner`` is supplied;
    otherwise install actions surface ``manual_undo_required`` with the planned undo
    text and keep ownership until a runner succeeds or the action is ownership-only.
    """

    ownership = _load_ownership(state_dir)
    managed = [item for item in ownership.get("managed_actions", []) if isinstance(item, dict)]
    wanted = frozenset(action_ids) if action_ids is not None else None
    selected = [
        item for item in managed if wanted is None or str(item.get("action_id") or "") in wanted
    ]
    if not selected:
        return StageResult(
            stage=StageName.ROLLBACK.value,
            status="ok",
            summary="No Verdict-owned bootstrap actions to roll back.",
            details={"rolled_back": [], "manual_undo_required": [], "failed": []},
        )

    backups = ownership.get("backups", {})
    if not isinstance(backups, dict):
        backups = {}

    rolled_back: list[str] = []
    manual: list[dict[str, object]] = []
    failed: list[dict[str, object]] = []
    remaining: list[dict[str, object]] = []
    selected_ids = {str(item.get("action_id") or "") for item in selected}

    for item in managed:
        action_id = str(item.get("action_id") or "")
        if action_id not in selected_ids:
            remaining.append(item)
            continue

        result_obj = item.get("result")
        result_map: Mapping[str, Any] = result_obj if isinstance(result_obj, Mapping) else {}
        kind = str(item.get("kind") or "")
        backup_path_raw = backups.get(action_id)
        undo_text = "Remove Verdict ownership markers for this action."
        action_payload: Mapping[str, Any] = {}
        if isinstance(backup_path_raw, str) and backup_path_raw:
            backup_file = Path(backup_path_raw)
            if backup_file.is_file():
                try:
                    backup_raw = json.loads(backup_file.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    backup_raw = {}
                if isinstance(backup_raw, dict):
                    action_obj = backup_raw.get("action")
                    if isinstance(action_obj, dict):
                        action_payload = action_obj
                        undo_text = str(action_obj.get("undo") or undo_text)

        if kind == "install_provider":
            if uninstall_runner is not None:
                try:
                    undo_result = uninstall_runner(item, action_payload)
                except Exception as exc:
                    failed.append({"action_id": action_id, "status": "failed", "error": str(exc)})
                    remaining.append(item)
                    continue
                if not _successful_apply_result(undo_result):
                    failed.append(
                        {
                            "action_id": action_id,
                            "status": str(undo_result.get("status") or "failed"),
                            "result": dict(undo_result),
                        }
                    )
                    remaining.append(item)
                    continue
                _remove_verdict_owned_marker(result_map, state_dir=state_dir)
                rolled_back.append(action_id)
                continue

            if _remove_verdict_owned_marker(result_map, state_dir=state_dir):
                rolled_back.append(action_id)
                continue

            manual.append(
                {
                    "action_id": action_id,
                    "status": "manual_undo_required",
                    "undo": undo_text,
                    "provider_id": item.get("provider_id"),
                }
            )
            remaining.append(item)
            continue

        # Ownership-only / recorded actions: drop Verdict management immediately.
        _remove_verdict_owned_marker(result_map, state_dir=state_dir)
        rolled_back.append(action_id)

    ownership["managed_actions"] = remaining
    _save_ownership(state_dir, ownership)

    if failed and not rolled_back:
        status = "failed"
        summary = "Rollback failed for all selected Verdict-owned actions."
    elif failed or manual:
        status = "partial"
        summary = (
            f"Rolled back {len(rolled_back)} action(s); "
            f"{len(manual)} need manual undo; {len(failed)} failed."
        )
    else:
        status = "ok"
        summary = f"Rolled back {len(rolled_back)} Verdict-owned bootstrap action(s)."

    return StageResult(
        stage=StageName.ROLLBACK.value,
        status=status,
        summary=summary,
        details={
            "rolled_back": rolled_back,
            "manual_undo_required": manual,
            "failed": failed,
            "remaining_managed": [
                str(item.get("action_id") or "") for item in remaining if item.get("action_id")
            ],
        },
    )


def run_bootstrap(
    *,
    providers: Sequence[DiscoveredProvider] | DiscoveredProvider | None = None,
    mode: BootstrapMode | str = BootstrapMode.PLAN,
    scope: BootstrapScope | str = BootstrapScope.ALL,
    non_interactive: bool = False,
    allowlist: Sequence[str] = (),
    consent: bool = False,
    decline_optional: bool = False,
    state_dir: Path | str | None = None,
    certifier: Certifier | None = None,
    install_runner: InstallRunner | None = None,
    install_command_runner: InstallCommandRunner | None = None,
    path_resolver: PathResolver | None = None,
    probe_gateway: GatewayProbe | None = None,
    registry: SemanticCapabilityRegistry | None = None,
    certify_now: datetime | None = None,
) -> BootstrapReport:
    """Run the staged bootstrap pipeline and return a machine-readable report."""

    mode_v = BootstrapMode(mode) if not isinstance(mode, BootstrapMode) else mode
    scope_v = BootstrapScope(scope) if not isinstance(scope, BootstrapScope) else scope
    stages: list[StageResult] = []

    # DISCOVER
    if providers is None:
        discovered = discover_providers(
            path_resolver=path_resolver, probe_gateway=probe_gateway, scope=scope_v
        )
        stages.append(
            StageResult(
                stage=StageName.DISCOVER.value,
                status="ok",
                summary=f"Discovered {len(discovered)} catalog provider(s).",
                details={"count": len(discovered)},
            )
        )
    else:
        discovered = (providers,) if isinstance(providers, DiscoveredProvider) else tuple(providers)
        discovered = _filter_scope(discovered, scope_v)
        stages.append(
            StageResult(
                stage=StageName.DISCOVER.value,
                status="ok",
                summary=f"Using {len(discovered)} injected provider observation(s).",
                details={"count": len(discovered), "injected": True},
            )
        )

    # NORMALIZE: de-dupe and fill catalog placeholders so recommendations stay capability-complete
    normalized = _merge_with_catalog(discovered, scope_v)
    stages.append(
        StageResult(
            stage=StageName.NORMALIZE.value,
            status="ok",
            summary=f"Normalized {len(normalized)} provider record(s).",
            details={"provider_ids": [item.provider_id for item in normalized]},
        )
    )

    # RECOMMEND
    recommendations = recommend_capabilities(normalized, registry=registry)
    stages.append(
        StageResult(
            stage=StageName.RECOMMEND.value,
            status="ok",
            summary=(
                f"{sum(1 for item in recommendations if item.status == 'missing')} missing, "
                f"{sum(1 for item in recommendations if item.status == 'degraded')} degraded, "
                f"{sum(1 for item in recommendations if item.status == 'covered')} covered."
            ),
            details={"count": len(recommendations)},
        )
    )

    # PREFLIGHT + unified plan
    recommended_only = mode_v is BootstrapMode.RECOMMENDED
    plan = build_bootstrap_plan(
        normalized, recommendations, mode=mode_v, scope=scope_v, recommended_only=recommended_only
    )
    stages.append(
        StageResult(
            stage=StageName.PREFLIGHT.value,
            status="ok",
            summary=f"Unified plan with {len(plan.actions)} action(s); digest {plan.digest}.",
            details={"plan_digest": plan.digest, "action_ids": [a.action_id for a in plan.actions]},
        )
    )

    apply_payload: dict[str, object] = {"mutated": False, "actions": []}
    resolved_state = (
        Path(state_dir) if state_dir is not None else Path.home() / ".verdict" / "bootstrap"
    )

    if mode_v is BootstrapMode.APPLY:
        consent_stage, apply_stage, apply_payload = apply_bootstrap_actions(
            plan,
            state_dir=resolved_state,
            consent=consent,
            allowlist=allowlist,
            non_interactive=non_interactive,
            decline_optional=decline_optional,
            install_runner=install_runner,
            install_command_runner=install_command_runner,
            path_resolver=path_resolver,
        )
        stages.extend([consent_stage, apply_stage])
    else:
        stages.append(
            StageResult(
                stage=StageName.CONSENT.value,
                status="skipped",
                summary="Plan/recommended mode: consent deferred until APPLY.",
                details={},
            )
        )
        stages.append(
            StageResult(
                stage=StageName.APPLY.value,
                status="skipped",
                summary="Mutation-free mode; no APPLY.",
                details={},
            )
        )

    # CERTIFY — refresh provider observations after successful APPLY so newly
    # installed providers are not certified from stale pre-apply snapshots.
    providers_for_cert = normalized
    if mode_v is BootstrapMode.APPLY and bool(apply_payload.get("mutated")):
        applied_obj = apply_payload.get("actions", [])
        applied_actions: list[Any] = list(applied_obj) if isinstance(applied_obj, list) else []
        installed_ids: set[str] = set()
        for item in applied_actions:
            if not isinstance(item, dict):
                continue
            result = item.get("result")
            if not isinstance(result, dict) or not _successful_apply_result(result):
                continue
            action_id = str(item.get("action_id") or "")
            if action_id.startswith("bootstrap:install:") or action_id.startswith(
                "bootstrap:repair:"
            ):
                installed_ids.add(action_id.rsplit(":", 1)[-1])

        if providers is None:
            providers_for_cert = _filter_scope(
                discover_providers(
                    path_resolver=path_resolver, probe_gateway=probe_gateway, scope=scope_v
                ),
                scope_v,
            )
        elif installed_ids:
            refreshed: list[DiscoveredProvider] = []
            for item in normalized:
                if item.provider_id in installed_ids and item.lifecycle in {
                    ProviderLifecycle.NOT_INSTALLED,
                    ProviderLifecycle.INSTALLED,
                }:
                    refreshed.append(
                        DiscoveredProvider(
                            provider_id=item.provider_id,
                            provider_kind=item.provider_kind,
                            capabilities=item.capabilities,
                            lifecycle=ProviderLifecycle.INSTALLED,
                            path_or_endpoint=item.path_or_endpoint,
                            config_sources=item.config_sources,
                            auth_state=item.auth_state,
                            health_state="unknown",
                            qualification_state="unqualified",
                            authority=item.authority,
                            freshness="fresh",
                            managed_by_verdict=True,
                            install_source=item.install_source,
                            install_command=item.install_command,
                            last_probe=item.last_probe,
                            failure_reason="installed; awaiting live certification",
                            optional=item.optional,
                            hard_dependency=item.hard_dependency,
                        )
                    )
                else:
                    refreshed.append(item)
            providers_for_cert = tuple(refreshed)

    if certifier is None:

        def _wired_certifier(providers: tuple[DiscoveredProvider, ...]) -> Mapping[str, Any]:
            return _default_certifier(providers, now=certify_now)

        certify: Certifier = _wired_certifier
    else:
        certify = certifier
    certification = dict(certify(providers_for_cert))
    cert_summary = str(certification.get("summary") or "certification complete")
    if any(
        isinstance(item, dict) and item.get("reason") == "installed != healthy"
        for item in certification.get("results", [])
    ):
        cert_summary = "installed != healthy; unhealthy providers not certified"
    stages.append(
        StageResult(
            stage=StageName.CERTIFY.value,
            status="ok",
            summary=cert_summary,
            details={"result_count": len(list(certification.get("results", [])))},
        )
    )

    mutation_free = mode_v is not BootstrapMode.APPLY or not bool(apply_payload.get("mutated"))
    return BootstrapReport(
        stages=tuple(stages),
        providers=providers_for_cert,
        recommendations=recommendations,
        plan=plan,
        apply=apply_payload,
        certification=certification,
        mutation_free=mutation_free,
    )


def doctor_capability_report(
    *,
    providers: Sequence[DiscoveredProvider] | None = None,
    registry: SemanticCapabilityRegistry | None = None,
) -> dict[str, object]:
    """Capability → selected provider → authority → health for ``verdict doctor``."""

    report = run_bootstrap(
        providers=providers, mode=BootstrapMode.PLAN, non_interactive=True, registry=registry
    )
    active = registry if registry is not None else build_default_registry()
    rows: list[dict[str, object]] = []
    for recommendation in report.recommendations:
        selected = None
        authority = None
        health = None
        if recommendation.selected_provider_id:
            match = next(
                (
                    item
                    for item in report.providers
                    if item.provider_id == recommendation.selected_provider_id
                ),
                None,
            )
            if match is not None:
                selected = match.provider_id
                authority = match.authority
                health = match.health_state
        # Augment with BOD-87 registry view when available.
        try:
            decision = resolve_capability(recommendation.capability_id, registry=active)
            if decision.selected is not None and selected is None:
                selected = decision.selected.provider_id
                authority = decision.selected.authority_rank
                health = decision.selected.health
        except Exception:
            pass
        rows.append(
            {
                "capability_id": recommendation.capability_id,
                "status": recommendation.status,
                "selected_provider_id": selected,
                "authority": authority,
                "health": health,
                "fallback_candidates": list(recommendation.candidate_providers),
                "reason": recommendation.reason,
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "doctor_capability_report",
        "capabilities": rows,
        "providers": [item.to_dict() for item in report.providers],
    }


__all__ = [
    "SCHEMA_VERSION",
    "BootstrapAction",
    "BootstrapMode",
    "BootstrapReport",
    "BootstrapScope",
    "CapabilityRecommendation",
    "Certifier",
    "ConsentAuthorization",
    "DiscoveredProvider",
    "ProviderKind",
    "ProviderLifecycle",
    "StageResult",
    "UnifiedBootstrapPlan",
    "apply_bootstrap_actions",
    "authorize_bootstrap_actions",
    "build_bootstrap_plan",
    "build_default_install_runner",
    "discover_providers",
    "doctor_capability_report",
    "recommend_capabilities",
    "rollback_bootstrap_actions",
    "run_bootstrap",
    "run_default_provider_install",
]
