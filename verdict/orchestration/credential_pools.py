"""Credential-pool identity for the BOD-297 pool-aware census.

Several OmniRoute alias prefixes bill against the same upstream
credential/quota as another prefix. Probing every alias independently
wastes budget and can trip provider-side rate limiting (the 2026-10-09
agy/antigravity 504/429 incident recorded in BOD-297's accepted criteria).
This module answers "what quota does this route draw from" and "what is
the one prefix to actually probe for this family".

``verdict.orchestration.provider_catalog.backend_pool`` already recognizes
two of these pairs from empirical evidence (identical upstream ids):
agy/antigravity (-> ``google-antigravity``) and kc/kilocode/openrouter's
``:free`` suffix (-> ``openrouter-free``). This module does NOT compete
with that table: ``pool_of`` delegates to ``backend_pool`` first, and only
falls back to the extra table below for pairs ``backend_pool`` does not
yet cover (the census needs more alias pairs than eligibility's reviewer-
independence check does). Where ``backend_pool`` already has an answer,
``pool_of(x) == backend_pool(x)`` — eligibility and the census agree.

Evidence for the extra pairs (2026-10-09 controller census,
/tmp/verdict-census and tests/fixtures/catalog_truth/*.json): short and
long prefix both appear in the live inventory with matching, equal-count
model sets, and the short prefix's ``owned_by`` resolves to the long one
(af/api-airforce, bm/bluesminds, kc/kilocode generally — not only its
``:free`` rows, gh/github, cc/claude, cx/codex, oc/opencode, zm/zenmux).
``ollama-cloud``/``ollamacloud`` rows carry ``owned_by: "ollama-cloud"``.

Candidates pending evidence (kept as separate pools): ``samba``/``sambanova``
have only equal attempted-outcome counts, not shared-quota or upstream-id
proof; bare ``dv`` has no inventory evidence tying it to ``devin-cli``.

Fail-closed decisions, noted rather than silently assumed:
  - cu/cua vs cursor/cursor-api are NOT merged into one pool. ``cursor``
    (oauth, testStatus "unavailable") and ``cursor-api`` (apikey,
    "active") are distinct connections with no shared-quota evidence
    (unlike agy/antigravity's matching tool-call ids), so each pair stays
    its own pool: cu<->cursor, cua<->cursor-api.
  - dv/dva vs devin-cli/devin-desktop: ``devin-cli`` and ``devin-desktop``
    are separate connections in the same fixture. ``dva``'s ``owned_by``
    (``devin-cli-agentic``) resolves only to ``devin-cli`` via
    ``provider_catalog.OWNED_BY_ALIASES``; no evidence ties any route to
    ``devin-desktop`` or to a bare ``dv`` prefix, so ``devin-desktop`` is
    left out of this table (kept its own pool), as is bare ``dv``.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence

from verdict.orchestration.provider_catalog import backend_pool

# alias prefix (lower-cased) -> canonical/representative prefix.
#
# Used for two purposes: (1) a fallback pool identity when ``backend_pool``
# does not recognize the pair (see ``pool_of``); (2) the representative
# prefix ``canonical_route`` rewrites onto. ``agy``/``antigravity`` are
# intentionally included even though ``backend_pool`` already pools them
# under a different internal name (``google-antigravity``) — the human-
# readable "antigravity" is what ``canonical_route`` should show.
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
    "kilocode": "kilocode",  # equal model sets, owned_by resolves (all rows, not just :free)
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
    "cu": "cursor",
    "cursor": "cursor",  # distinct connection, own pool
    "cua": "cursor-api",
    "cursor-api": "cursor-api",  # distinct connection, own pool
    "dva": "devin-cli",  # owned_by=devin-cli-agentic -> devin-cli only
}

_EFFORT = r"(?:low|medium|high|xhigh|max)"
_STRIP_THINKING_EFFORT = re.compile(rf"-thinking-{_EFFORT}$")
_STRIP_EFFORT = re.compile(rf"-{_EFFORT}$")


def pool_of(route_id: str) -> str:
    """Canonical credential-pool name for ``route_id``.

    Delegates to ``provider_catalog.backend_pool`` first: when it recognizes
    an alias (its result differs from the bare prefix), that answer is
    authoritative and this function returns it unchanged, so
    ``pool_of(x) == backend_pool(x)`` for every family ``backend_pool``
    already knows (agy/antigravity, kc/kilocode/openrouter ``:free``).
    Otherwise falls back to this module's extra ``ALIAS_FAMILIES`` table.
    Unaliased prefixes are their own pool either way.
    """
    prefix = route_id.split("/", 1)[0].lower()
    delegated = backend_pool(route_id)
    if delegated != prefix:
        return delegated
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


__all__ = [
    "ALIAS_FAMILIES",
    "base_route",
    "canonical_route",
    "collapse_alias_duplicates",
    "is_non_chat_route",
    "pool_of",
]


# ---------------------------------------------------------------------------
# Non-chat route detection (BOD-297 census: skip embed/image/audio/rerank/tts)
# ---------------------------------------------------------------------------

_NON_CHAT_TYPES = frozenset({"embedding", "image", "audio", "rerank", "tts"})
_NON_CHAT_CAPABILITY_KEYS = frozenset({"embedding", "image", "audio", "rerank", "tts"})

# Id-marker fallback: the same heuristic the 2026-10-09 controller census
# scripts (/tmp/verdict-census/free_census3.py) used when inventory rows
# carry no capabilities/type (the live OmniRoute catalog usually does not).
_NON_CHAT_ID_MARKERS = re.compile(
    r"(embed|rerank|bge-|e5-|gte-|tts|whisper|transcri|speech|audio-|moderation|"
    r"dall-e|imagen|image-gen|flux|stable-diffusion|sdxl|midjourney|ocr|clip-)",
    re.IGNORECASE,
)


def is_non_chat_route(route_id: str, row: Mapping[str, object] | None = None) -> bool:
    """True when ``route_id`` is not a chat/completions model.

    Checks the inventory ``row`` first: an explicit ``type`` in a known
    non-chat set, or a ``capabilities`` map positively declaring a non-chat
    modality. Falls back to an id-marker heuristic when ``row`` is absent or
    silent on both.
    """
    if row is not None:
        if str(row.get("type", "")).lower() in _NON_CHAT_TYPES:
            return True
        caps = row.get("capabilities")
        if isinstance(caps, Mapping) and any(caps.get(key) for key in _NON_CHAT_CAPABILITY_KEYS):
            return True
    return bool(_NON_CHAT_ID_MARKERS.search(route_id))


def collapse_alias_duplicates(route_ids: Sequence[str]) -> tuple[list[str], dict[str, str]]:
    """Group route ids that share one canonical id; keep only one per group.

    Routes whose ``canonical_route`` matches are the same underlying model
    reachable through more than one alias prefix (e.g. ``agy/x`` and
    ``antigravity/x``). Only the member already named under the canonical
    prefix is kept for probing; every other member is recorded as
    *inherited* from it. A group with exactly one member is returned as-is
    (nothing to collapse). Input order is preserved for the kept ids.

    Returns ``(kept_ids, inherited)`` where ``inherited`` maps a dropped
    route id to the kept route id whose probe result it should borrow. The
    caller is responsible for surfacing that inheritance (a report field);
    this function never invents or writes a cache entry.
    """
    groups: dict[str, list[str]] = {}
    for route_id in route_ids:
        groups.setdefault(canonical_route(route_id), []).append(route_id)
    kept: list[str] = []
    inherited: dict[str, str] = {}
    decided: set[str] = set()
    for route_id in route_ids:
        canon = canonical_route(route_id)
        if canon in decided:
            continue
        decided.add(canon)
        members = groups[canon]
        if len(members) == 1:
            kept.append(members[0])
            continue
        # Prefer the member already named under the canonical prefix; fall
        # back to the first-seen member when none matches (fail-closed,
        # deterministic — never silently drops the whole group).
        probed = next((member for member in members if member == canon), members[0])
        kept.append(probed)
        for member in members:
            if member != probed:
                inherited[member] = probed
    return kept, inherited
