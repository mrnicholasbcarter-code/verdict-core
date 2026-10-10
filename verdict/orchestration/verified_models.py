"""Pure projection of evidenced-working model identities (BOD-291).

This module owns one pure function, ``project_verified_models``, plus the
immutable row/view/query dataclasses and a set of strict parser helpers that
turn the raw JSON documents of the existing evidence stores into a validated
``EvidenceSnapshots``.

Design contract: ``openspec/changes/bod-291-292-verified-model-control``
sections 1-3, 8 (Unit 1) and 9.

The projection separates *display evidence* from *launch authority*.  Only the
at-rest health cache (``health_cache.py``) carries chat/tool/identity proof, so
only it can mint ``VERIFIED``/``STALE``.  Ladder, worker and receipt positives
are timestamped hints and never prove verification.  Active scoped blockers beat
positive evidence of any age or source.  Nothing here reads disk, the clock or
the network, writes anything, or probes a model.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from verdict.admission import canonical_route_id, is_opaque, route_provider_prefix
from verdict.availability import QuotaEvidence, account_forms
from verdict.orchestration.contracts import CapacityClass
from verdict.orchestration.eligibility import capacity_class_of
from verdict.orchestration.health_cache import (
    CATEGORY_AUTH,
    CATEGORY_CATALOG_STALE,
    CATEGORY_GONE,
    CATEGORY_MODEL_MISMATCH,
    CATEGORY_NOT_FOUND,
    CATEGORY_OK,
    CATEGORY_PAYMENT,
    CATEGORY_PERMISSION,
    CATEGORY_RATE_LIMITED,
    CATEGORY_TIMEOUT,
    CATEGORY_UPSTREAM,
    FRESH_SECONDS,
    USABLE_SECONDS,
)
from verdict.orchestration.provider_catalog import resolve_provider
from verdict.security import redact_text

SCHEMA = "verdict.verified-models/v1"
HEALTH_CACHE_SCHEMA = "1"
LADDER_HEALTH_TTL_SECONDS = 300.0

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

# Known at-rest category / identity vocabularies.  Anything outside these sets
# is treated as malformed evidence (recorded as a source error, never trusted).
_KNOWN_CATEGORIES: frozenset[str] = frozenset(
    {
        "",
        CATEGORY_OK,
        CATEGORY_RATE_LIMITED,
        CATEGORY_TIMEOUT,
        CATEGORY_UPSTREAM,
        CATEGORY_AUTH,
        CATEGORY_PAYMENT,
        CATEGORY_PERMISSION,
        CATEGORY_NOT_FOUND,
        CATEGORY_GONE,
        CATEGORY_CATALOG_STALE,
        CATEGORY_MODEL_MISMATCH,
        # prove_at_rest.category_for's non-specific-HTTP-error fallback
        # (``named or "http_error"``): a probed negative with a status that
        # does not match any named category above. Real at-rest caches carry
        # this for the large majority of negatives; rejecting it turned every
        # such row into a source_error instead of a diagnostic FAILED row.
        "http_error",
        # prove_at_rest.run_agentic_probes writes this literal category
        # directly via ``self.cache.record`` (CATEGORY_OK if passed else
        # "agentic_fail") when a BOD-299 session-grade agentic qualification
        # probe fails. It is a real cache contract value, not request-scoped
        # noise: once agentic probes run, those rows hit the same defect.
        "agentic_fail",
    }
)
_KNOWN_IDENTITIES: frozenset[str] = frozenset(
    {"", "verified", "not_reported", "mismatch", "legacy", "unknown"}
)

# Availability (rule 2) categories map to UNAVAILABLE; diagnostic (rule 3)
# categories map to FAILED.  Mirrors design section 7.
_AVAILABILITY_CATEGORIES: frozenset[str] = frozenset(
    {
        CATEGORY_AUTH,
        CATEGORY_PAYMENT,
        CATEGORY_PERMISSION,
        CATEGORY_RATE_LIMITED,
        "unauthorized",
        "quota_exhausted",
        "exhausted",
        "cooldown",
        "lockout",
        "payment",
        "rate_limit",
        "subscription_exhaustion",
        "subscription_unknown",
        "concurrency_limit",
        "provider_overload",
    }
)
_DIAGNOSTIC_CATEGORIES: frozenset[str] = frozenset(
    {
        CATEGORY_TIMEOUT,
        CATEGORY_UPSTREAM,
        CATEGORY_NOT_FOUND,
        CATEGORY_GONE,
        CATEGORY_CATALOG_STALE,
        CATEGORY_MODEL_MISMATCH,
        "http_error",
        "agentic_fail",
        "upstream",
        "response_validation",
        "tool_validation",
        "unhealthy",
    }
)

_BAD_TEST_STATUS: frozenset[str] = frozenset(
    {"error", "failed", "expired", "unauthorized", "invalid", "unavailable"}
)

# Admission ``first_failed_stage`` classes (design section 2).  A declared
# capability / context gate is an EXCLUDED reason; an entitlement / availability
# denial is an UNAVAILABLE reason.  Both are current facts that beat positive
# proof.  Other stages (policy / controller / downstream / discovered) fall
# through to the inventory / non-admissible rules unchanged.
_CAPABILITY_STAGES: frozenset[str] = frozenset(
    {"CAPABILITY", "CAPABLE", "CONTEXT", "CONTEXT_WINDOW"}
)
_ENTITLEMENT_STAGES: frozenset[str] = frozenset({"ENTITLED", "ENTITLEMENT", "AVAILABLE"})
# Admission ``reason`` codes that denote an entitlement / auth / payment /
# permission / quota denial (design section 2 -> UNAVAILABLE).
_DENIAL_REASONS: frozenset[str] = frozenset(
    {
        "authentication",
        "unauthorized",
        "payment_required",
        "payment",
        "permission",
        "permission_denied",
        "quota_exhausted",
        "exhausted",
        "rate_limited",
        "rate_limit",
        "no_connection_evidence",
        "no_active_account",
        "connection_status_bad",
    }
)

_PREPAID: frozenset[CapacityClass] = frozenset({CapacityClass.FREE, CapacityClass.SUBSCRIPTION})

# Availability-blocker tie-break ranks (design section 2).
_SCOPE_RANK: Mapping[str, int] = {
    "provider": 0,
    "pool": 0,
    "account": 0,
    "connection": 0,
    "route": 1,
}
_CATEGORY_RANK: Mapping[str, int] = {
    "authentication": 0,
    "unauthorized": 0,
    "payment_required": 1,
    "payment": 1,
    "permission": 2,
    "quota_exhausted": 3,
    "exhausted": 3,
    "rate_limited": 3,
    "rate_limit": 3,
}
_SOURCE_RANK: Mapping[str, int] = {
    "health_cache": 0,
    "ladder_state": 1,
    "worker_health": 2,
    "admission": 3,
    "connections": 4,
    "inventory": 5,
    "policy": 6,
    "harness_visibility": 7,
}


# ---------------------------------------------------------------------------
# Status vocabulary
# ---------------------------------------------------------------------------


class VerifiedStatus(str, Enum):
    """One status per route. Order is the stable display/sort order."""

    VERIFIED = "VERIFIED"
    STALE = "STALE"
    FAILED = "FAILED"
    UNAVAILABLE = "UNAVAILABLE"
    UNVERIFIED = "UNVERIFIED"
    INVENTORY_ONLY = "INVENTORY_ONLY"
    EXCLUDED = "EXCLUDED"


_STATUS_ORDER: tuple[VerifiedStatus, ...] = (
    VerifiedStatus.VERIFIED,
    VerifiedStatus.STALE,
    VerifiedStatus.FAILED,
    VerifiedStatus.UNAVAILABLE,
    VerifiedStatus.UNVERIFIED,
    VerifiedStatus.INVENTORY_ONLY,
    VerifiedStatus.EXCLUDED,
)
_STATUS_RANK: Mapping[VerifiedStatus, int] = {s: i for i, s in enumerate(_STATUS_ORDER)}


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def _require_aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def _parse_ts(value: Any) -> datetime:
    """Parse an ISO-8601 timestamp into an aware UTC datetime. Naive is rejected."""
    if isinstance(value, datetime):
        return _require_aware(value)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timestamp must be a non-empty ISO-8601 string")
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("invalid timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _require_aware(value).isoformat().replace("+00:00", "Z")


def _epoch(value: datetime | None) -> float:
    return value.timestamp() if value is not None else -math.inf


# Secret shapes are checked before output so route ids are never rewritten.
_BEARER_TOKEN_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-/+=]+")
_SK_TOKEN_RE = re.compile(r"(?i)\b[srp]k-[A-Za-z0-9._\-/+=]+")
_SECRET_KEY_RE = re.compile(
    r"(?i)\b[A-Za-z0-9_]*(?:key|token|secret|password)[A-Za-z0-9_]*\s*=\s*[^\s,;]+"
)
_SECRET_ID_RE = re.compile(r"(?i)\b[srp]k-[A-Za-z0-9._\-/+=]{12,}")
_JWT_RE = re.compile(
    r"\b(?:eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
    r"|[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})\b"
)
_EMAIL_RE = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _secret_like(value: str) -> bool:
    return any(
        pattern.search(value)
        for pattern in (_SECRET_ID_RE, _BEARER_TOKEN_RE, _SECRET_KEY_RE, _JWT_RE)
    )


def _scrub(value: Any) -> str:
    """Redact credentials, account emails and control characters at display."""
    text = redact_text(str(value))
    for pattern in (_BEARER_TOKEN_RE, _SK_TOKEN_RE, _SECRET_KEY_RE, _JWT_RE, _EMAIL_RE):
        text = pattern.sub("[redacted]", text)
    text = "".join(ch for ch in text if ch == " " or ch.isprintable())
    return text.strip()


def _withhold_route_id(route_id: str) -> bool:
    """Reject sensitive ids, rather than rewriting a canonical route identity."""
    return _secret_like(route_id) or _scrub(route_id) != route_id


def _safe_value(value: Any) -> str:
    """Bounded, redacted rendering of an untrusted value for a source-error
    message. Never echoes a raw secret; truncates to a short bound so even a
    non-pattern secret cannot leak in full."""
    text = _scrub(value)
    if len(text) > 48:
        text = text[:48] + "..."
    return repr(text)


def _sanitize(value: Any) -> str | None:
    """Sanitize an arbitrary diagnostic string; drop secrets and control chars."""
    if value is None:
        return None
    return _scrub(value) or None


def _bool_or_none(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


# ---------------------------------------------------------------------------
# Display-boundary vocabularies.  Every public string field a row exposes is
# validated here so untrusted free text (provider errors, pool/account names,
# admission reasons) can never reach ``to_dict``.  Restrictions are stable
# machine codes; scope identifiers must match a safe id pattern; every other
# free-text field is scrubbed.
# ---------------------------------------------------------------------------

# Fixed restriction vocabulary (design sections 2-3).  Any code outside this set
# at the display boundary collapses to a generic marker so a raw admission
# reason can never surface in ``restriction`` / ``restrictions``.
_RESTRICTION_CODES: frozenset[str] = frozenset(
    {
        "agentic_not_fresh",
        "availability_blocked",
        "chat_only_not_coding_verified",
        "contradictory_inventory",
        "half_open",
        "identity_not_verified",
        "opaque_route",
        "policy_excluded",
        "route_negative",
        "visibility_unavailable",
        "capability_excluded",
        "entitlement_denied",
        "no_connection_evidence",
        "no_active_account",
        "tools_capability_unavailable",
        "not_admissible",
        "account_binding_ambiguous",
        "pool_binding_ambiguous",
        "stale_stale",
        "stale_expired",
    }
)
_RESTRICTION_UNKNOWN = "admission_denied_unknown"
_CONNECTION_STATUS_PREFIX = "connection_status:"
# Safe scope/id pattern: no whitespace, no credential punctuation.
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")
_SCOPE_KINDS: frozenset[str] = frozenset({"route", "provider", "pool", "account", "connection"})


def _safe_restriction(code: str | None) -> str | None:
    """Map a restriction to the fixed vocabulary.

    Known codes pass through; the ``connection_status:<status>`` shape passes
    only for a controlled bad-status token; anything else (e.g. a raw admission
    reason) collapses to ``admission_denied_unknown`` so untrusted free text can
    never leak in a public restriction field.
    """
    if code is None:
        return None
    if code in _RESTRICTION_CODES:
        return code
    if code.startswith(_CONNECTION_STATUS_PREFIX):
        status = code[len(_CONNECTION_STATUS_PREFIX) :]
        if status in _BAD_TEST_STATUS:
            return code
    return _RESTRICTION_UNKNOWN


def _safe_scope(scope: str | None) -> str | None:
    """Pseudonymize accounts and unsafe pools; never display an account id."""
    if scope is None:
        return None
    kind, sep, name = scope.partition(":")
    kind = kind.strip().lower()
    name = name.strip()
    if sep and kind in _SCOPE_KINDS:
        if kind == "account" or (
            kind == "pool" and not (_SAFE_ID_RE.fullmatch(name) and _scrub(name) == name)
        ):
            digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]
            return f"{kind}:h-{digest}"
        if _SAFE_ID_RE.fullmatch(name) and _scrub(name) == name:
            return f"{kind}:{name}"
        return f"{kind}:<invalid>"
    text = scope.strip()
    if _SAFE_ID_RE.fullmatch(text) and _scrub(text) == text:
        return text
    return "scope:<invalid>"


def _display_safe(value: str | None) -> str | None:
    """Scrub a free-text public field (reason/category/identity/source ...).

    Redacts secrets and control characters; an empty result becomes ``None``.
    Idempotent for the controlled vocabularies the projection already emits.
    """
    if value is None:
        return None
    return _scrub(value) or None


# ---------------------------------------------------------------------------
# Parsed evidence structures (internal, produced by the parsers)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AtRestHealth:
    """One validated schema-1 at-rest health-cache route entry."""

    route_id: str
    category: str
    checked_at: datetime
    until: datetime
    consecutive_failures: int
    chat_ok: bool
    tool_ok: bool
    healthy: bool
    identity: str
    latency_ms: float | None = None
    http_status: int | None = None
    probe_class: str | None = None
    agentic_ok: bool = False
    agentic_checked_at: datetime | None = None
    last_success_at: datetime | None = None
    pool: str | None = None

    def is_positive_proof(self) -> bool:
        """True only for full chat/identity proof (design section 1)."""
        return (
            self.healthy
            and self.category == CATEGORY_OK
            and self.chat_ok
            and self.identity == "verified"
        )


@dataclass(frozen=True)
class LadderHealth:
    """One ladder-state health row (TTL 300s). Positives are hints only."""

    route_id: str
    category: str
    checked_at: datetime
    healthy: bool


@dataclass(frozen=True)
class WorkerHealth:
    """One worker-cache route row. Positives are hints only."""

    route_id: str
    category: str
    expires_at: datetime
    healthy: bool
    observed_at: datetime | None = None
    status_code: int | None = None


@dataclass(frozen=True)
class ScopedCooldown:
    """An active scoped availability blocker (route/provider/pool/account)."""

    scope: str
    name: str
    category: str
    until: datetime
    source: str
    canonical_category: str | None = None
    checked_at: datetime | None = None
    observed_at: datetime | None = None
    pool_id: str | None = None
    account_id: str | None = None


@dataclass(frozen=True)
class AdmissionFact:
    """Current unscoped admission for one route (precomputed by the caller)."""

    admitted: bool
    first_failed_stage: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class EvidenceSnapshots:
    """Parsed, validated evidence for the projection. No raw documents inside."""

    at_rest: Mapping[str, AtRestHealth] = field(default_factory=dict)
    at_rest_cooldowns: tuple[ScopedCooldown, ...] = ()
    ladder_health: Mapping[str, LadderHealth] = field(default_factory=dict)
    ladder_cooldowns: tuple[ScopedCooldown, ...] = ()
    worker_health: Mapping[str, WorkerHealth] = field(default_factory=dict)
    worker_cooldowns: tuple[ScopedCooldown, ...] = ()
    ladder_hints: frozenset[str] = frozenset()
    worker_hints: frozenset[str] = frozenset()
    receipt_admitted: frozenset[str] = frozenset()
    receipt_reasons: Mapping[str, str] = field(default_factory=dict)
    admission_facts: Mapping[str, AdmissionFact] | None = None
    policy_exclusions: Mapping[str, str] = field(default_factory=dict)
    visibility_unavailable: frozenset[str] = frozenset()
    source_errors: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Query + row + view
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerifiedModelQuery:
    """Filters and paging for the projection. Validated in ``project_verified_models``."""

    status: str | None = None
    provider: str | None = None
    search: str | None = None
    page: int = 1
    page_size: int = DEFAULT_PAGE_SIZE


@dataclass(frozen=True)
class VerifiedModelRow:
    """One route's projected status and evidence. All fields are display-safe."""

    route_id: str
    provider: str
    status: VerifiedStatus
    coding_ok: bool = False
    last_success_at: datetime | None = None
    checked_at: datetime | None = None
    evidence_source: str | None = None
    fresh_until: datetime | None = None
    expires_at: datetime | None = None
    freshness: str | None = None
    context_window: int | None = None
    tools: bool | None = None
    structured: bool | None = None
    restriction: str | None = None
    reason: str | None = None
    restrictions: tuple[str, ...] = ()
    cooldown_until: datetime | None = None
    cooldown_scope: str | None = None
    failure_category: str | None = None
    http_status: int | None = None
    latency_ms: float | None = None
    identity: str | None = None
    probe_class: str | None = None
    agentic_ok: bool | None = None
    agentic_checked_at: datetime | None = None
    capacity_class: str | None = None
    refreshable: bool = False
    refresh_reason: str | None = None
    hints: tuple[str, ...] = ()
    availability_evidence: tuple[Mapping[str, Any], ...] = ()
    # Private sort keys (not serialized).
    _sort_checked: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the stable envelope (design section 3).

        This is the display boundary: every public free-text field is scrubbed
        and every restriction is coerced to the fixed vocabulary here, so an
        untrusted value stored on the row can never leak into the rendered JSON
        regardless of how the row was built.
        """
        if _withhold_route_id(self.route_id):
            raise ValueError("route id withheld")
        return {
            "route_id": self.route_id,
            "provider": _display_safe(self.provider),
            "status": self.status.value,
            "coding_ok": self.coding_ok,
            "last_success_at": _iso(self.last_success_at),
            "checked_at": _iso(self.checked_at),
            "evidence_source": _display_safe(self.evidence_source),
            "fresh_until": _iso(self.fresh_until),
            "expires_at": _iso(self.expires_at),
            "freshness": _display_safe(self.freshness),
            "capabilities": {
                "context_window": self.context_window,
                "tools": self.tools,
                "structured": self.structured,
            },
            "restriction": _safe_restriction(self.restriction),
            "reason": _display_safe(self.reason),
            "restrictions": [
                code
                for code in (_safe_restriction(r) for r in self.restrictions)
                if code is not None
            ],
            "cooldown_until": _iso(self.cooldown_until),
            "cooldown_scope": _safe_scope(self.cooldown_scope),
            "failure_category": _display_safe(self.failure_category),
            "http_status": self.http_status,
            "latency_ms": self.latency_ms,
            "identity": _display_safe(self.identity),
            "probe_class": _display_safe(self.probe_class),
            "agentic_ok": self.agentic_ok,
            "agentic_checked_at": _iso(self.agentic_checked_at),
            "capacity_class": _display_safe(self.capacity_class),
            "refreshable": self.refreshable,
            "refresh_reason": _display_safe(self.refresh_reason),
            "hints": [_display_safe(hint) for hint in self.hints],
            "availability_evidence": [dict(item) for item in self.availability_evidence],
        }


@dataclass(frozen=True)
class VerifiedModelsView:
    """Full projection: paged rows plus reconciled counts and filters."""

    generated_at: datetime
    rows: tuple[VerifiedModelRow, ...]
    counts_by_status: Mapping[str, int]
    filtered_counts_by_status: Mapping[str, int]
    total_count: int
    filtered_count: int
    filters: Mapping[str, str | None]
    page: int
    page_size: int
    page_count: int
    source_errors: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "generated_at": _iso(self.generated_at),
            "rows": [r.to_dict() for r in self.rows],
            "counts_by_status": {
                s.value: self.counts_by_status.get(s.value, 0) for s in _STATUS_ORDER
            },
            "filtered_counts_by_status": {
                s.value: self.filtered_counts_by_status.get(s.value, 0) for s in _STATUS_ORDER
            },
            "total_count": self.total_count,
            "filtered_count": self.filtered_count,
            "filters": {
                key: (
                    "[redacted]"
                    if _secret_like(self.filters.get(key) or "")
                    else _display_safe(self.filters.get(key))
                )
                for key in ("status", "provider", "search")
            },
            "page": self.page,
            "page_size": self.page_size,
            "page_count": self.page_count,
            "source_errors": [_scrub(error) for error in self.source_errors],
        }


# ---------------------------------------------------------------------------
# Strict parsers: raw JSON documents -> EvidenceSnapshots
# ---------------------------------------------------------------------------


def _parse_at_rest(
    doc: Mapping[str, Any] | None, *, now: datetime, errors: list[str]
) -> tuple[dict[str, AtRestHealth], list[ScopedCooldown]]:
    """Parse ``health-cache.json``. Strict: schema=="1", key==route_id, literal bools,
    known category/identity, aware checked_at<=now. Invalid entries -> source_errors."""
    routes: dict[str, AtRestHealth] = {}
    cooldowns: list[ScopedCooldown] = []
    if doc is None:
        return routes, cooldowns
    if not isinstance(doc, Mapping):
        errors.append("health_cache: document is not an object")
        return routes, cooldowns
    schema = doc.get("schema_version")
    if schema != HEALTH_CACHE_SCHEMA:
        errors.append(f"health_cache: unsupported schema_version {schema!r}")
        return routes, cooldowns
    raw_routes = doc.get("routes")
    if raw_routes is not None and not isinstance(raw_routes, Mapping):
        errors.append("health_cache: routes is not an object")
        raw_routes = None
    for key, entry in (raw_routes or {}).items():
        try:
            parsed = _parse_at_rest_entry(str(key), entry, now=now)
        except ValueError as exc:
            errors.append(f"health_cache[{_sanitize(key)}]: {exc}")
            continue
        canon = canonical_route_id(str(key))
        if canon in routes:
            errors.append(f"health_cache[{_sanitize(key)}]: duplicate canonical route id")
            continue
        routes[canon] = parsed
    raw_cooldowns = doc.get("cooldowns")
    if raw_cooldowns is not None and not isinstance(raw_cooldowns, Mapping):
        errors.append("health_cache: cooldowns is not an object")
        raw_cooldowns = None
    for key, entry in (raw_cooldowns or {}).items():
        cd = _parse_scoped_cooldown(str(key), entry, now=now, source="health_cache", errors=errors)
        if cd is not None:
            cooldowns.append(cd)
    return routes, cooldowns


def _parse_at_rest_entry(key: str, entry: Any, *, now: datetime) -> AtRestHealth:
    if not isinstance(entry, Mapping):
        raise ValueError("entry is not an object")
    rid = entry.get("route_id")
    if not isinstance(rid, str) or not rid.strip():
        raise ValueError("route_id is required")
    if canonical_route_id(rid) != canonical_route_id(key):
        raise ValueError("route_id does not match map key")
    for name in ("chat_ok", "tool_ok", "healthy"):
        if not isinstance(entry.get(name), bool):
            raise ValueError(f"{name} must be a literal boolean")
    category = entry.get("category")
    if not isinstance(category, str) or category not in _KNOWN_CATEGORIES:
        raise ValueError(f"unknown category (value={_safe_value(category)})")
    identity = entry.get("identity", "")
    if not isinstance(identity, str) or identity not in _KNOWN_IDENTITIES:
        raise ValueError(f"unknown identity (value={_safe_value(identity)})")
    checked_at = _parse_ts(entry.get("checked_at"))
    if checked_at > now:
        raise ValueError("checked_at is in the future")
    until = _parse_ts(entry.get("until"))
    cf = entry.get("consecutive_failures", 0)
    if not isinstance(cf, int) or isinstance(cf, bool) or cf < 0:
        raise ValueError("consecutive_failures must be a non-negative int")
    latency = entry.get("latency_ms")
    latency_ms = (
        float(latency)
        if isinstance(latency, (int, float)) and not isinstance(latency, bool)
        else None
    )
    http = entry.get("http_status")
    http_status = int(http) if isinstance(http, int) and not isinstance(http, bool) else None
    probe_class = entry.get("probe_class")
    probe_class = probe_class if isinstance(probe_class, str) and probe_class else None
    agentic_ok = entry.get("agentic_ok") is True
    agentic_checked_at: datetime | None = None
    if entry.get("agentic_checked_at"):
        agentic_checked_at = _parse_ts(entry.get("agentic_checked_at"))
        if agentic_checked_at > now:
            raise ValueError("agentic_checked_at is in the future")
    last_success_at: datetime | None = None
    if entry.get("last_success_at"):
        last_success_at = _parse_ts(entry.get("last_success_at"))
        if last_success_at > now:
            raise ValueError("last_success_at is in the future")
    pool = entry.get("pool")
    pool = pool if isinstance(pool, str) and pool else None
    return AtRestHealth(
        route_id=canonical_route_id(rid),
        category=category,
        checked_at=checked_at,
        until=until,
        consecutive_failures=cf,
        chat_ok=entry["chat_ok"],
        tool_ok=entry["tool_ok"],
        healthy=entry["healthy"],
        identity=identity,
        latency_ms=latency_ms,
        http_status=http_status,
        probe_class=probe_class,
        agentic_ok=agentic_ok,
        agentic_checked_at=agentic_checked_at,
        last_success_at=last_success_at,
        pool=pool,
    )


def _parse_scoped_cooldown(
    key: str, entry: Any, *, now: datetime, source: str, errors: list[str]
) -> ScopedCooldown | None:
    if not isinstance(entry, Mapping):
        errors.append(f"{source} cooldown[{_sanitize(key)}]: entry is not an object")
        return None
    scope, _, name = str(key).partition(":")
    scope = scope.strip().lower()
    if scope not in {"route", "provider", "pool", "account"} or not name.strip():
        errors.append(f"{source} cooldown[{_sanitize(key)}]: bad scope/name")
        return None
    try:
        until = _parse_ts(entry.get("until"))
    except ValueError as exc:
        errors.append(f"{source} cooldown[{_sanitize(key)}]: {exc}")
        return None
    norm = canonical_route_id(name) if scope == "route" else name.strip()
    if scope == "provider":
        norm = norm.lower()
    category = str(entry.get("category", "cooldown") or "cooldown")
    canonical_category = entry.get("canonical_category")
    checked_at: datetime | None = None
    if entry.get("checked_at"):
        try:
            checked_at = _parse_ts(entry.get("checked_at"))
        except ValueError:
            checked_at = None
    return ScopedCooldown(
        scope=scope,
        name=norm,
        category=category,
        until=until,
        source=source,
        canonical_category=canonical_category if isinstance(canonical_category, str) else None,
        checked_at=checked_at,
        pool_id=str(entry.get("pool_id")) if entry.get("pool_id") else None,
        account_id=str(entry.get("account_id")) if entry.get("account_id") else None,
    )


def _parse_ladder(
    doc: Mapping[str, Any] | None, *, now: datetime, errors: list[str]
) -> tuple[dict[str, LadderHealth], list[ScopedCooldown], set[str]]:
    """Parse ``orchestration-health.json``. Positives within TTL become hints only."""
    health: dict[str, LadderHealth] = {}
    cooldowns: list[ScopedCooldown] = []
    hints: set[str] = set()
    if doc is None:
        return health, cooldowns, hints
    if not isinstance(doc, Mapping):
        errors.append("ladder_state: document is not an object")
        return health, cooldowns, hints
    raw_health = doc.get("health")
    if raw_health is not None and not isinstance(raw_health, Mapping):
        errors.append("ladder_state: health is not an object")
        raw_health = None
    for key, entry in (raw_health or {}).items():
        if not isinstance(entry, Mapping):
            errors.append(f"ladder_state[{_sanitize(key)}]: entry is not an object")
            continue
        try:
            checked_at = _parse_ts(entry.get("checked_at"))
        except ValueError as exc:
            errors.append(f"ladder_state[{_sanitize(key)}]: {exc}")
            continue
        if checked_at > now:
            errors.append(f"ladder_state[{_sanitize(key)}]: checked_at is in the future")
            continue
        if (now - checked_at).total_seconds() > LADDER_HEALTH_TTL_SECONDS:
            continue  # stale ladder evidence proves nothing either way
        canon = canonical_route_id(str(key))
        healthy = entry.get("healthy") is True
        category = str(entry.get("category", "") or "")
        if healthy:
            hints.add(canon)
            continue  # positive is a scheduling hint, never a negative to project
        health[canon] = LadderHealth(
            route_id=canon, category=category, checked_at=checked_at, healthy=False
        )
    raw_cooldowns = doc.get("cooldowns")
    if raw_cooldowns is not None and not isinstance(raw_cooldowns, Mapping):
        errors.append("ladder_state: cooldowns is not an object")
        raw_cooldowns = None
    for key, entry in (raw_cooldowns or {}).items():
        cd = _parse_scoped_cooldown(str(key), entry, now=now, source="ladder_state", errors=errors)
        if cd is not None:
            cooldowns.append(cd)
    return health, cooldowns, hints


def _parse_worker(
    doc: Mapping[str, Any] | None, *, now: datetime, errors: list[str]
) -> tuple[dict[str, WorkerHealth], list[ScopedCooldown], set[str]]:
    """Parse ``subagent-health.json`` (flat worker cache). Positives are hints only."""
    health: dict[str, WorkerHealth] = {}
    cooldowns: list[ScopedCooldown] = []
    hints: set[str] = set()
    if doc is None:
        return health, cooldowns, hints
    if not isinstance(doc, Mapping):
        errors.append("worker_health: document is not an object")
        return health, cooldowns, hints
    for key, entry in doc.items():
        if not isinstance(entry, Mapping):
            errors.append(f"worker_health[{_sanitize(key)}]: entry is not an object")
            continue
        try:
            expires = _parse_ts(entry.get("expires_at"))
        except ValueError as exc:
            errors.append(f"worker_health[{_sanitize(key)}]: {exc}")
            continue
        expired = expires <= now
        healthy = entry.get("healthy") is True
        category = str(entry.get("category", "unknown") or "unknown")
        observed_at: datetime | None = None
        if entry.get("observed_at"):
            try:
                observed_at = _parse_ts(entry.get("observed_at"))
            except ValueError:
                observed_at = None
        status_code = entry.get("status_code")
        status_code = (
            status_code
            if isinstance(status_code, int) and not isinstance(status_code, bool)
            else None
        )
        text = str(key)
        if text.startswith("provider:"):
            # An expired provider cooldown is inactive; a positive is not proof.
            if healthy or expired:
                continue
            cooldowns.append(
                ScopedCooldown(
                    scope="provider",
                    name=text.split(":", 1)[1].strip().lower(),
                    category=category,
                    until=expires,
                    source="worker_health",
                    observed_at=observed_at,
                )
            )
            continue
        canon = canonical_route_id(text)
        if healthy:
            if expired:
                continue  # an expired positive proves nothing either way
            hints.add(canon)
            continue
        # Retain route-level negatives even once expired: a fresh one drives
        # rule 3 FAILED (deadline > now), an expired one keeps the half-open
        # reason observable (``_is_half_open``) instead of silently vanishing.
        health[canon] = WorkerHealth(
            route_id=canon,
            category=category,
            expires_at=expires,
            healthy=False,
            observed_at=observed_at,
            status_code=status_code,
        )
    return health, cooldowns, hints


def _parse_receipt(
    doc: Mapping[str, Any] | None, *, errors: list[str]
) -> tuple[set[str], dict[str, str]]:
    """Parse ``admission-latest.json``. Admitted ids are hints only (possibly narrowed)."""
    admitted: set[str] = set()
    reasons: dict[str, str] = {}
    if doc is None:
        return admitted, reasons
    if not isinstance(doc, Mapping):
        errors.append("admission_receipt: document is not an object")
        return admitted, reasons
    candidates = doc.get("candidates")
    if isinstance(candidates, Sequence) and not isinstance(candidates, (str, bytes)):
        for cand in candidates:
            if not isinstance(cand, Mapping):
                continue
            rid = cand.get("route_id")
            if not isinstance(rid, str) or not rid.strip():
                continue
            canon = canonical_route_id(rid)
            if cand.get("admitted") is True:
                admitted.add(canon)
            reason = cand.get("reason")
            if isinstance(reason, str) and reason:
                reasons[canon] = reason
    raw_admitted = doc.get("admitted")
    if isinstance(raw_admitted, Sequence) and not isinstance(raw_admitted, (str, bytes)):
        for rid in raw_admitted:
            if isinstance(rid, str) and rid.strip():
                admitted.add(canonical_route_id(rid))
    return admitted, reasons


def snapshots_from_documents(
    *,
    health_cache_doc: Mapping[str, Any] | None = None,
    ladder_state_doc: Mapping[str, Any] | None = None,
    worker_health_doc: Mapping[str, Any] | None = None,
    admission_receipt_doc: Mapping[str, Any] | None = None,
    admission_facts: Mapping[str, Mapping[str, Any]] | None = None,
    policy_exclusions: Mapping[str, str] | None = None,
    visibility_unavailable: Sequence[str] | None = None,
    now: datetime,
    extra_source_errors: Sequence[str] = (),
) -> EvidenceSnapshots:
    """Parse the raw JSON documents of each evidence store into an EvidenceSnapshots.

    Validation is strict; invalid pieces are recorded in ``source_errors`` and do
    not make any row healthy. Unit 2 performs the file reads and passes the loaded
    dicts here; this function performs no I/O.
    """
    now = _require_aware(now)
    errors: list[str] = list(extra_source_errors)
    at_rest, at_rest_cd = _parse_at_rest(health_cache_doc, now=now, errors=errors)
    ladder_health, ladder_cd, ladder_hints = _parse_ladder(ladder_state_doc, now=now, errors=errors)
    worker_health, worker_cd, worker_hints = _parse_worker(
        worker_health_doc, now=now, errors=errors
    )
    receipt_admitted, receipt_reasons = _parse_receipt(admission_receipt_doc, errors=errors)

    facts: dict[str, AdmissionFact] | None = None
    if admission_facts is not None:
        facts = {}
        for rid, value in admission_facts.items():
            if not isinstance(value, Mapping):
                errors.append(f"admission_facts[{_sanitize(rid)}]: entry is not an object")
                continue
            facts[canonical_route_id(str(rid))] = AdmissionFact(
                admitted=value.get("admitted") is True,
                first_failed_stage=(
                    str(value.get("first_failed_stage"))
                    if value.get("first_failed_stage")
                    else None
                ),
                reason=str(value.get("reason", "") or ""),
            )

    policy = {canonical_route_id(str(k)): str(v) for k, v in (policy_exclusions or {}).items()}
    vis = frozenset(canonical_route_id(str(r)) for r in (visibility_unavailable or ()))

    return EvidenceSnapshots(
        at_rest=at_rest,
        at_rest_cooldowns=tuple(at_rest_cd),
        ladder_health=ladder_health,
        ladder_cooldowns=tuple(ladder_cd),
        worker_health=worker_health,
        worker_cooldowns=tuple(worker_cd),
        ladder_hints=frozenset(ladder_hints),
        worker_hints=frozenset(worker_hints),
        receipt_admitted=frozenset(receipt_admitted),
        receipt_reasons=receipt_reasons,
        admission_facts=facts,
        policy_exclusions=policy,
        visibility_unavailable=vis,
        source_errors=tuple(_scrub(e) for e in errors),
    )


# ---------------------------------------------------------------------------
# Projection internals
# ---------------------------------------------------------------------------


def _dedupe_inventory(
    inventory_rows: Sequence[Mapping[str, Any]], errors: list[str]
) -> tuple[dict[str, Mapping[str, Any]], set[str]]:
    """One row per canonical id. Contradictory duplicates fail closed."""
    rows: dict[str, Mapping[str, Any]] = {}
    contradictory: set[str] = set()
    withheld: set[str] = set()
    for row in inventory_rows:
        rid = row.get("id")
        if not isinstance(rid, str) or not rid.strip():
            continue
        canon = canonical_route_id(rid)
        if _withhold_route_id(canon):
            withheld.add(canon)
            continue
        if canon in rows:
            if not _rows_equivalent(rows[canon], row):
                if canon not in contradictory:
                    errors.append(f"inventory[{_sanitize(rid)}]: contradictory duplicate route id")
                contradictory.add(canon)
            continue
        rows[canon] = row
    if withheld:
        errors.append(f"route_id_withheld:{len(withheld)}")
    return rows, contradictory


def _rows_equivalent(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """Compare the fields the projection relies on; ignore cosmetic differences.

    Binding fields (``pool_id`` / ``subscription_pool_id`` / ``account_id``) are
    included: two inventory rows for the same canonical id that disagree on their
    pool/account binding are contradictory duplicates and must fail closed, not
    silently last-writer win (re-review non-blocking note).
    """
    keys = (
        "owned_by",
        "capabilities",
        "context_length",
        "max_input_tokens",
        "pricing",
        "pool_id",
        "subscription_pool_id",
        "account_id",
    )
    return all(a.get(k) == b.get(k) for k in keys)


def _resolve_provider_for_row(row: Mapping[str, Any], route_id: str) -> str:
    owned = str(row.get("owned_by", "") or "").lower()
    resolved = resolve_provider(owned) if owned else ""
    return resolved or owned or route_provider_prefix(route_id)


@dataclass(frozen=True)
class _CooldownIndex:
    """Scoped availability cooldowns bucketed by their binding key."""

    by_route: Mapping[str, list[ScopedCooldown]]
    by_provider: Mapping[str, list[ScopedCooldown]]
    by_pool: Mapping[str, list[ScopedCooldown]]
    by_account: Mapping[str, list[ScopedCooldown]]


def _index_cooldowns(cooldowns: Sequence[ScopedCooldown]) -> _CooldownIndex:
    """Bucket cooldowns by scope so bound pool/account blockers can be matched.

    Pool/account cooldowns are keyed by their (lower-cased) pool/account name and
    only apply to a route that is bound to that pool/account (design rule 2); they
    never sink unrelated providers' routes.
    """
    by_route: dict[str, list[ScopedCooldown]] = {}
    by_provider: dict[str, list[ScopedCooldown]] = {}
    by_pool: dict[str, list[ScopedCooldown]] = {}
    by_account: dict[str, list[ScopedCooldown]] = {}
    for cd in cooldowns:
        if cd.scope == "route":
            by_route.setdefault(cd.name, []).append(cd)
        elif cd.scope == "pool":
            by_pool.setdefault(cd.name.strip().lower(), []).append(cd)
        elif cd.scope == "account":
            by_account.setdefault(cd.name.strip().lower(), []).append(cd)
        else:
            by_provider.setdefault(cd.name, []).append(cd)
    return _CooldownIndex(
        by_route=by_route, by_provider=by_provider, by_pool=by_pool, by_account=by_account
    )


def _cooldown_sort_key(cd: ScopedCooldown) -> tuple[int, int, float, float, int]:
    return (
        _SCOPE_RANK.get(cd.scope, 2),
        _CATEGORY_RANK.get(cd.category, 9),
        -cd.until.timestamp(),
        -_epoch(cd.observed_at or cd.checked_at),
        _SOURCE_RANK.get(cd.source, 9),
    )


def _negative_deadline(
    route_id: str,
    now: datetime,
    at_rest: AtRestHealth | None,
    worker: WorkerHealth | None,
    ladder: LadderHealth | None,
) -> tuple[datetime | None, str | None]:
    """Active exact-route negative deadline (design rule 3). Equality is expired."""
    candidates: list[tuple[datetime, str]] = []
    if (
        at_rest is not None
        and not at_rest.is_positive_proof()
        and not at_rest.healthy
        and at_rest.until > now
    ):
        candidates.append((at_rest.until, "health_cache"))
    if worker is not None and worker.expires_at > now:
        candidates.append((worker.expires_at, "worker_health"))
    if ladder is not None:
        deadline = ladder.checked_at + timedelta(seconds=LADDER_HEALTH_TTL_SECONDS)
        if deadline > now:
            candidates.append((deadline, "ladder_state"))
    if not candidates:
        return None, None
    candidates.sort(key=lambda c: (-c[0].timestamp(), _SOURCE_RANK.get(c[1], 9)))
    return candidates[0]


def _capacity_for_row(
    row: Mapping[str, Any], connection: Mapping[str, Any] | None
) -> CapacityClass:
    cls, _ = capacity_class_of(connection, row)
    return cls


def _refresh_decision(
    status: VerifiedStatus, capacity: CapacityClass, half_open: bool, has_blocker: bool
) -> tuple[bool, str | None]:
    """Design section 3 refresh policy. Prepaid + non-fresh + no blocker => refreshable."""
    needs_refresh = status in {VerifiedStatus.STALE, VerifiedStatus.UNVERIFIED} or half_open
    if not needs_refresh:
        return False, None
    if status in {VerifiedStatus.INVENTORY_ONLY, VerifiedStatus.EXCLUDED}:
        return False, "not_refreshable"
    if has_blocker:
        return False, "blocked"
    if capacity in _PREPAID:
        return True, None
    # metered / unknown: never auto; requires explicit consent.
    return False, "requires_confirmation"


def _active_connections_for(
    provider_names: set[str], conn_index: Mapping[str, list[Mapping[str, Any]]]
) -> list[Mapping[str, Any]]:
    out: list[Mapping[str, Any]] = []
    seen: set[int] = set()
    for name in provider_names:
        for conn in conn_index.get(name, ()):
            if id(conn) not in seen:
                seen.add(id(conn))
                out.append(conn)
    return out


def _secondary_restrictions(
    route_id: str,
    tools: bool | None,
    at_rest: AtRestHealth | None,
    evidence: EvidenceSnapshots,
    *,
    exclude: Sequence[str] = (),
) -> list[str]:
    """Independent restriction codes to preserve even when another reason wins.

    Design section 2: "Preserve secondary restrictions even when another reason
    wins." These are evidence-backed facts about the route that remain true
    regardless of the winning status (e.g. an active cooldown also has an
    unverified identity, a declared no-tools capability, or an unavailable
    harness visibility). The winning branch's own code is passed in ``exclude``
    so it is not duplicated; the caller keeps its primary ``restriction`` field.
    """
    codes: list[str] = []
    if _identity_not_verified(at_rest):
        codes.append("identity_not_verified")
    if tools is False:
        codes.append("tools_capability_unavailable")
    if route_id in evidence.visibility_unavailable:
        codes.append("visibility_unavailable")
    excluded = set(exclude)
    return [c for c in codes if c not in excluded]


def _classify_row(
    route_id: str,
    row: Mapping[str, Any],
    *,
    now: datetime,
    evidence: EvidenceSnapshots,
    conn_index: Mapping[str, list[Mapping[str, Any]]],
    cd_index: _CooldownIndex,
    contradictory: set[str],
) -> VerifiedModelRow:
    provider = _resolve_provider_for_row(row, route_id)
    owned = str(row.get("owned_by", "") or "").lower()
    prefix = route_provider_prefix(route_id)
    provider_names = {n for n in (provider, owned, prefix) if n}

    ctx, tools, structured = _capability_facts(row)

    active_conns = [
        c for c in _active_connections_for(provider_names, conn_index) if c.get("isActive") is True
    ]
    connection = active_conns[0] if active_conns else None
    capacity = _capacity_for_row(row, connection)

    restrictions: list[str] = []
    # Independent secondary restriction codes that must survive into whatever
    # terminal branch wins (re-review finding 2 / non-blocking: secondary
    # restrictions were incomplete on early branches).  ``base_row`` merges
    # these into every row's ``restrictions`` without duplicating.
    carried: list[str] = []
    hints: list[str] = []
    if route_id in evidence.ladder_hints:
        hints.append("ladder_positive_hint")
    if route_id in evidence.worker_hints:
        hints.append("worker_positive_hint")
    if route_id in evidence.receipt_admitted:
        hints.append("receipt_admitted_hint")

    at_rest = evidence.at_rest.get(route_id)
    worker = evidence.worker_health.get(route_id)
    ladder = evidence.ladder_health.get(route_id)
    bindings = _route_bindings(route_id, row, active_conns, at_rest)
    last_success = (
        at_rest.last_success_at or (at_rest.checked_at if at_rest.healthy else None)
        if at_rest
        else None
    )
    gateway_cooldowns: list[ScopedCooldown] = []
    for c in active_conns:
        quota = QuotaEvidence.from_connection(c)
        until = quota.blocked_until(now, last_success)
        if until is not None:
            gateway_cooldowns.append(
                ScopedCooldown(
                    "route"
                    if quota.scope == "model"
                    else quota.scope
                    if quota.scope_id
                    else "provider",
                    (quota.scope_id or str(c.get("provider") or "")).lower(),
                    "rate_limited",
                    until,
                    quota.source,
                    observed_at=quota.observed_at,
                )
            )
    # Reuse account/pool path exhaustion and provider blockers from the cache projection.
    local_cd_index = _index_cooldowns(
        [
            cd
            for group in (
                cd_index.by_route,
                cd_index.by_provider,
                cd_index.by_pool,
                cd_index.by_account,
            )
            for entries in group.values()
            for cd in entries
        ]
        + gateway_cooldowns
    )
    binding_blockers, binding_restrictions = _binding_cooldowns(bindings, now, local_cd_index)
    carried.extend(binding_restrictions)

    def base_row(status: VerifiedStatus, **kw: Any) -> VerifiedModelRow:
        # Merge any carried secondary restrictions (e.g. account_binding_ambiguous)
        # into the branch's own restriction codes without duplicating, preserving
        # the branch's primary ``restriction`` field unchanged.
        existing = list(kw.pop("restrictions", ()))
        for code in carried:
            if code not in existing:
                existing.append(code)
        return VerifiedModelRow(
            route_id=route_id,
            provider=provider,
            status=status,
            context_window=ctx,
            tools=tools,
            structured=structured,
            capacity_class=capacity.value,
            hints=tuple(hints),
            availability_evidence=tuple(
                QuotaEvidence.from_connection(c).to_dict(now, last_success)
                for c in _active_connections_for(provider_names, conn_index)
            ),
            restrictions=tuple(existing),
            **kw,
        )

    # -- Rule 1: EXCLUDED / INVENTORY_ONLY ---------------------------------
    policy_reason = evidence.policy_exclusions.get(route_id)
    if policy_reason:
        restrictions.append("policy_excluded")
        restrictions.extend(
            _secondary_restrictions(
                route_id, tools, at_rest, evidence, exclude=("policy_excluded",)
            )
        )
        return base_row(
            VerifiedStatus.EXCLUDED,
            restriction="policy_excluded",
            reason=_sanitize(policy_reason),
            restrictions=tuple(restrictions),
            refresh_reason="not_refreshable",
        )
    if contradictory and route_id in contradictory:
        restrictions.append("contradictory_inventory")
        restrictions.extend(
            _secondary_restrictions(
                route_id, tools, at_rest, evidence, exclude=("contradictory_inventory",)
            )
        )
        return base_row(
            VerifiedStatus.INVENTORY_ONLY,
            restriction="contradictory_inventory",
            reason="contradictory duplicate inventory rows",
            restrictions=tuple(restrictions),
            evidence_source="inventory",
            refresh_reason="not_refreshable",
        )
    if is_opaque(route_id) or owned == "combo":
        restrictions.append("opaque_route")
        restrictions.extend(
            _secondary_restrictions(route_id, tools, at_rest, evidence, exclude=("opaque_route",))
        )
        return base_row(
            VerifiedStatus.INVENTORY_ONLY,
            restriction="opaque_route",
            reason="opaque or unaddressable inventory identity",
            restrictions=tuple(restrictions),
            evidence_source="inventory",
            refresh_reason="not_refreshable",
        )

    # Admission fact for this route (precomputed or conservatively derived).
    fact = _admission_fact(route_id, provider_names, active_conns, evidence)
    # A current admission denial is a current fact that beats positive proof: a
    # declared capability / context gate is EXCLUDED (rule 1); an entitlement /
    # auth / payment / permission / quota denial is UNAVAILABLE (rule 2). Both
    # are evaluated BEFORE any positive at-rest proof below.
    adm_block = _admission_block(fact)

    # -- Rule 1 (cont.): EXCLUDED for a declared capability / context gate -
    if adm_block is not None and adm_block[0] is VerifiedStatus.EXCLUDED:
        _status, _reason, _restriction, _source = adm_block
        restrictions.append(_restriction)
        restrictions.extend(
            _secondary_restrictions(route_id, tools, at_rest, evidence, exclude=(_restriction,))
        )
        return base_row(
            _status,
            reason=_sanitize(_reason),
            restriction=_restriction,
            restrictions=tuple(restrictions),
            evidence_source=_source,
            refresh_reason="not_refreshable",
        )

    # -- Rule 2: UNAVAILABLE (active scoped blockers beat positives) -------
    blocker = _winning_blocker(
        route_id, provider_names, now, local_cd_index, binding_blockers, at_rest, worker, ladder
    )
    if blocker is not None:
        restrictions.append("availability_blocked")
        # Design s2: preserve independent secondary restrictions even though the
        # cooldown wins the status.
        restrictions.extend(
            _secondary_restrictions(
                route_id, tools, at_rest, evidence, exclude=("availability_blocked",)
            )
        )
        cat = blocker.canonical_category or blocker.category
        return base_row(
            VerifiedStatus.UNAVAILABLE,
            reason=_sanitize(cat),
            restriction="availability_blocked",
            restrictions=tuple(restrictions),
            evidence_source=blocker.source,
            checked_at=blocker.checked_at or blocker.observed_at,
            cooldown_until=blocker.until,
            cooldown_scope=f"{blocker.scope}:{blocker.name}",
            failure_category=_sanitize(cat),
            refreshable=False,
            refresh_reason="blocked",
        )
    # Connection-level unavailability (inactive account / bad status).
    conn_block = _connection_blocker(provider_names, conn_index, active_conns, route_id, now)
    if conn_block is not None:
        restrictions.append(conn_block)
        # Preserve independent secondary restrictions (design s2).
        restrictions.extend(
            _secondary_restrictions(route_id, tools, at_rest, evidence, exclude=(conn_block,))
        )
        return base_row(
            VerifiedStatus.UNAVAILABLE,
            reason=conn_block,
            restriction=conn_block,
            restrictions=tuple(restrictions),
            evidence_source="connections",
            refreshable=False,
            refresh_reason="blocked",
        )
    # Required harness visibility unavailable for this route (rule 2).
    if route_id in evidence.visibility_unavailable:
        restrictions.append("visibility_unavailable")
        restrictions.extend(
            _secondary_restrictions(
                route_id, tools, at_rest, evidence, exclude=("visibility_unavailable",)
            )
        )
        return base_row(
            VerifiedStatus.UNAVAILABLE,
            reason="visibility_unavailable",
            restriction="visibility_unavailable",
            restrictions=tuple(restrictions),
            evidence_source="harness_visibility",
            refreshable=False,
            refresh_reason="blocked",
        )
    # Current entitlement / auth / payment / permission / quota denial (rule 2).
    if adm_block is not None and adm_block[0] is VerifiedStatus.UNAVAILABLE:
        _status, _reason, _restriction, _source = adm_block
        restrictions.append(_restriction)
        restrictions.extend(
            _secondary_restrictions(route_id, tools, at_rest, evidence, exclude=(_restriction,))
        )
        return base_row(
            _status,
            reason=_sanitize(_reason),
            restriction=_restriction,
            restrictions=tuple(restrictions),
            evidence_source=_source,
            refreshable=False,
            refresh_reason="blocked",
        )

    # -- Rule 3: FAILED (active exact-route diagnostic negative) -----------
    deadline, neg_source = _negative_deadline(route_id, now, at_rest, worker, ladder)
    if deadline is not None:
        neg = _negative_detail(route_id, neg_source, at_rest, worker, ladder)
        restrictions.append("route_negative")
        restrictions.extend(
            _secondary_restrictions(route_id, tools, at_rest, evidence, exclude=("route_negative",))
        )
        return base_row(
            VerifiedStatus.FAILED,
            reason=_sanitize(neg["category"]),
            restriction="route_negative",
            restrictions=tuple(restrictions),
            evidence_source=neg_source,
            checked_at=neg["checked_at"],
            expires_at=deadline,
            freshness="negative",
            failure_category=_sanitize(neg["category"]),
            http_status=neg["http_status"],
            cooldown_until=deadline,
            refreshable=False,
            refresh_reason="blocked",
            _sort_checked=neg["checked_at"],
        )

    # -- Rule 4/5: VERIFIED / STALE (validated at-rest positive) -----------
    if at_rest is not None and at_rest.is_positive_proof():
        age = (now - at_rest.checked_at).total_seconds()
        fresh_until = at_rest.checked_at + timedelta(seconds=FRESH_SECONDS)
        expires_at = at_rest.checked_at + timedelta(seconds=USABLE_SECONDS)
        last_success = at_rest.last_success_at or at_rest.checked_at
        coding_ok = at_rest.tool_ok and tools is not False
        if not coding_ok:
            restrictions.append("chat_only_not_coding_verified")
        agentic_fresh = (
            at_rest.agentic_ok
            and at_rest.agentic_checked_at is not None
            and (now - at_rest.agentic_checked_at).total_seconds() < FRESH_SECONDS
        )
        if not agentic_fresh:
            restrictions.append("agentic_not_fresh")
        if age < FRESH_SECONDS:
            status = VerifiedStatus.VERIFIED
            freshness = "fresh"
        else:
            status = VerifiedStatus.STALE
            freshness = "stale" if age < USABLE_SECONDS else "expired"
            restrictions.append(f"stale_{freshness}")
            coding_ok = False
        refreshable, refresh_reason = _refresh_decision(status, capacity, False, False)
        return base_row(
            status,
            coding_ok=coding_ok,
            last_success_at=last_success,
            checked_at=at_rest.checked_at,
            evidence_source="health_cache",
            fresh_until=fresh_until,
            expires_at=expires_at,
            freshness=freshness,
            restriction=restrictions[0] if restrictions else None,
            restrictions=tuple(restrictions),
            failure_category=None,
            http_status=at_rest.http_status,
            latency_ms=at_rest.latency_ms,
            identity=at_rest.identity or None,
            probe_class=at_rest.probe_class,
            agentic_ok=at_rest.agentic_ok,
            agentic_checked_at=at_rest.agentic_checked_at,
            refreshable=refreshable,
            refresh_reason=refresh_reason,
            _sort_checked=at_rest.checked_at,
        )

    # -- Rule 6: UNVERIFIED (admissible, no proof / no active negative) ----
    if fact.admitted:
        half_open = _is_half_open(route_id, now, at_rest, worker)
        reason = (
            "identity_not_verified"
            if _identity_not_verified(at_rest)
            else ("half_open" if half_open else "admitted_no_proof")
        )
        if reason == "identity_not_verified":
            restrictions.append("identity_not_verified")
        if half_open:
            restrictions.append("half_open")
        refreshable, refresh_reason = _refresh_decision(
            VerifiedStatus.UNVERIFIED, capacity, half_open, False
        )
        return base_row(
            VerifiedStatus.UNVERIFIED,
            reason=reason,
            restriction=restrictions[0] if restrictions else None,
            restrictions=tuple(restrictions),
            evidence_source="admission",
            freshness="none",
            identity=(at_rest.identity or None) if at_rest is not None else None,
            refreshable=refreshable,
            refresh_reason=refresh_reason,
        )

    # -- Rule 7: non-admissible -> INVENTORY_ONLY / UNAVAILABLE ------------
    if fact.first_failed_stage == "ENTITLED" or fact.reason in {
        "no_connection_evidence",
        "no_active_account",
    }:
        # Restriction is a fixed code; the (possibly external) reason is scrubbed
        # and carries only the sanitized detail (re-review finding 1).
        code = fact.reason if fact.reason in _RESTRICTION_CODES else "no_connection_evidence"
        restrictions.append(code)
        restrictions.extend(
            _secondary_restrictions(route_id, tools, at_rest, evidence, exclude=(code,))
        )
        return base_row(
            VerifiedStatus.UNAVAILABLE,
            reason=_sanitize(fact.reason) or "no_connection_evidence",
            restriction=code,
            restrictions=tuple(restrictions),
            evidence_source="connections",
            refreshable=False,
            refresh_reason="blocked",
        )
    # Unknown / undetermined admission: the external reason text is never a
    # restriction code.  Store the fixed ``not_admissible`` code and keep the
    # redacted detail only in the sanitized ``reason`` (re-review finding 1).
    code = fact.reason if fact.reason in _RESTRICTION_CODES else "not_admissible"
    restrictions.append(code)
    restrictions.extend(
        _secondary_restrictions(route_id, tools, at_rest, evidence, exclude=(code,))
    )
    return base_row(
        VerifiedStatus.INVENTORY_ONLY,
        reason=_sanitize(fact.reason) or "not_admissible",
        restriction=code,
        restrictions=tuple(restrictions),
        evidence_source="inventory",
        refresh_reason="not_refreshable",
    )


@dataclass(frozen=True)
class _ConnectionScopes:
    pools: frozenset[str]
    accounts: frozenset[str]


@dataclass(frozen=True)
class _RouteBindings:
    """Explicit scopes and active connection paths associated with a route."""

    pools: set[str]
    accounts: set[str]
    connections: tuple[_ConnectionScopes, ...]


def _binding_names(row: Mapping[str, Any], *keys: str) -> set[str]:
    return {text for key in keys if (text := str(row.get(key) or "").strip().lower())}


def _route_bindings(
    route_id: str,
    row: Mapping[str, Any],
    active_conns: Sequence[Mapping[str, Any]],
    at_rest: AtRestHealth | None,
) -> _RouteBindings:
    """Associate active paths using both explicit pool and account constraints."""
    pools = _binding_names(row, "subscription_pool_id", "pool_id")
    # Inventory may carry the raw account id; sanitized connections carry the
    # opaque token. Match either form (same rule as admission).
    raw_account = str(row.get("account_id") or "")
    accounts = {form.lower() for form in account_forms(raw_account)}
    parts = route_id.split("/")
    prefix = route_provider_prefix(route_id)
    owned = str(row.get("owned_by", "") or "").lower()
    if len(parts) > 2 and owned.startswith(prefix + "/"):
        accounts.add(parts[1].strip().lower())
    if at_rest is not None and at_rest.pool:
        pools.add(at_rest.pool.strip().lower())
    associated: list[_ConnectionScopes] = []
    for c in active_conns:
        conn_pools = _binding_names(c, "pool_id", "subscription_pool_id")
        conn_accounts = _binding_names(c, "account_id", "id")
        if (not pools or pools & conn_pools) and (not accounts or accounts & conn_accounts):
            associated.append(_ConnectionScopes(frozenset(conn_pools), frozenset(conn_accounts)))
    return _RouteBindings(pools, accounts, tuple(associated))


def _binding_cooldowns(
    bindings: _RouteBindings, now: datetime, cd_index: _CooldownIndex
) -> tuple[list[ScopedCooldown], list[str]]:
    """Sink explicit matches or exhausted paths; disclose partial path blocks."""

    def matches(
        pools: Sequence[str] | set[str] | frozenset[str],
        accounts: Sequence[str] | set[str] | frozenset[str],
    ) -> list[ScopedCooldown]:
        return [
            cd
            for names, index in ((pools, cd_index.by_pool), (accounts, cd_index.by_account))
            for name in names
            for cd in index.get(name, ())
            if cd.until > now
        ]

    explicit = matches(bindings.pools, bindings.accounts)
    paths = [matches(c.pools, c.accounts) for c in bindings.connections]
    if paths and all(paths):
        return [*explicit, *(cd for path in paths for cd in path)], []
    if explicit:
        return explicit, []
    restrictions = [
        f"{kind}_binding_ambiguous"
        for kind in ("pool", "account")
        if any(cd.scope == kind for path in paths for cd in path)
    ]
    return [], restrictions


def _winning_blocker(
    route_id: str,
    provider_names: set[str],
    now: datetime,
    cd_index: _CooldownIndex,
    binding_blockers: Sequence[ScopedCooldown],
    at_rest: AtRestHealth | None,
    worker: WorkerHealth | None,
    ladder: LadderHealth | None,
) -> ScopedCooldown | None:
    """Pick the winning active availability blocker across all stores (design rule 2)."""
    active: list[ScopedCooldown] = []
    for cd in cd_index.by_route.get(route_id, ()):
        if cd.until > now:
            active.append(cd)
    for name in provider_names:
        for cd in cd_index.by_provider.get(name, ()):
            if cd.until > now:
                active.append(cd)
    # Explicit matches or exhaustion of every associated connection path.
    active.extend(binding_blockers)
    # At-rest / worker negatives whose category is an availability category also
    # block here (auth/payment/permission/rate-limit), even if only a route
    # negative deadline is known.
    for entry, source in ((at_rest, "health_cache"), (worker, "worker_health")):
        if entry is None:
            continue
        category = entry.category
        if category not in _AVAILABILITY_CATEGORIES:
            continue
        until = entry.until if isinstance(entry, AtRestHealth) else entry.expires_at
        if until <= now:
            continue
        active.append(
            ScopedCooldown(
                scope="route",
                name=route_id,
                category=category,
                until=until,
                source=source,
                checked_at=getattr(entry, "checked_at", None),
                observed_at=getattr(entry, "observed_at", None),
            )
        )
    if ladder is not None and ladder.category in _AVAILABILITY_CATEGORIES:
        until = ladder.checked_at + timedelta(seconds=LADDER_HEALTH_TTL_SECONDS)
        if until > now:
            active.append(
                ScopedCooldown(
                    scope="route",
                    name=route_id,
                    category=ladder.category,
                    until=until,
                    source="ladder_state",
                    checked_at=ladder.checked_at,
                )
            )
    if not active:
        return None
    active.sort(key=_cooldown_sort_key)
    return active[0]


def _connection_blocker(
    provider_names: set[str],
    conn_index: Mapping[str, list[Mapping[str, Any]]],
    active_conns: Sequence[Mapping[str, Any]],
    route_id: str,
    now: datetime,
) -> str | None:
    """Connection availability (design rule 2 / rule 7 distinction)."""
    conns = _active_connections_for(provider_names, conn_index)
    if not conns:
        return "no_connection_evidence"
    if not active_conns:
        return "no_active_account"
    statuses = {str(c.get("testStatus", "") or "").strip().lower() for c in active_conns}
    if statuses and statuses <= _BAD_TEST_STATUS:
        return f"connection_status:{sorted(statuses)[0]}"
    return None


def _negative_detail(
    route_id: str,
    source: str | None,
    at_rest: AtRestHealth | None,
    worker: WorkerHealth | None,
    ladder: LadderHealth | None,
) -> dict[str, Any]:
    if source == "health_cache" and at_rest is not None:
        return {
            "category": at_rest.category or "unhealthy",
            "checked_at": at_rest.checked_at,
            "http_status": at_rest.http_status,
        }
    if source == "worker_health" and worker is not None:
        return {
            "category": worker.category or "unhealthy",
            "checked_at": worker.observed_at,
            "http_status": worker.status_code,
        }
    if source == "ladder_state" and ladder is not None:
        return {
            "category": ladder.category or "unhealthy",
            "checked_at": ladder.checked_at,
            "http_status": None,
        }
    return {"category": "unhealthy", "checked_at": None, "http_status": None}


def _is_half_open(
    route_id: str, now: datetime, at_rest: AtRestHealth | None, worker: WorkerHealth | None
) -> bool:
    """A recently-expired negative with no fresh positive is half-open."""
    if (
        at_rest is not None
        and not at_rest.is_positive_proof()
        and not at_rest.healthy
        and at_rest.until <= now
    ):
        return True
    return worker is not None and worker.expires_at <= now


def _identity_not_verified(at_rest: AtRestHealth | None) -> bool:
    """A healthy at-rest entry that never echoed a verified identity."""
    if at_rest is None:
        return False
    return (
        at_rest.healthy
        and at_rest.category == CATEGORY_OK
        and at_rest.chat_ok
        and at_rest.identity != "verified"
    )


def _admission_block(fact: AdmissionFact) -> tuple[VerifiedStatus, str, str, str] | None:
    """Map a current admission denial to an early EXCLUDED/UNAVAILABLE block.

    Design rules 1-2: a declared capability / context gate is ``EXCLUDED``; an
    entitlement / auth / payment / permission / quota denial is ``UNAVAILABLE``.
    Both are current facts and are evaluated BEFORE any positive proof. Only
    fires when the route is not admitted. Other stages (policy / controller /
    downstream / unknown) return ``None`` and fall through to the inventory /
    non-admissible rules unchanged.

    Returns ``(status, reason, restriction, evidence_source)`` or ``None``.
    """
    if fact.admitted:
        return None
    stage = (fact.first_failed_stage or "").strip().upper()
    reason = (fact.reason or "").strip().lower()
    if stage in _CAPABILITY_STAGES:
        return (
            VerifiedStatus.EXCLUDED,
            fact.reason or "capability_gate",
            "capability_excluded",
            "policy",
        )
    if stage in _ENTITLEMENT_STAGES or reason in _DENIAL_REASONS:
        return (
            VerifiedStatus.UNAVAILABLE,
            fact.reason or "entitlement_denied",
            "entitlement_denied",
            "admission",
        )
    return None


def _admission_fact(
    route_id: str,
    provider_names: set[str],
    active_conns: Sequence[Mapping[str, Any]],
    evidence: EvidenceSnapshots,
) -> AdmissionFact:
    """Current unscoped admission. Prefer precomputed facts; else derive conservatively."""
    if evidence.admission_facts is not None:
        fact = evidence.admission_facts.get(route_id)
        if fact is not None:
            return fact
        # Caller supplied facts but omitted this route: treat as unknown/non-admissible.
        return AdmissionFact(admitted=False, first_failed_stage=None, reason="admission_unknown")
    # Conservative derivation from inventory + connections only: an active,
    # non-bad connection for the provider is necessary but not sufficient proof,
    # so the route is admitted-for-refresh but carries an explicit unknown note.
    viable = [
        c
        for c in active_conns
        if str(c.get("testStatus", "") or "").strip().lower() not in _BAD_TEST_STATUS
    ]
    if viable:
        return AdmissionFact(admitted=True, first_failed_stage=None, reason="admission_unknown")
    if active_conns:
        return AdmissionFact(
            admitted=False, first_failed_stage="ENTITLED", reason="connection_status_bad"
        )
    return AdmissionFact(
        admitted=False, first_failed_stage="ENTITLED", reason="no_connection_evidence"
    )


def _capability_facts(row: Mapping[str, Any]) -> tuple[int | None, bool | None, bool | None]:
    """Read ``(context_window, tools, structured)`` from an inventory row.

    Mirrors ``eligibility._capability_facts``: unknown stays ``None`` rather than a
    fabricated default. ``context_length`` / ``max_input_tokens`` are the two
    inventory shapes; ``tool_calling`` / ``structured_output`` are the capability
    keys the task gate reads.
    """
    ctx_raw = row.get("max_input_tokens")
    if ctx_raw is None:
        ctx_raw = row.get("context_length")
    context_window: int | None
    if isinstance(ctx_raw, bool) or ctx_raw is None:
        context_window = None
    else:
        try:
            parsed = int(ctx_raw)
        except (TypeError, ValueError):
            context_window = None
        else:
            context_window = parsed if parsed > 0 else None
    caps_raw = row.get("capabilities")
    tools: bool | None = None
    structured: bool | None = None
    if isinstance(caps_raw, Mapping):
        if "tool_calling" in caps_raw:
            tools = _bool_or_none(caps_raw.get("tool_calling"))
            if tools is None:
                tools = bool(caps_raw.get("tool_calling"))
        if "structured_output" in caps_raw:
            structured = _bool_or_none(caps_raw.get("structured_output"))
            if structured is None:
                structured = bool(caps_raw.get("structured_output"))
    return context_window, tools, structured


def _sort_key(row: VerifiedModelRow) -> tuple[Any, ...]:
    status_rank = _STATUS_RANK[row.status]
    if row.status == VerifiedStatus.VERIFIED:
        latency = row.latency_ms if row.latency_ms is not None else math.inf
        return (status_rank, -_epoch(row.last_success_at), latency, row.route_id)
    return (status_rank, -_epoch(row._sort_checked), row.route_id)


def _validate_query(
    query: VerifiedModelQuery,
) -> tuple[VerifiedStatus | None, str | None, str | None, int, int]:
    status: VerifiedStatus | None = None
    if query.status is not None:
        want = str(query.status).strip().upper()
        if not want:
            status = None
        else:
            try:
                status = VerifiedStatus(want)
            except ValueError as exc:
                raise ValueError(f"invalid status filter: {query.status!r}") from exc
    provider = None
    if query.provider is not None:
        provider = str(query.provider).strip().lower() or None
        if provider is not None:
            provider = resolve_provider(provider)
    search = None
    if query.search is not None:
        search = str(query.search).strip().lower() or None
    if not isinstance(query.page, int) or isinstance(query.page, bool) or query.page < 1:
        raise ValueError(f"page must be a positive integer, got {query.page!r}")
    if (
        not isinstance(query.page_size, int)
        or isinstance(query.page_size, bool)
        or query.page_size < 1
    ):
        raise ValueError(f"page_size must be a positive integer, got {query.page_size!r}")
    page_size = min(query.page_size, MAX_PAGE_SIZE)
    return status, provider, search, query.page, page_size


def _matches(
    row: VerifiedModelRow, status: VerifiedStatus | None, provider: str | None, search: str | None
) -> bool:
    if status is not None and row.status != status:
        return False
    if provider is not None and resolve_provider(row.provider.lower()) != provider:
        return False
    if search is not None:
        hay = " ".join((row.route_id, row.provider, row.reason or "")).lower()
        if search not in hay:
            return False
    return True


def project_verified_models(
    inventory_rows: Sequence[Mapping[str, Any]],
    connections: Sequence[Mapping[str, Any]] | None,
    evidence: EvidenceSnapshots,
    *,
    now: datetime,
    query: VerifiedModelQuery = VerifiedModelQuery(),
) -> VerifiedModelsView:
    """Project evidenced model identities to one status each (pure; design sections 1-3).

    ``now`` is required and must be timezone-aware. No disk/network/clock reads,
    no writes, no probes. Invalid filters/paging raise ``ValueError`` before work.
    """
    now = _require_aware(now)
    status_filter, provider_filter, search_filter, page, page_size = _validate_query(query)

    errors = list(evidence.source_errors)
    rows_by_id, contradictory = _dedupe_inventory(inventory_rows, errors)

    conn_index: dict[str, list[Mapping[str, Any]]] = {}
    for conn in connections or ():
        name = str(conn.get("provider", "") or "").strip().lower()
        if name:
            conn_index.setdefault(name, []).append(conn)

    all_cooldowns = (
        list(evidence.at_rest_cooldowns)
        + list(evidence.ladder_cooldowns)
        + list(evidence.worker_cooldowns)
    )
    cd_index = _index_cooldowns(all_cooldowns)

    projected: list[VerifiedModelRow] = []
    for route_id in rows_by_id:
        projected.append(
            _classify_row(
                route_id,
                rows_by_id[route_id],
                now=now,
                evidence=evidence,
                conn_index=conn_index,
                cd_index=cd_index,
                contradictory=contradictory,
            )
        )

    projected.sort(key=_sort_key)

    counts: dict[str, int] = {s.value: 0 for s in _STATUS_ORDER}
    for row in projected:
        counts[row.status.value] += 1
    total_count = len(projected)

    filtered = [r for r in projected if _matches(r, status_filter, provider_filter, search_filter)]
    filtered_counts: dict[str, int] = {s.value: 0 for s in _STATUS_ORDER}
    for row in filtered:
        filtered_counts[row.status.value] += 1
    filtered_count = len(filtered)

    if filtered_count == 0:
        page_count = 0
        page_index = 1
        page_rows: list[VerifiedModelRow] = []
    else:
        page_count = (filtered_count + page_size - 1) // page_size
        page_index = min(page, page_count)
        start = (page_index - 1) * page_size
        page_rows = filtered[start : start + page_size]

    return VerifiedModelsView(
        generated_at=now,
        rows=tuple(page_rows),
        counts_by_status=counts,
        filtered_counts_by_status=filtered_counts,
        total_count=total_count,
        filtered_count=filtered_count,
        filters={
            "status": status_filter.value if status_filter else None,
            "provider": provider_filter,
            "search": search_filter,
        },
        page=page_index,
        page_size=page_size,
        page_count=page_count,
        source_errors=tuple(_scrub(e) for e in errors),
    )


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "SCHEMA",
    "AdmissionFact",
    "AtRestHealth",
    "EvidenceSnapshots",
    "LadderHealth",
    "ScopedCooldown",
    "VerifiedModelQuery",
    "VerifiedModelRow",
    "VerifiedModelsView",
    "VerifiedStatus",
    "WorkerHealth",
    "project_verified_models",
    "snapshots_from_documents",
]
