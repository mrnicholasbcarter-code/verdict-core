"""Credential-pool identity for the BOD-297 pool-aware census.

Several OmniRoute alias prefixes bill against the same upstream
credential/quota as another prefix. Probing every alias independently
wastes budget and can trip provider-side rate limiting (the 2026-10-09
agy/antigravity 504/429 incident recorded in BOD-297's accepted criteria).
This module answers "what quota does this route draw from" and "what is
the one prefix to actually probe for this family".

Deliberately separate from ``verdict.orchestration.provider_catalog``
(``backend_pool`` / ``OWNED_BY_ALIASES``), which serves eligibility /
reviewer-independence under its own pool names. Neither module reads the
other.

Evidence (2026-10-09 controller census, /tmp/verdict-census and
tests/fixtures/catalog_truth/*.json):
  - agy/antigravity: research-empirical.md — identical tool-call ids
    observed across both prefixes (one Google Antigravity quota).
  - af/api-airforce, bm/bluesminds, kc/kilocode, gh/github, cc/claude,
    cx/codex, oc/opencode, zm/zenmux: short and long prefix both appear in
    the live inventory with matching, equal-count model sets, and the
    short prefix's ``owned_by`` resolves to the long one.
  - ollama-cloud/ollamacloud: ``ollamacloud`` rows carry
    ``owned_by: "ollama-cloud"``, the real connection name.
  - sambanova/samba: free_census3 ledger shows equal attempt counts (8/8)
    under both; no connections fixture distinguishes them, so the full
    vendor name is kept canonical.
  - cu/cua vs cursor/cursor-api: NOT merged. ``cursor`` (oauth,
    testStatus "unavailable") and ``cursor-api`` (apikey, "active") are
    distinct connections with no shared-quota evidence, so each pair stays
    its own pool: cu<->cursor, cua<->cursor-api.
  - dv/dva vs devin-cli/devin-desktop: ``devin-cli`` and ``devin-desktop``
    are separate connections in the same fixture. ``dva``'s ``owned_by``
    (``devin-cli-agentic``) resolves only to ``devin-cli`` via
    ``provider_catalog.OWNED_BY_ALIASES``; no evidence ties any route to
    ``devin-desktop`` or to a bare ``dv`` prefix. Fail-closed choice, noted
    rather than silently assumed: ``devin-desktop`` is left out of this
    table (kept its own, separate pool); ``dv`` is mapped like ``dva`` as
    the simplest guess.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping

# alias prefix (lower-cased) -> canonical pool / prefix name.
ALIAS_FAMILIES: Mapping[str, str] = {
    "agy": "antigravity",
    "antigravity": "antigravity",  # research-empirical.md tool-call ids
    "af": "api-airforce",
    "api-airforce": "api-airforce",  # equal model sets, owned_by resolves
    "ollama-cloud": "ollama-cloud",
    "ollamacloud": "ollama-cloud",  # owned_by=ollama-cloud
    "bm": "bluesminds",
    "bluesminds": "bluesminds",  # equal model sets, owned_by resolves
    "kc": "kilocode",
    "kilocode": "kilocode",  # equal model sets, owned_by resolves
    "gh": "github",
    "github": "github",  # equal model sets, owned_by resolves
    "cc": "claude",
    "claude": "claude",  # equal model sets, owned_by resolves
    "cx": "codex",
    "codex": "codex",  # equal model sets, owned_by resolves
    "oc": "opencode",
    "opencode": "opencode",  # equal model sets, owned_by resolves
    "zm": "zenmux",
    "zenmux": "zenmux",  # equal model sets, owned_by resolves
    "sambanova": "sambanova",
    "samba": "sambanova",  # free-attempts.json 8/8 parity
    "cu": "cursor",
    "cursor": "cursor",  # distinct connection, own pool
    "cua": "cursor-api",
    "cursor-api": "cursor-api",  # distinct connection, own pool
    "dv": "devin-cli",
    "dva": "devin-cli",  # owned_by=devin-cli-agentic -> devin-cli only
}

_EFFORT = r"(?:low|medium|high|xhigh|max)"
_STRIP_THINKING_EFFORT = re.compile(rf"-thinking-{_EFFORT}$")
_STRIP_EFFORT = re.compile(rf"-{_EFFORT}$")


def pool_of(route_id: str) -> str:
    """Canonical credential-pool name for ``route_id``.

    Unaliased prefixes are their own pool (same default as
    ``provider_catalog.backend_pool``).
    """
    prefix = route_id.split("/", 1)[0].lower()
    return ALIAS_FAMILIES.get(prefix, prefix)


def canonical_route(route_id: str) -> str:
    """``route_id`` with its prefix collapsed onto the canonical pool name.

    Only the prefix segment changes; the rest of the id (model name, any
    ``:tier`` suffix) is preserved verbatim.
    """
    prefix, sep, rest = route_id.partition("/")
    canonical_prefix = ALIAS_FAMILIES.get(prefix.lower(), prefix.lower())
    if not sep:
        return canonical_prefix
    return f"{canonical_prefix}{sep}{rest}"


def base_route(route_id: str, inventory_ids: Collection[str]) -> str:
    """Strip a trailing effort suffix, only when the stripped id is known.

    Tries the more specific ``-thinking-<effort>`` strip first (e.g.
    ``cu/model-thinking-high`` -> ``cu/model``), then a plain ``-<effort>``
    strip that preserves a leading ``-thinking`` (e.g.
    ``agy/model-thinking-high`` -> ``agy/model-thinking``). Returns
    ``route_id`` unchanged when neither candidate exists in
    ``inventory_ids``, so an unknown-base effort variant is never silently
    merged into a nonexistent route.
    """
    known = inventory_ids if isinstance(inventory_ids, (set, frozenset)) else set(inventory_ids)
    for pattern in (_STRIP_THINKING_EFFORT, _STRIP_EFFORT):
        match = pattern.search(route_id)
        if match is None:
            continue
        candidate = route_id[: match.start()]
        if candidate in known:
            return candidate
    return route_id


__all__ = ["ALIAS_FAMILIES", "base_route", "canonical_route", "pool_of"]
