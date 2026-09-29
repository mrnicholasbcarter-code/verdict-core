"""Provider catalog: alias resolution, backend pool identity, and capacity helpers.

Resolves owned_by aliases that the OmniRoute inventory uses but that do not
match any connection provider name.  Identifies backend pools so two prefixes
backed by the same quota are never treated as independent.

Evidence sources:
- /tmp/research-empirical.md: agy≡antigravity (identical tool-call ids),
  kilocode :free ≡ openrouter :free (same upstream generation id).
- /tmp/research-or-providers.json: connection list with importFreeModelsOnly,
  authType, providerSpecificData.tier fields.
- /tmp/research-audit.md §5: 274 routes with unmapped owned_by values.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from verdict.security import redact_text

# ---------------------------------------------------------------------------
# A. Owned-by alias table
#
# The OmniRoute inventory sets ``owned_by`` to a UI-internal name that may
# differ from the connection ``provider`` string.  Every entry below is a
# concrete defect observed in the recorded inventory (research-audit.md §5).
# ---------------------------------------------------------------------------

OWNED_BY_ALIASES: Mapping[str, str] = {
    # Evidence: research-audit.md §5 — 137 routes owned_by=devin-cli-agentic,
    # connection provider is "devin-cli" (inactive).
    "devin-cli-agentic": "devin-cli",
    # Evidence: 28 routes owned_by=auggie, no connection. Closest match is
    # the inactive "openference" connection (web agent). Named gap.
    "auggie": "auggie",
    # Evidence: 26 routes owned_by=codex-app-server, connection is "codex".
    "codex-app-server": "codex",
    # Evidence: 20 routes owned_by=cloudflare-playground, no connection.
    "cloudflare-playground": "cloudflare-playground",
    # Evidence: 6 routes, no connection.
    "duckduckgo-web": "duckduckgo-web",
    # Evidence: 5 routes, no connection.
    "felo-web": "felo-web",
    # Evidence: 5 routes, no connection.
    "uncloseai": "uncloseai",
    # Evidence: 4 routes, no connection.
    "veoaifree-web": "veoaifree-web",
    # Evidence: 1 route, no connection.
    "chipotle": "chipotle",
}
"""Maps ``owned_by`` values that appear in the inventory but match no
connection ``provider`` string.  A value that maps to itself is a *named gap*:
no connection can serve it, but the gap is explicitly acknowledged rather
than silently dropped.
"""


# ---------------------------------------------------------------------------
# B. Backend pool identity
#
# Two OmniRoute prefixes can share one upstream quota.  Selection,
# reviewer-independence, and ``route_family`` must treat routes in the same
# pool as *not independent*.
# ---------------------------------------------------------------------------

# Hard-coded pools derived from empirical evidence.
# Evidence: research-empirical.md — agy and antigravity returned identical
# tool-call ids (toolu_vrtx_01RNdPf2… for sonnet, call_4180c5fb… for gpt-oss).
# They are one Google Antigravity quota under two prefixes.
_POOL_BY_PREFIX: Mapping[str, str] = {
    "agy": "google-antigravity",
    "antigravity": "google-antigravity",
}

# Evidence: research-empirical.md — kilocode :free and openrouter :free for
# north-mini-code returned the same upstream generation id (gen-1790657873-…).
_FREE_SUFFIX_POOL: Mapping[str, str] = {
    "kc": "openrouter-free",
    "kilocode": "openrouter-free",
    "openrouter": "openrouter-free",
}


def backend_pool(route_id: str) -> str:
    """Return a pool identity string for ``route_id``.

    Routes in the same pool share quota and must not be considered independent.
    The returned string is opaque to callers; only equality matters.

    Rules (priority order):
    1. ``*:free`` suffix on a known prefix → the shared free pool.
    2. Prefix in the hard-coded pool table → the table value.
    3. Otherwise → the connection-derived prefix itself (each prefix is its
       own pool by default).
    """
    prefix = route_id.split("/", 1)[0].lower()
    if route_id.endswith(":free") and prefix in _FREE_SUFFIX_POOL:
        return _FREE_SUFFIX_POOL[prefix]
    if prefix in _POOL_BY_PREFIX:
        return _POOL_BY_PREFIX[prefix]
    return prefix


def resolve_provider(owned_by: str, route_id: str) -> str:
    """Resolve the effective provider name for connection lookup.

    Checks the alias table first, then falls back to ``owned_by`` as-is
    (which is what the existing code does).
    """
    lower = owned_by.lower()
    if lower in OWNED_BY_ALIASES:
        return OWNED_BY_ALIASES[lower]
    return lower


# ---------------------------------------------------------------------------
# C. Pool-aware independence
#
# A single helper used by both review.py (exclude_families) and claims.py
# (reviewer independence).
# ---------------------------------------------------------------------------


def routes_share_pool(route_a: str, route_b: str) -> bool:
    """True when two routes share the same backend pool.

    Use this instead of comparing prefixes or route_family when the question
    is "are these two routes actually independent capacity?"
    """
    return backend_pool(route_a) == backend_pool(route_b)


def pool_aware_families(route_ids: frozenset[str] | set[str]) -> frozenset[str]:
    """Return the set of backend pools for a collection of route ids.

    Like ``route_family`` but uses pool identity, so agy/* and antigravity/*
    both map to the same pool string.
    """
    return frozenset(backend_pool(rid) for rid in route_ids)


# Pools that alias two or more prefixes onto one quota.  Default
# prefix-as-pool identities are omitted: those are not extra independence
# constraints beyond the prefix itself.
ALIASED_POOLS: frozenset[str] = frozenset(_POOL_BY_PREFIX.values()) | frozenset(
    _FREE_SUFFIX_POOL.values()
)


def aliased_pools_for(route_ids: frozenset[str] | set[str]) -> frozenset[str]:
    """Return cross-prefix aliased pools used by ``route_ids``.

    ``agy/*`` and ``antigravity/*`` share ``google-antigravity``;
    kc/kilocode/openrouter ``:free`` share ``openrouter-free``.  Same-prefix
    default pools (``kr``, ``cc``, ...) are omitted so reviewer
    independence still allows a different family on the same prefix.
    """
    return frozenset(p for p in pool_aware_families(route_ids) if p in ALIASED_POOLS)


# ---------------------------------------------------------------------------
# D. Capacity classification helpers
# ---------------------------------------------------------------------------


def has_free_suffix(route_id: str) -> bool:
    """True when the route id ends with ``:free``."""
    return route_id.endswith(":free")


_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")


def sanitized_plan_label(value: object) -> str:
    """Return a display-safe plan label with credentials and email addresses removed."""
    return _EMAIL.sub("[redacted]", redact_text(value))


def connection_signals_free(conn: Mapping[str, Any]) -> tuple[bool, str]:
    """Check whether a connection signals free-tier access.

    Returns ``(is_free, rule)`` where ``rule`` names the signal that fired.

    Extra rules restored from origin/main ``_capacity_class``:
    ``import_free_only`` (snake_case, used by tests and sanitize_connections)
    and ``plan_label`` containing ``free`` for any auth type.
    """
    if conn.get("importFreeModelsOnly") is True:
        return True, "importFreeModelsOnly"
    if bool(conn.get("import_free_only")):
        return True, "import_free_only"
    psd = conn.get("providerSpecificData")
    if isinstance(psd, Mapping):
        if psd.get("importFreeModelsOnly") is True:
            return True, "importFreeModelsOnly"
        tier = str(psd.get("tier", "")).lower()
        if tier and "free" in tier:
            return True, "providerSpecificData.tier"
        plan = str(psd.get("plan", "")).lower()
        if plan and "free" in plan:
            return True, "providerSpecificData.plan"
    plan_label = str(conn.get("plan_label", "")).lower()
    if "free" in plan_label:
        return True, "plan_label"
    return False, ""


# ---------------------------------------------------------------------------
# E. Catalog ghost classification
# ---------------------------------------------------------------------------

# TTL for catalog-stale entries (ghosts).  The live catalog changes rarely;
# re-probing ghosts sooner than this wastes budget.
CATALOG_STALE_COOLDOWN_SECONDS: float = 21600.0  # 6 hours

_CATALOG_STALE_PHRASES = ("not available in the active live catalog",)


def is_catalog_stale_error(error_message: str) -> bool:
    """True when ``error_message`` indicates a catalog ghost.

    OmniRoute returns 400 with this message for ids that appear in
    ``/v1/models`` but are not in the provider's live catalog.
    """
    lower = error_message.lower()
    return any(phrase in lower for phrase in _CATALOG_STALE_PHRASES)


# ---------------------------------------------------------------------------
# F. Live-evidence override (not-free marking)
# ---------------------------------------------------------------------------

# Probe result categories that prove a route or pool is not actually free.
NOT_FREE_CATEGORIES: frozenset[str] = frozenset({"payment_required", "insufficient_credits"})

# Error messages / HTTP status codes that indicate a route is not free,
# even if statically classified as FREE.
_NOT_FREE_STATUS_CODES: frozenset[int] = frozenset({402})

_NOT_FREE_PHRASES = (
    "insufficient credits",
    "payment required",
    "deposit required",
    "purchase more credits",
    "credits exhausted",
)


def is_not_free_signal(
    *, category: str = "", status_code: int | None = None, error_message: str = ""
) -> bool:
    """True when probe evidence shows the route is not actually free."""
    if category in NOT_FREE_CATEGORIES:
        return True
    if status_code in _NOT_FREE_STATUS_CODES:
        return True
    lower = error_message.lower()
    return any(phrase in lower for phrase in _NOT_FREE_PHRASES)


NOT_FREE_OVERRIDE_TTL_SECONDS: float = 21600.0  # 6 hours


def record_not_free_override(
    state: dict[str, Any], route_id: str, *, pool: str, until_iso: str, reason: str
) -> None:
    """Record a not-free override in the ladder state.

    Marks both the individual route and its pool so that FREE-tier ranking
    can exclude them.  Story 2's daemon will call this after a probe result
    triggers ``is_not_free_signal``.

    The override is stored under ``state["not_free_overrides"]`` as::

        { "route:<route_id>": {"until": "<iso>", "pool": "<pool>", "reason": "..."},
          "pool:<pool>":      {"until": "<iso>", "reason": "..."} }
    """
    overrides = state.setdefault("not_free_overrides", {})
    entry = {"until": until_iso, "reason": reason}
    overrides[f"route:{route_id}"] = {**entry, "pool": pool}
    # Pool-level: only extend, never shorten an existing override.
    pool_key = f"pool:{pool}"
    existing = overrides.get(pool_key)
    if not isinstance(existing, dict) or str(existing.get("until", "")) < until_iso:
        overrides[pool_key] = dict(entry)


def is_not_free_overridden(
    state: Mapping[str, Any], route_id: str, *, pool: str, now_iso: str
) -> tuple[bool, str]:
    """Check whether a route or its pool has a live not-free override.

    Returns ``(overridden, reason)``.
    """
    overrides = state.get("not_free_overrides")
    if not isinstance(overrides, Mapping):
        return False, ""
    for key in (f"route:{route_id}", f"pool:{pool}"):
        entry = overrides.get(key)
        if isinstance(entry, dict) and str(entry.get("until", "")) > now_iso:
            return True, str(entry.get("reason", "not_free_override"))
    return False, ""
