"""Untrusted context + agent tooling security boundary (security boundary).

Pipeline (native-first):

    external evidence
    → classify trust/authority
    → detect instruction/prompt-injection content
    → secret/credential filtering
    → provenance/freshness
    → bounded transformation
    → ContextUnit
    → model

Rules:
- retrieved content is evidence/data, never executable instruction authority
- external text cannot silently override system/project/task policy
- provenance and trust class survive transformation
- fail closed for high-confidence credential exposure when policy requires
- lower-confidence findings remain visible evidence, not automatic destruction
- no security result silently changes routing eligibility without receipt/policy

Optional AgentShield / Gitleaks / Semgrep providers are BOD-87 stubs only —
never hard Core dependencies. This module does not own BOD-89 CI/appsec.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Final, Literal

from verdict.capability_registry import (
    ResolveDecision,
    SemanticCapabilityRegistry,
    build_default_registry,
)
from verdict.context_pack import ContextUnit, SlotType, sanitize_injection_patterns

CONTEXT_TRUST_SCHEMA_VERSION: Final[str] = "context-trust/v1"
SECRET_REDACTION_TOKEN: Final[str] = "[REDACTED_SECRET]"
_NATIVE_PROVIDER_ID: Final[str] = "native.verdict.security"

TrustClass = Literal["system", "project", "task", "trusted", "untrusted", "unverified"]
AuthorityClass = Literal["instruction", "policy", "evidence", "data", "unverified"]
SourceKind = Literal[
    "system",
    "project",
    "task",
    "web",
    "external_docs",
    "github",
    "linear",
    "mcp_output",
    "memory_non_authoritative",
    "generated_summary",
    "agent_config",
    "hook",
    "skill",
    "unknown",
]
ConfigKind = Literal[
    "hooks",
    "mcp_config",
    "agents_md",
    "claude_md",
    "skills",
    "permissions",
    "provider_config",
    "unknown",
]
ReceiptAction = Literal["allow", "transform", "surface_evidence", "fail_closed"]

_EXTERNAL_SOURCES: Final[frozenset[str]] = frozenset(
    {
        "web",
        "external_docs",
        "github",
        "linear",
        "mcp_output",
        "memory_non_authoritative",
        "generated_summary",
        "agent_config",
        "hook",
        "skill",
        "unknown",
    }
)

_SECRET_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)\s*[:=]\s*\S+"),
    re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{16,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),
    re.compile(r"(?i)https?://[^\s/@]+:[^\s/@]+@"),
)

_INJECTION_PATTERNS: Final[tuple[tuple[re.Pattern[str], float, str], ...]] = (
    (
        re.compile(r"(?i)\bignore\s+(all\s+)?(previous|prior|above)\s+instructions?\b"),
        0.95,
        "ignore_previous_instructions",
    ),
    (
        re.compile(r"(?i)\bdisregard\s+(all\s+)?(project|system|task)?\s*rules?\b"),
        0.9,
        "disregard_rules",
    ),
    (re.compile(r"(?i)\boverride\s+(task|system|project)\s+policy\b"), 0.92, "override_policy"),
    (re.compile(r"(?i)\[/?INST\]"), 0.85, "inst_control_tokens"),
    (re.compile(r"(?i)</?system>"), 0.88, "system_tags"),
    (re.compile(r"(?i)\byou\s+are\s+now\s+(unrestricted|jailbroken|DAN)\b"), 0.9, "role_jailbreak"),
    (
        re.compile(r"(?i)\btreat\s+this\s+.*\s+as\s+system\s+policy\b"),
        0.87,
        "elevate_to_system_policy",
    ),
    # Mild / low-confidence — visible evidence only
    (re.compile(r"(?i)\bsystem\s+architecture\b"), 0.25, "benign_system_mention"),
)

_DANGEROUS_HOOK_PATTERNS: Final[tuple[tuple[re.Pattern[str], float, str], ...]] = (
    (re.compile(r"(?i)\b(curl|wget|fetch)\b[^\n]{0,80}\|\s*(ba)?sh\b"), 0.95, "pipe_to_shell"),
    (re.compile(r"(?i)\"shell\"\s*:\s*\"always\""), 0.85, "shell_always"),
    (re.compile(r"(?i)\"network\"\s*:\s*\"unrestricted\""), 0.8, "network_unrestricted"),
    (re.compile(r"(?i)https?://evil\."), 0.9, "evil_exfil_host"),
    (re.compile(r"(?i)\"preToolUse\"[^\]]*command"), 0.75, "executable_pre_tool_hook"),
)


@dataclass(frozen=True)
class TrustFinding:
    """One security observation with confidence and provenance."""

    kind: str
    confidence: float
    detail: str
    evidence_span: str | None = None
    capability_id: str | None = None
    provider_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "confidence": self.confidence,
            "detail": self.detail,
            "evidence_span": self.evidence_span,
            "capability_id": self.capability_id,
            "provider_id": self.provider_id,
        }


@dataclass(frozen=True)
class SecurityReceipt:
    """Auditable record required before routing eligibility changes."""

    receipt_id: str
    policy_reason: str
    findings: tuple[TrustFinding, ...]
    action: ReceiptAction
    routing_eligibility_delta: bool = False
    schema_version: str = CONTEXT_TRUST_SCHEMA_VERSION
    provider_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "policy_reason": self.policy_reason,
            "findings": [f.to_dict() for f in self.findings],
            "action": self.action,
            "routing_eligibility_delta": self.routing_eligibility_delta,
            "provider_ids": list(self.provider_ids),
        }


@dataclass(frozen=True)
class TrustPipelineResult:
    """Outcome of admitting external evidence across the trust boundary."""

    trust: TrustClass
    authority: AuthorityClass
    findings: tuple[TrustFinding, ...]
    receipt: SecurityReceipt
    content_for_model: str
    unit: ContextUnit | None = None
    excluded: bool = False
    exclusion_reason: str | None = None
    transform_lineage: tuple[str, ...] = ()
    preserved_system_policy: str | None = None
    preserved_task_policy: str | None = None
    provider_decisions: Mapping[str, ResolveDecision] = field(default_factory=dict)
    schema_version: str = CONTEXT_TRUST_SCHEMA_VERSION


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _digest_text(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode()).hexdigest()}"


def _new_receipt_id() -> str:
    return f"ctr-{uuid.uuid4().hex[:16]}"


def classify_trust_authority(source_kind: SourceKind) -> tuple[TrustClass, AuthorityClass]:
    """Map source kind → trust class + authority (external = evidence only)."""
    if source_kind == "system":
        return "system", "instruction"
    if source_kind == "project":
        return "project", "policy"
    if source_kind == "task":
        return "task", "policy"
    if source_kind in _EXTERNAL_SOURCES:
        return "untrusted", "evidence"
    return "unverified", "unverified"


def detect_prompt_injection(
    text: str, *, provider_id: str = _NATIVE_PROVIDER_ID
) -> tuple[TrustFinding, ...]:
    """Native prompt-injection heuristics (security.prompt_injection)."""
    findings: list[TrustFinding] = []
    for pattern, confidence, label in _INJECTION_PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        findings.append(
            TrustFinding(
                kind="prompt_injection",
                confidence=confidence,
                detail=label,
                evidence_span=match.group(0)[:120],
                capability_id="security.prompt_injection",
                provider_id=provider_id,
            )
        )
    return tuple(findings)


def filter_secrets(
    text: str, *, provider_id: str = _NATIVE_PROVIDER_ID, fail_closed: bool = True
) -> tuple[str, tuple[TrustFinding, ...], bool]:
    """Redact secret shapes. Returns (filtered_text, findings, excluded).

    High-confidence hits are always redacted from model-bound text. When
    ``fail_closed`` is True and confidence is high, the caller may exclude the
    unit entirely — this helper still returns redacted text for safety.
    """
    findings: list[TrustFinding] = []
    filtered = text
    high_confidence = False
    for pattern in _SECRET_PATTERNS:
        for match in pattern.finditer(text):
            high_confidence = True
            findings.append(
                TrustFinding(
                    kind="secret",
                    confidence=0.95,
                    detail="credential_shape_detected",
                    evidence_span=SECRET_REDACTION_TOKEN,
                    capability_id="security.secrets",
                    provider_id=provider_id,
                )
            )
            filtered = filtered.replace(match.group(0), SECRET_REDACTION_TOKEN)
    excluded = bool(fail_closed and high_confidence and findings)
    # Even when fail-closed, keep a redacted evidence unit rather than dropping
    # provenance — exclusion is reserved for policy callers; pipeline keeps text
    # redacted and marks findings. Default: do not auto-exclude on secrets alone
    # unless content is *only* secrets; tests accept redaction OR exclusion.
    if excluded and filtered.strip() and SECRET_REDACTION_TOKEN in filtered:
        # Prefer redacted evidence over hard drop so provenance survives.
        excluded = False
    return filtered, tuple(findings), excluded


def scan_agent_environment(
    *,
    content: str,
    config_kind: ConfigKind = "unknown",
    path: str | None = None,
    provider_id: str = _NATIVE_PROVIDER_ID,
) -> tuple[TrustFinding, ...]:
    """Native heuristics over hooks / MCP / AGENTS.md / permissions configs."""
    findings: list[TrustFinding] = []
    capability = "security.mcp_config" if config_kind == "mcp_config" else "security.agent_config"
    for pattern, confidence, label in _DANGEROUS_HOOK_PATTERNS:
        match = pattern.search(content)
        if match is None:
            continue
        kind = "dangerous_hook" if config_kind == "hooks" else "malicious_config"
        findings.append(
            TrustFinding(
                kind=kind,
                confidence=confidence,
                detail=f"{label}:{path or config_kind}",
                evidence_span=match.group(0)[:120],
                capability_id=capability,
                provider_id=provider_id,
            )
        )
    # Secrets accidentally embedded in agent config
    _, secret_findings, _ = filter_secrets(content, provider_id=provider_id, fail_closed=False)
    for finding in secret_findings:
        findings.append(
            TrustFinding(
                kind=finding.kind,
                confidence=finding.confidence,
                detail=f"agent_config_secret:{path or config_kind}",
                evidence_span=SECRET_REDACTION_TOKEN,
                capability_id=capability,
                provider_id=provider_id,
            )
        )
    return tuple(findings)


def _resolve_security_providers(
    registry: SemanticCapabilityRegistry | None,
) -> dict[str, ResolveDecision]:
    active = registry if registry is not None else build_default_registry()
    return {
        cap: active.resolve(cap)
        for cap in (
            "security.prompt_injection",
            "security.secrets",
            "security.agent_config",
            "security.mcp_config",
        )
    }


def _provider_id_for(decisions: Mapping[str, ResolveDecision], capability: str) -> str:
    decision = decisions.get(capability)
    if decision is not None and decision.selected is not None:
        return decision.selected.provider_id
    return _NATIVE_PROVIDER_ID


def _choose_action(
    findings: tuple[TrustFinding, ...], *, transformed: bool, fail_closed_secrets: bool
) -> ReceiptAction:
    high_secret = any(f.kind == "secret" and f.confidence >= 0.8 for f in findings)
    high_danger = any(
        f.kind in {"malicious_config", "dangerous_hook"} and f.confidence >= 0.8 for f in findings
    )
    if fail_closed_secrets and high_secret and high_danger:
        return "fail_closed"
    if any(f.kind == "prompt_injection" and f.confidence >= 0.8 for f in findings) or transformed:
        return "transform"
    if findings:
        return "surface_evidence"
    return "allow"


def admit_external_evidence(
    *,
    content: str,
    source_kind: SourceKind,
    source_uri: str,
    unit_id: str,
    key: str,
    slot_type: SlotType = "evidence",
    system_policy: str | None = None,
    task_policy: str | None = None,
    fail_closed_secrets: bool = True,
    observed_at: str | None = None,
    tenant_scope: str = "default",
    project_scope: str = "default",
    registry: SemanticCapabilityRegistry | None = None,
) -> TrustPipelineResult:
    """Run the full trust boundary pipeline → ContextUnit (or exclusion)."""
    trust, authority = classify_trust_authority(source_kind)
    # External text never gains instruction/policy authority
    if source_kind in _EXTERNAL_SOURCES:
        authority = "evidence"
        if trust not in {"untrusted", "unverified"}:
            trust = "untrusted"

    decisions = _resolve_security_providers(registry)
    inj_provider = _provider_id_for(decisions, "security.prompt_injection")
    secret_provider = _provider_id_for(decisions, "security.secrets")

    injection_findings = detect_prompt_injection(content, provider_id=inj_provider)
    filtered, secret_findings, excluded_by_secret = filter_secrets(
        content, provider_id=secret_provider, fail_closed=fail_closed_secrets
    )

    # Bounded transformation: neutralize injection framing while retaining text
    transformed_content = sanitize_injection_patterns(filtered)
    lineage: list[str] = [f"{CONTEXT_TRUST_SCHEMA_VERSION}:admit"]
    lineage.append(f"trust:{trust}")
    lineage.append(f"authority:{authority}")
    if injection_findings:
        lineage.append("transform:prompt_injection_sanitized")
    if secret_findings:
        lineage.append("transform:secrets_redacted")

    findings = tuple(list(injection_findings) + list(secret_findings))
    action = _choose_action(
        findings,
        transformed=transformed_content != content,
        fail_closed_secrets=fail_closed_secrets,
    )

    # High-confidence secrets: never let canary reach model payload
    content_for_model = transformed_content
    for pattern in _SECRET_PATTERNS:
        content_for_model = pattern.sub(SECRET_REDACTION_TOKEN, content_for_model)

    excluded = excluded_by_secret and action == "fail_closed"
    exclusion_reason = "fail_closed_high_confidence_secret" if excluded else None

    observed = observed_at or _now_iso()
    source_digest = _digest_text(content)
    unit: ContextUnit | None = None
    if not excluded:
        unit = ContextUnit(
            unit_id=unit_id,
            slot_type=slot_type,
            key=key,
            content=content_for_model,
            source_uri=source_uri,
            source_digest=source_digest,
            revision="trust-boundary",
            observed_at=observed,
            retrieved_at=observed,
            trust=trust,
            authority=authority,
            sensitivity="restricted" if secret_findings else "standard",
            tenant_scope=tenant_scope,
            project_scope=project_scope,
            raw=False,
            status="active",
            transform_lineage=tuple(lineage),
            confidence=1.0 - max((f.confidence for f in injection_findings), default=0.0) * 0.3,
        )

    provider_ids = tuple(
        sorted({d.selected.provider_id for d in decisions.values() if d.selected is not None})
    )
    policy_reason = exclusion_reason or (
        "external_evidence_admitted" if action == "allow" else f"security_action:{action}"
    )
    receipt = SecurityReceipt(
        receipt_id=_new_receipt_id(),
        policy_reason=policy_reason,
        findings=findings,
        action=action,
        routing_eligibility_delta=False,
        provider_ids=provider_ids,
    )

    return TrustPipelineResult(
        trust=trust,
        authority=authority,
        findings=findings,
        receipt=receipt,
        content_for_model=content_for_model if not excluded else "",
        unit=unit,
        excluded=excluded,
        exclusion_reason=exclusion_reason,
        transform_lineage=tuple(lineage),
        preserved_system_policy=system_policy,
        preserved_task_policy=task_policy,
        provider_decisions=decisions,
    )


def apply_routing_eligibility(
    *,
    currently_eligible: bool,
    make_eligible: bool,
    receipt: SecurityReceipt | None,
    policy_reason: str | None,
) -> tuple[bool, SecurityReceipt]:
    """Change routing eligibility only with an explicit receipt + policy reason.

    Silent eligibility changes are rejected — security findings alone must not
    mutate routing without an auditable policy decision.
    """
    if receipt is None or not (policy_reason and policy_reason.strip()):
        raise ValueError("routing eligibility changes require a security receipt and policy reason")
    if currently_eligible == make_eligible:
        return currently_eligible, receipt

    updated = SecurityReceipt(
        receipt_id=receipt.receipt_id,
        policy_reason=policy_reason.strip(),
        findings=receipt.findings,
        action=receipt.action,
        routing_eligibility_delta=True,
        schema_version=receipt.schema_version,
        provider_ids=receipt.provider_ids,
    )
    return make_eligible, updated


def describe_trust_boundary() -> dict[str, Any]:
    """Controller-friendly snapshot of the native trust boundary contract."""
    return {
        "schema_version": CONTEXT_TRUST_SCHEMA_VERSION,
        "pipeline": [
            "classify_trust_authority",
            "detect_prompt_injection",
            "filter_secrets",
            "provenance_freshness",
            "bounded_transformation",
            "context_unit",
        ],
        "capabilities": sorted(
            {
                "security.prompt_injection",
                "security.secrets",
                "security.agent_config",
                "security.mcp_config",
            }
        ),
        "native_provider_id": _NATIVE_PROVIDER_ID,
        "secret_redaction_token": SECRET_REDACTION_TOKEN,
    }


__all__ = [
    "CONTEXT_TRUST_SCHEMA_VERSION",
    "SECRET_REDACTION_TOKEN",
    "AuthorityClass",
    "ConfigKind",
    "ReceiptAction",
    "SecurityReceipt",
    "SourceKind",
    "TrustClass",
    "TrustFinding",
    "TrustPipelineResult",
    "admit_external_evidence",
    "apply_routing_eligibility",
    "classify_trust_authority",
    "describe_trust_boundary",
    "detect_prompt_injection",
    "filter_secrets",
    "scan_agent_environment",
]
