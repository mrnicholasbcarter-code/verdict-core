"""Prove-at-rest: the single prober that fills the health cache.

The daemon covers every admitted route in every capacity class. It spends
most of a cycle on FREE routes and only checks liveness for the other
classes. It writes ``health-cache.json`` (see
``verdict.orchestration.health_cache``) and nothing else.

It does **not** write ``~/.verdict/orchestration-health.json``. That file
belongs to the selection ladder. Story 3 is what teaches the ladder to read
this cache, so this change leaves selection order and ranking untouched.

Old state
---------
Releases before this one persisted a cycle document at
``~/.verdict/prove-at-rest/state.json`` (override ``VERDICT_PROVE_AT_REST_STATE``).
That document is a different schema (one cycle of free-intersect-active proof
results plus passports). This daemon **ignores** it. It neither reads nor
deletes it. ``load_healthy_passports`` still reads it, unchanged, for callers
that have not moved.

Cycle order
-----------
1. half-open negatives (a negative whose ``until`` has elapsed);
2. stale healthy entries;
3. never-probed FREE routes, round-robin by provider/pool;
4. SUBSCRIPTION, METERED and UNKNOWN, liveness (chat) only;
5. a small epsilon slice of cold providers (providers with no fresh entry).

Within each pass, prefer pools with recent success or qualified session evidence.
Active credential failures cool only their canonical pool through cache negatives.

Each cycle stops at ``max_requests`` (default 300) or ``max_wall_seconds``
(default 10 min), with concurrency 4. Every completed probe is persisted, so
a crash keeps partial progress. The cursor records which ordering pass the
cycle was in.

Two-step probe
--------------
Chat first: ``Reply with exactly: OK``. Then one required tool call. A route
is a coding worker only when the tool call succeeds (``tool_ok``). Other
capacity classes stop after the chat step.

Token buckets
-------------
One bucket per provider/pool, shared with real calls through
``HealthCache.consume``. A 429 zeroes that bucket until ``Retry-After``.
"""

from __future__ import annotations

import http.client
import json
import re
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable, Collection, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from verdict.http_safety import open_no_redirect
from verdict.openrouter_free_set import (
    OpenRouterFreeSet,
    key_limit_on_non_free_is_route_scoped,
    load_openrouter_free_set,
)
from verdict.orchestration.contracts import CapacityClass
from verdict.orchestration.credential_pools import POOL_COOLDOWN_CATEGORIES, pool_of
from verdict.orchestration.eligibility import capacity_class_of
from verdict.orchestration.health_cache import (
    AGENTIC_PROBE_TTL_SECONDS,
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
    STATE_FRESH,
    STATE_NEGATIVE,
    STATE_STALE,
    STATE_UNPROBED,
    HealthCache,
    HealthCacheError,
    HealthCacheLockTimeoutError,
    HealthEntry,
    ProbeResult,
    agentic_capability,
    default_cache_path,
)
from verdict.orchestration.provider_catalog import is_catalog_stale_error

# The old cycle document stays readable. This module no longer writes it.
# ``ProveAtRestDaemon`` is the legacy free-intersect-active prover; the
# single prober above is ``Prober``. Both names are importable so existing
# callers keep working while the daemon command uses ``Prober``.
from verdict.prove_at_rest_legacy import (
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_PROBE_TIMEOUT_SECONDS,
    ENV_STATE_PATH,
    PROVE_AT_REST_SCHEMA_VERSION,
    STATUS_FAILED,
    STATUS_HEALTHY,
    STATUS_SKIPPED,
    ProofResult,
    ProveAtRestCycle,
    ProveAtRestDaemon,
    ProveAtRestError,
    ProveAtRestStore,
    default_state_path,
    load_healthy_passports,
    passport_from_probe,
)
from verdict.subagent_selection import CONTEXT_LENGTH_CATEGORY

CHAT_PROBE_MESSAGE = "Reply with exactly: OK"
TOOL_NAME = "verdict_probe_ping"

# --- Agentic probe: 3-turn tool task (read, edit, confirm) -----------------
AGENTIC_TOOL_READ = "verdict_probe_read_file"
AGENTIC_TOOL_EDIT = "verdict_probe_edit_file"
# This is a model-visible identifier for AgenticFileState, not a filesystem
# path: read/edit only operate on an in-memory string (never open this path).
AGENTIC_PROBE_FILE = "/tmp/verdict_agentic_probe.txt"  # nosec B108 — in-memory identifier only
AGENTIC_PROBE_ORIGINAL = "line one\nline two\nline three\n"
AGENTIC_PROBE_EXPECTED = "line one\nLINE TWO\nline three\n"
DEFAULT_AGENTIC_INTERVAL_HOURS = AGENTIC_PROBE_TTL_SECONDS / 3600.0
AGENTIC_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": AGENTIC_TOOL_READ,
            "description": "Read a file and return its contents.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": AGENTIC_TOOL_EDIT,
            "description": "Replace old_text with new_text in a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
]


class AgenticFakeFile:
    """In-memory fake file for agentic probe tool simulation.

    Starts with ``AGENTIC_PROBE_ORIGINAL`` content. ``read`` returns
    the current content. ``edit`` applies ``old_text`` → ``new_text``
    only when the path matches ``AGENTIC_PROBE_FILE`` and ``old_text``
    is found; returns ``True`` on success, ``False`` on mismatch.
    """

    def __init__(self) -> None:
        self.content: str = AGENTIC_PROBE_ORIGINAL

    def read(self, path: str) -> str | None:
        """Read the fake file content, or *None* if the path is wrong."""
        if path != AGENTIC_PROBE_FILE:
            return None
        return self.content

    def edit(self, path: str, old_text: str, new_text: str) -> bool:
        """Apply an edit. Returns *True* iff the path and old_text match."""
        if path != AGENTIC_PROBE_FILE:
            return False
        if old_text not in self.content:
            return False
        self.content = self.content.replace(old_text, new_text, 1)
        return True


def _extract_tool_calls(response_body: Any) -> list[dict[str, Any]]:
    """Extract tool calls from an OpenAI-style response body.

    Returns a list of ``{"id": ..., "name": ..., "arguments": {...}}`` dicts.
    """
    if not isinstance(response_body, Mapping):
        return []
    choices = response_body.get("choices", [])
    if not choices:
        return []
    msg = choices[0]
    if isinstance(msg, Mapping):
        msg = msg.get("message", msg)
    if not isinstance(msg, Mapping):
        return []
    calls = msg.get("tool_calls", [])
    result: list[dict[str, Any]] = []
    for tc in calls if isinstance(calls, list) else []:
        if not isinstance(tc, Mapping):
            continue
        fn = tc.get("function")
        if not isinstance(fn, Mapping):
            continue
        name = fn.get("name")
        if not name:
            continue
        raw = fn.get("arguments", "{}")
        if isinstance(raw, str):
            try:
                args = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                args = {}
        elif isinstance(raw, dict):
            args = raw
        else:
            args = {}
        result.append({"id": tc.get("id", ""), "name": name, "arguments": args})
    return result


def _simulate_tool_calls(
    fake_file: AgenticFakeFile, tool_calls: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Execute tool calls against the fake file and return tool-result messages."""
    results: list[dict[str, Any]] = []
    for tc in tool_calls:
        name = tc["name"]
        args = tc["arguments"]
        if name == AGENTIC_TOOL_READ:
            content = fake_file.read(args.get("path", ""))
            if content is None:
                output = json.dumps({"error": f"file not found: {args.get('path', '')}"})
            else:
                output = content
        elif name == AGENTIC_TOOL_EDIT:
            ok = fake_file.edit(
                args.get("path", ""), args.get("old_text", ""), args.get("new_text", "")
            )
            output = json.dumps({"ok": ok})
        else:
            output = json.dumps({"error": f"unknown tool: {name}"})
        results.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": output})
    return results


def _extract_assistant_message(response_body: Any) -> dict[str, Any]:
    """Extract the assistant message from an OpenAI response for conversation continuity."""
    if not isinstance(response_body, Mapping):
        return {"role": "assistant", "content": ""}
    choices = response_body.get("choices", [])
    if not choices or not isinstance(choices[0], Mapping):
        return {"role": "assistant", "content": ""}
    msg = choices[0].get("message", {})
    if not isinstance(msg, Mapping):
        return {"role": "assistant", "content": ""}
    # Return a minimal assistant message preserving tool_calls if present.
    result: dict[str, Any] = {"role": "assistant"}
    if msg.get("content") is not None:
        result["content"] = msg["content"]
    else:
        result["content"] = None
    if msg.get("tool_calls"):
        result["tool_calls"] = msg["tool_calls"]
    return result


DEFAULT_MAX_REQUESTS = 300
DEFAULT_MAX_WALL_SECONDS = 600.0
DEFAULT_CONCURRENCY = 4
DEFAULT_EPSILON = 2
DEFAULT_AGENTIC_MAX_ROUTES = 8
DEFAULT_AGENTIC_CONCURRENCY = 4
# Free providers rate-limit per account: one agentic conversation per pool at a time.
DEFAULT_AGENTIC_POOL_CONCURRENCY = 1
AGENTIC_TURNS = 3
ENV_CACHE_PATH = "VERDICT_HEALTH_CACHE"

# Old on-disk document. Ignored, never migrated, never deleted.
LEGACY_STATE_RELATIVE = Path(".verdict") / "prove-at-rest" / "state.json"


class ProveError(HealthCacheError):
    """Raised when a prove-at-rest cycle cannot start."""


@dataclass(frozen=True)
class AdmittedRoute:
    """One admitted route the prober may visit. Built by the caller.

    ``capacity`` is the evidence-backed class (``free``, ``subscription``,
    ``metered``, ``unknown``). ``pool`` is an optional shared-quota pool.
    ``non_chat`` (BOD-297) marks a route ``order_cycle`` must never probe
    (embedding/image/audio/rerank/tts): default ``False`` so every existing
    caller that builds an ``AdmittedRoute`` without naming it is unaffected.
    """

    route_id: str
    provider: str
    capacity: str
    pool: str | None = None
    capacity_evidence: str | None = None
    non_chat: bool = False
    # Ordering hint from the gateway's synced live catalog: False means the
    # gateway is known not to serve the id; None means no catalog evidence.
    live_catalog: bool | None = None

    def __post_init__(self) -> None:
        if not self.route_id.strip() or not self.provider.strip():
            raise ProveError("route_id and provider must be non-empty")
        if self.capacity not in {item.value for item in CapacityClass}:
            raise ProveError(f"unknown capacity class: {self.capacity}")


@dataclass(frozen=True)
class ProbeExchange:
    """What the fake or live transport returns for one HTTP call."""

    http_status: int | None
    ok: bool
    chat_exact: bool = False
    tool_called: bool = False
    latency_ms: float | None = None
    retry_after_seconds: float | None = None
    error_category: str | None = None
    response_body: Mapping[str, Any] | None = None  # parsed JSON for agentic scoring
    status_code: int | None = None  # alias for http_status (used by score_agentic_probe)
    reported_model: str = ""  # model returned by the backend (for identity check)
    detail: str = ""  # sanitized provider message (for model-vs-provider scope)


# A transport probes one route for one phase ("chat" or "tool") and must not
# touch the network unless the caller built a live transport.
ProbeTransportFn = Callable[[str, str, float], ProbeExchange]

# A screen sends one provider's model ids to ``POST /api/models/test-all`` and
# returns the per-model results, or None when the screen itself failed.
ScreenTransportFn = Callable[[str, Sequence[str], float], Mapping[str, Mapping[str, Any]] | None]


@dataclass
class CycleStats:
    """Counters for one cycle. ``requests`` counts chat and tool calls."""

    requests: int = 0
    probed: int = 0
    fresh: int = 0
    negative: int = 0
    skipped_bucket: int = 0
    stopped_reason: str = ""
    # True when the gateway-auth-outage breaker stopped the cycle
    # (see ``Prober._buffer_auth_failure``). ``stopped_reason`` is also set to
    # ``"auth_outage"`` in that case; this flag lets a caller branch on it by
    # name instead of matching the reason string.
    auth_outage: bool = False
    # The agentic phase has its own budget, so it reports its own counters.
    agentic_probed: int = 0
    agentic_passed: int = 0
    agentic_stopped_reason: str = ""
    # BOD-410: routes sent to the cheap gateway screen (POST /api/models/test-all)
    # before the agentic probe, and how many answered.
    screened: int = 0
    screen_ok: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "probed": self.probed,
            "fresh": self.fresh,
            "negative": self.negative,
            "skipped_bucket": self.skipped_bucket,
            "stopped_reason": self.stopped_reason,
            "auth_outage": self.auth_outage,
            "agentic_probed": self.agentic_probed,
            "agentic_passed": self.agentic_passed,
            "agentic_stopped_reason": self.agentic_stopped_reason,
            "screened": self.screened,
            "screen_ok": self.screen_ok,
        }


def category_for(exchange: ProbeExchange) -> str:
    """Map a transport result onto a cache category."""
    status = exchange.http_status
    if exchange.ok:
        return CATEGORY_OK
    named = exchange.error_category or ""
    if status == 429 or named in {"rate_limited", "rate_limit"}:
        return CATEGORY_RATE_LIMITED
    if status == 401 or named in {"auth", "unauthorized", "authentication"}:
        return CATEGORY_AUTH
    if status == 402 or named in {"payment_required", "quota_exhausted"}:
        return CATEGORY_PAYMENT
    if status == 403 or named in {"forbidden", "permission"}:
        return CATEGORY_PERMISSION
    if status == 404 or named == "not_found":
        return CATEGORY_NOT_FOUND
    if status == 410 or named in {"gone", "http_410"}:
        return CATEGORY_GONE
    if named == "catalog_stale":
        return CATEGORY_CATALOG_STALE
    if status is None or named == "timeout":
        return CATEGORY_TIMEOUT
    if status >= 500 or named in {"upstream", "upstream_error"}:
        return CATEGORY_UPSTREAM
    return named or "http_error"


def chat_payload(route_id: str) -> dict[str, Any]:
    """First step: a chat completion that must answer OK. No tools."""
    return {
        "model": route_id,
        "messages": [{"role": "user", "content": CHAT_PROBE_MESSAGE}],
        "max_tokens": 8,
        "temperature": 0,
        "stream": False,
    }


def tool_payload(route_id: str) -> dict[str, Any]:
    """Second step: the model must call the single required tool."""
    return {
        "model": route_id,
        "messages": [
            {"role": "user", "content": f"Call the tool {TOOL_NAME} with argument value ok."}
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": TOOL_NAME,
                    "parameters": {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": ["value"],
                    },
                },
            }
        ],
        "tool_choice": {"type": "function", "function": {"name": TOOL_NAME}},
        "max_tokens": 64,
        "temperature": 0,
        "stream": False,
    }


def agentic_turn1_payload(
    route_id: str, conversation: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Turn 1: ask the model to read the probe file."""
    return {
        "model": route_id,
        "messages": [
            {
                "role": "user",
                "content": (
                    f"Read the file at {AGENTIC_PROBE_FILE} using the "
                    f"{AGENTIC_TOOL_READ} tool and tell me its contents."
                ),
            }
        ],
        "tools": AGENTIC_TOOLS,
        "max_tokens": 256,
        "temperature": 0,
        "stream": False,
    }


def agentic_turn2_payload(
    route_id: str, conversation: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Turn 2: ask the model to edit a line (given previous conversation)."""
    prior = list(conversation) if conversation else []
    messages = [
        *prior,
        {
            "role": "user",
            "content": (
                f"Now use {AGENTIC_TOOL_EDIT} to change 'line two' to 'LINE TWO' "
                f"in {AGENTIC_PROBE_FILE}."
            ),
        },
    ]
    return {
        "model": route_id,
        "messages": messages,
        "tools": AGENTIC_TOOLS,
        "max_tokens": 256,
        "temperature": 0,
        "stream": False,
    }


def agentic_turn3_payload(
    route_id: str, conversation: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Turn 3: ask the model to confirm the edit by reading again."""
    prior = list(conversation) if conversation else []
    messages = [
        *prior,
        {
            "role": "user",
            "content": (
                f"Read {AGENTIC_PROBE_FILE} again with {AGENTIC_TOOL_READ} to "
                f"confirm the edit was applied."
            ),
        },
    ]
    return {
        "model": route_id,
        "messages": messages,
        "tools": AGENTIC_TOOLS,
        "max_tokens": 256,
        "temperature": 0,
        "stream": False,
    }


def score_agentic_probe(exchanges: Sequence[ProbeExchange]) -> bool:
    """Score a 3-turn agentic probe sequence as pass/fail.

    Pass requires:
    1. Turn 1 calls read_file with ``path`` == :data:`AGENTIC_PROBE_FILE`.
    2. Turn 2 calls edit_file with ``old_text`` == ``'line two'`` and
       ``new_text`` == ``'LINE TWO'``.
    3. Turn 3 calls read_file with the same path.
    All three turns must have HTTP 2xx. The caller must also check that the
    fake file ended in :data:`AGENTIC_PROBE_EXPECTED`.
    """
    if len(exchanges) < 3:
        return False
    for ex in exchanges:
        if not ex.ok:
            return False
        if ex.http_status is not None and ex.http_status >= 400:
            return False

    def _tool_call_args(ex: ProbeExchange, tool_name: str) -> dict[str, Any] | None:
        """Return parsed arguments of the first matching tool call, or *None*."""
        body = ex.response_body
        if not isinstance(body, Mapping):
            return None
        choices = body.get("choices", [])
        if not choices:
            return None
        message = choices[0].get("message", {}) if isinstance(choices[0], Mapping) else {}
        calls = message.get("tool_calls", [])
        for tc in calls if isinstance(calls, list) else []:
            if not isinstance(tc, Mapping):
                continue
            fn = tc.get("function")
            if not isinstance(fn, Mapping):
                continue
            if fn.get("name") != tool_name:
                continue
            raw = fn.get("arguments", "{}")
            if isinstance(raw, str):
                try:
                    parsed = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    return None
                return parsed if isinstance(parsed, dict) else None
            if isinstance(raw, dict):
                return raw
            return None
        return None

    # Turn 1: read_file with the exact probe path.
    t1_args = _tool_call_args(exchanges[0], AGENTIC_TOOL_READ)
    if t1_args is None or t1_args.get("path") != AGENTIC_PROBE_FILE:
        return False

    # Turn 2: edit_file with correct path, old_text, and new_text.
    t2_args = _tool_call_args(exchanges[1], AGENTIC_TOOL_EDIT)
    if t2_args is None:
        return False
    if t2_args.get("path") != AGENTIC_PROBE_FILE:
        return False
    if t2_args.get("old_text") != "line two" or t2_args.get("new_text") != "LINE TWO":
        return False

    # Turn 3: read_file with the same path. The edited content is produced by
    # the simulated read that runs AFTER this response, so it can't be in the
    # response body. The caller checks the fake file's final state
    # (``AgenticFakeFile.content``) instead.
    t3_args = _tool_call_args(exchanges[2], AGENTIC_TOOL_READ)
    return t3_args is not None and t3_args.get("path") == AGENTIC_PROBE_FILE


def _provider_of(route_id: str) -> str:
    if "/" in route_id:
        return route_id.split("/", 1)[0]
    return route_id or "unknown"


def model_identity_matches(route_id: str, reported: str) -> bool:
    """True when *reported* is the expected model, tolerating a stripped prefix.

    Rules (defect 4 fix):
    - Empty reported: not a mismatch (the gateway chose not to echo an id).
    - Otherwise the reported id must equal the full route id, or the route id
      minus its gateway provider segment. OmniRoute was observed (2026-09-29)
      echoing ``cc/claude-x`` as ``claude-x`` and
      ``nvidia/moonshotai/kimi-k3`` as ``moonshotai/kimi-k3``. Anything else,
      including a different provider with the same suffix, is a mismatch.
    """
    if not reported:
        return True
    route_suffix = route_id.split("/", 1)[1] if "/" in route_id else route_id
    return reported in (route_id, route_suffix)


def _pool_of(route: AdmittedRoute) -> str:
    return route.pool or pool_of(route.route_id)


def _cooled_pools(cache: HealthCache, now: datetime) -> set[str]:
    """Derive pool cooldowns from existing active negatives, never a second store."""
    return {
        entry.pool or pool_of(entry.route_id)
        for entry in cache.routes().values()
        if entry.state_at(now) == STATE_NEGATIVE
        and entry.category in POOL_COOLDOWN_CATEGORIES
        and entry.failure_scope != "route"
    }


def order_cycle(
    routes: Sequence[AdmittedRoute],
    cache: HealthCache,
    now: datetime,
    *,
    epsilon: int = DEFAULT_EPSILON,
) -> list[tuple[AdmittedRoute, str]]:
    """Order one cycle. Each item is ``(route, kind)``.

    ``kind`` is ``full`` (chat then tool) or ``liveness`` (chat only).

    BOD-297 pool-aware planning, applied before the existing ordering:

    1. A ``non_chat`` route (embedding/image/audio/rerank/tts) is dropped
       entirely; it is never probed here (the census report records it
       with reason ``"non_chat"``).
    2. Alias-family duplicates (``agy``/``antigravity``, ``af``/
       ``api-airforce``, ...) collapse onto one canonical member per
       cycle; the others are not ordered at all. This function never
       writes a synthetic cache entry for the dropped members -- a report
       reads ``credential_pools.collapse_alias_duplicates`` separately to
       record which routes inherited whose evidence.
    3. A never-probed effort variant (``-low``/``-medium``/.../``-high``,
       or ``-thinking-<effort>``) is skipped this cycle unless its base
       route is already healthy in the cache, so a cycle never spends
       budget proving a stronger variant before its cheaper base passed.
       A variant with an already-probed history (half-open or stale) is
       exempt: once known, refreshing it does not wait on the base again.
    """
    from verdict.orchestration.credential_pools import base_route, collapse_alias_duplicates

    chat_routes = [route for route in routes if not route.non_chat]
    kept_ids, _inherited = collapse_alias_duplicates([route.route_id for route in chat_routes])
    kept_id_set = set(kept_ids)
    cooled = _cooled_pools(cache, now)
    candidates = [
        route
        for route in chat_routes
        if route.route_id in kept_id_set and _pool_of(route) not in cooled
    ]
    candidate_ids = {route.route_id for route in candidates}
    # Evidence is advisory: prefer warm pools within each existing ordering pass.
    warm = {
        entry.pool or pool_of(entry.route_id)
        for entry in cache.routes().values()
        if (
            entry.last_success_at is not None
            and timedelta(0) <= now - entry.last_success_at <= timedelta(hours=24)
        )
        or agentic_capability(entry, now)["qualified"]
    }
    # Gateway-served ids first, then warm pools. Both only reorder admitted routes.
    candidates.sort(key=lambda route: (_live_rank(route), _pool_of(route) not in warm))

    by_id = {route.route_id: route for route in candidates}
    half_open: list[AdmittedRoute] = []
    stale: list[AdmittedRoute] = []
    seen: set[str] = set()
    for route in candidates:
        lookup = cache.lookup(route.route_id, now)
        entry = lookup.entry
        if entry is None:
            continue
        if lookup.state == STATE_NEGATIVE:
            continue
        if not entry.healthy and lookup.state == STATE_UNPROBED:
            # Negative whose ``until`` has elapsed: half-open.
            half_open.append(route)
            seen.add(route.route_id)
        elif entry.healthy and lookup.state == STATE_UNPROBED:
            # Usable window elapsed: probe again with the never-probed group.
            continue
        elif lookup.state == STATE_STALE:
            stale.append(route)
            seen.add(route.route_id)

    def _base_is_ready(route: AdmittedRoute) -> bool:
        base_id = base_route(route.route_id, candidate_ids)
        return base_id == route.route_id or cache.lookup(base_id, now).healthy

    free_new = [
        route
        for route in candidates
        if route.capacity == CapacityClass.FREE.value
        and route.route_id not in seen
        and cache.lookup(route.route_id, now).state == STATE_UNPROBED
        and _base_is_ready(route)
    ]
    other_new = [
        route
        for route in candidates
        if route.capacity != CapacityClass.FREE.value
        and route.route_id not in seen
        and cache.lookup(route.route_id, now).state == STATE_UNPROBED
        and _base_is_ready(route)
    ]

    ordered: list[tuple[AdmittedRoute, str]] = []

    def _kind_for(route: AdmittedRoute) -> str:
        """Derive probe kind from capacity class (defect 3 fix for half-open/stale)."""
        if route.capacity in {
            CapacityClass.SUBSCRIPTION.value,
            CapacityClass.METERED.value,
            CapacityClass.UNKNOWN.value,
        }:
            return "liveness"
        return "full"

    ordered.extend((route, _kind_for(route)) for route in half_open)
    ordered.extend((route, _kind_for(route)) for route in stale)
    for live in (True, None, False):
        tier = [route for route in free_new if route.live_catalog is live]
        ordered.extend((route, "full") for route in _round_robin(tier))
    ordered.extend((route, "liveness") for route in other_new)
    ordered.extend(
        _epsilon_slice(
            candidates,
            cache,
            now,
            seen=set(item[0].route_id for item in ordered),
            epsilon=epsilon,
            by_id=by_id,
        )
    )
    return ordered


def _live_rank(route: AdmittedRoute) -> int:
    """0 when the gateway serves the id, 1 when unknown, 2 when it does not."""
    return {True: 0, None: 1, False: 2}[route.live_catalog]


def agentic_candidates(
    routes: Sequence[AdmittedRoute],
    cache: HealthCache,
    now: datetime,
    *,
    include_cooled: bool = False,
) -> list[AdmittedRoute]:
    """FREE routes worth an agentic probe, most promising first.

    Applies the same filters as ``order_cycle`` (non-chat, alias duplicates,
    cooled pools, effort variants whose base has not passed) and skips routes
    with an active negative. Order: chat+tool passed, chat passed, then
    unprobed routes by live-catalog evidence. Ordering never adds a route.
    ``include_cooled`` keeps cooled pools so a screen can half-open them.
    """
    from verdict.orchestration.credential_pools import base_route, collapse_alias_duplicates

    chat_routes = [
        route
        for route in routes
        if not route.non_chat and route.capacity == CapacityClass.FREE.value
    ]
    kept, _inherited = collapse_alias_duplicates([route.route_id for route in chat_routes])
    kept_ids = set(kept)
    cooled = set() if include_cooled else _cooled_pools(cache, now)
    picked: list[tuple[int, AdmittedRoute]] = []
    for route in chat_routes:
        if route.route_id not in kept_ids or _pool_of(route) in cooled:
            continue
        lookup = cache.lookup(route.route_id, now)
        if lookup.state == STATE_NEGATIVE:
            continue
        base_id = base_route(route.route_id, kept_ids)
        if base_id != route.route_id and not cache.lookup(base_id, now).healthy:
            continue
        entry = lookup.entry
        if lookup.healthy and entry is not None and entry.tool_ok:
            rank = 0
        elif lookup.healthy:
            rank = 1
        else:
            rank = 2 + _live_rank(route)
        picked.append((rank, route))
    picked.sort(key=lambda item: item[0])
    return [route for _rank, route in picked]


def _round_robin(routes: Sequence[AdmittedRoute]) -> list[AdmittedRoute]:
    """Spread never-probed FREE routes across canonical credential buckets."""
    groups: dict[str, list[AdmittedRoute]] = {}
    for route in routes:
        key = _pool_of(route)
        groups.setdefault(key, []).append(route)
    queues = list(groups.values())
    if not queues:
        return []
    depth = max(len(queue) for queue in queues)
    out: list[AdmittedRoute] = []
    for index in range(depth):
        for queue in queues:
            if index < len(queue):
                out.append(queue[index])
    return out


def _epsilon_slice(
    routes: Sequence[AdmittedRoute],
    cache: HealthCache,
    now: datetime,
    *,
    seen: set[str],
    epsilon: int,
    by_id: Mapping[str, AdmittedRoute],
) -> list[tuple[AdmittedRoute, str]]:
    """One liveness probe for each provider that has no fresh entry.

    Capped at ``epsilon`` providers. A provider is cold when none of its
    cached entries is fresh. Routes already ordered in this cycle are skipped,
    so the slice never repeats work the earlier passes already cover.
    """
    from verdict.orchestration.credential_pools import base_route, collapse_alias_duplicates

    chat_routes = [route for route in routes if not route.non_chat]
    kept_ids, _inherited = collapse_alias_duplicates([route.route_id for route in chat_routes])
    candidate_ids = set(kept_ids)
    fresh_providers: set[str] = set()
    for entry in cache.routes().values():
        if entry.state_at(now) == STATE_FRESH and entry.healthy:
            fresh_providers.add(_provider_of(entry.route_id))
    out: list[tuple[AdmittedRoute, str]] = []
    taken: set[str] = set()
    for route in routes:
        if len(out) >= max(0, epsilon):
            break
        if route.provider in fresh_providers or route.provider in taken:
            continue
        if route.route_id in seen or route.route_id not in candidate_ids:
            continue
        base_id = base_route(route.route_id, candidate_ids)
        if base_id != route.route_id and not cache.lookup(base_id, now).healthy:
            continue
        taken.add(route.provider)
        out.append((by_id[route.route_id], "liveness"))
    return out


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Prober:
    """One cycle, or a loop of cycles, over an injected admitted-route source."""

    cache: HealthCache
    routes_loader: Callable[[], Sequence[AdmittedRoute]]
    transport: ProbeTransportFn
    max_requests: int = DEFAULT_MAX_REQUESTS
    max_wall_seconds: float = DEFAULT_MAX_WALL_SECONDS
    concurrency: int = DEFAULT_CONCURRENCY
    probe_timeout_seconds: float = 15.0
    epsilon: int = DEFAULT_EPSILON
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS
    clock: Callable[[], datetime] = field(default=_now)
    monotonic: Callable[[], float] = field(default=time.monotonic)
    sleep: Callable[[float], None] = field(default=time.sleep)
    on_cycle_error: Callable[[Exception], None] | None = None
    agentic_interval_hours: float = DEFAULT_AGENTIC_INTERVAL_HOURS
    agentic_transport: Callable[[str, dict[str, Any], float], ProbeExchange] | None = None
    # The agentic phase has its own budget: it runs even when the chat phase
    # spent its requests or wall time. ``None`` wall means ``max_wall_seconds``.
    agentic_max_routes: int = DEFAULT_AGENTIC_MAX_ROUTES
    agentic_concurrency: int = DEFAULT_AGENTIC_CONCURRENCY
    agentic_pool_concurrency: int = DEFAULT_AGENTIC_POOL_CONCURRENCY
    agentic_wall_seconds: float | None = None
    # BOD-338: confirm the gateway key independently of provider credentials.
    # Offline/injected transports without a checker use a majority-pool fallback.
    gateway_auth_check: Callable[[float], ProbeExchange] | None = None
    # BOD-410: cheap gateway chat screen (``POST /api/models/test-all``) that
    # picks agentic survivors. ``None`` keeps the unscreened candidate order.
    screen_transport: ScreenTransportFn | None = None
    # Half-open agentic allowlist: screen survivors from pools already cooled
    # before this cycle's screen, with no fresh account-level failure for that
    # pool in the current screen/cycle. One tool conversation each.
    _half_open_routes: set[str] = field(default_factory=set, init=False, repr=False)
    # Pools that received a fresh account-level (pool-cooling) negative during
    # this screen/cycle. Half-open is never granted or retained for these.
    _screen_failed_pools: set[str] = field(default_factory=set, init=False, repr=False)
    _gateway_auth_result: ProbeExchange | None = field(default=None, init=False, repr=False)
    _cycle_pools: set[str] = field(default_factory=set, init=False, repr=False)
    _cycle_started: float = field(default=0.0, init=False, repr=False)
    # Request cap of the running phase, for the gateway-auth confirmation.
    _request_cap: int = field(default=0, init=False, repr=False)
    auth_outage_consecutive: int = 5
    auth_outage_distinct_pools: int = 3
    _stop: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _auth_fail_pools: list[str] = field(default_factory=list, init=False, repr=False)
    _auth_fail_buffer: list[tuple[str, ProbeResult, datetime]] = field(
        default_factory=list, init=False, repr=False
    )

    def __post_init__(self) -> None:
        if self.max_requests < 1:
            raise ProveError("max_requests must be >= 1")
        if self.max_wall_seconds <= 0:
            raise ProveError("max_wall_seconds must be positive")
        if self.concurrency < 1:
            raise ProveError("concurrency must be >= 1")
        if self.probe_timeout_seconds <= 0:
            raise ProveError("probe_timeout_seconds must be positive")
        if self.agentic_max_routes < 0:
            raise ProveError("agentic_max_routes must be >= 0")
        if self.agentic_concurrency < 1 or self.agentic_pool_concurrency < 1:
            raise ProveError("agentic concurrency limits must be >= 1")
        if self.agentic_wall_seconds is not None and self.agentic_wall_seconds <= 0:
            raise ProveError("agentic_wall_seconds must be positive")

    def stop(self) -> None:
        self._stop.set()

    def _save(self) -> None:
        """Persist under the cache lock; the wait is bounded by ``LOCK_WAIT_SECONDS``.

        Not by the cycle deadline: a probe that finished at the wall cap must
        still be written, and an already-passed deadline made every such save
        fail at once with ``lock_timeout``.
        """
        self.cache.save(monotonic=self.monotonic, sleep=self.sleep)

    def run_once(self) -> CycleStats:
        """Chat phase, then the agentic phase, each within its own bounds.

        A chat phase that stops early (request, wall or lock bound) does not
        skip the agentic phase. Only a gateway auth outage skips it.
        """
        started = self.monotonic()
        stats = CycleStats()
        self._auth_fail_pools = []  # fresh auth-outage window per cycle
        self._auth_fail_buffer = []
        self._gateway_auth_result = None
        self._cycle_started = started
        self._request_cap = self.max_requests
        routes = list(self.routes_loader())
        try:
            self._run_chat_phase(routes, started, stats)
        except HealthCacheLockTimeoutError:
            stats.stopped_reason = stats.stopped_reason or "lock_timeout"
        if stats.auth_outage:
            return stats
        try:
            # Keep the outage window across both phases; a broken gateway key
            # must not poison routes through agentic probes after chat probing stops.
            self.run_agentic_probes(stats, routes=routes)
            if self.gateway_auth_check is None or self._gateway_auth_result is not None:
                self._flush_auth_fail_buffer(stats)
            self._save()
        except HealthCacheLockTimeoutError:
            stats.agentic_stopped_reason = stats.agentic_stopped_reason or "lock_timeout"
        return stats

    def _run_chat_phase(
        self, routes: Sequence[AdmittedRoute], started: float, stats: CycleStats
    ) -> None:
        """BOD-297/S05-B: a bounded RefreshCoordinator client, not its own loop.

        Scoped to FREE+SUBSCRIPTION only (automatic trigger: ``authorized``
        is never passed, so METERED/UNKNOWN never enter -- that liveness
        probing is out of scope for this slice; it stays available only
        through the consented manual plan).

        Parent decision (BOD-297/S05-B, 2026-10-19, BOD-297/BOD-342): prove-at-
        rest does NOT run at census's maintenance size (4 routes/16 requests
        -- too small: it would cut the daemon's fresh set from ~44 routes to
        ~8 per 300 s tick). It goes through the SAME coordinator (flock,
        marker, buckets, auth_gate) but with its OWN bounded config:
        ``maintenance_from_env()`` values, except ``max_routes = min(daemon
        max_requests // 2, HARD_MAX_ROUTES)``, ``max_requests = min(daemon
        max_requests, HARD_MAX_REQUESTS)``, ``wall_seconds = min(daemon
        max_wall_seconds, HARD_MAX_WALL_SECONDS_MAINTENANCE)`` -- never above
        the coordinator hard maxima. Behaviour change: 100 requests/cycle
        cap (``HARD_MAX_REQUESTS``), not the daemon's own 300; no partial
        probes (a probe that cannot fully fit the remaining budget is never
        started, unlike the old loop's per-call spend).

        The existing durable cursor (``cache.cursor``) is kept so a fleet
        larger than one cycle's route cap still rotates across cycles,
        exactly as the old loop did: already-probed ids from an open cycle
        are excluded from this cycle's candidates, newly-probed ids are
        merged in, and the cursor clears only once the whole fleet is
        covered.
        """
        from verdict.orchestration.verified_refresh import (
            CAPACITY_FREE,
            CAPACITY_SUBSCRIPTION,
            CONSUMER_PROVE_AT_REST,
            HARD_MAX_REQUESTS,
            HARD_MAX_ROUTES,
            HARD_MAX_WALL_SECONDS_MAINTENANCE,
            REASON_BUCKET,
            REASON_PROVIDER_STOP,
            STATUS_UNVERIFIED,
            RefreshCoordinator,
            RefreshSnapshot,
            RowInput,
            maintenance_from_env,
        )

        now = self.clock()
        ordered = order_cycle(routes, self.cache, now, epsilon=self.epsilon)
        self._cycle_pools = {_pool_of(route) for route, _kind in ordered}
        cursor = self.cache.cursor
        probed_ids: set[str] = set()
        if cursor.get("cycle_open"):
            raw_ids = cursor.get("probed_ids") or []
            if isinstance(raw_ids, list):
                probed_ids = set(str(rid) for rid in raw_ids if isinstance(rid, str))
        prepaid = [
            route
            for route, _kind in ordered
            if route.capacity in {CapacityClass.FREE.value, CapacityClass.SUBSCRIPTION.value}
            and route.route_id not in probed_ids
        ]
        rows = tuple(
            RowInput(
                route_id=route.route_id,
                provider=route.provider,
                status=STATUS_UNVERIFIED,
                capacity_class=(
                    CAPACITY_FREE
                    if route.capacity == CapacityClass.FREE.value
                    else CAPACITY_SUBSCRIPTION
                ),
                refreshable=True,
                pool=route.pool,
                capacity_evidence=route.capacity_evidence,
                rank_hint=index,
            )
            for index, route in enumerate(prepaid)
        )

        def auth_gate(
            route_id: str, http_status: int | None, remaining_requests: int
        ) -> tuple[bool | None, int]:
            """BOD-338 breaker: confirm the gateway key once per cycle.

            Both production callers (``cli.py`` daemon, ``extra.py`` once)
            build the live daemon without an injected transport, so
            ``gateway_auth_check`` is always set in production
            (``build_live_daemon`` wires ``live_gateway_auth_check``). A
            memoized single call replaces the old loop's multi-route
            buffer: the coordinator already withholds the write for the
            route that triggered this call, so there is nothing buffered to
            flush on a sibling success or a later non-auth failure.

            Without ``gateway_auth_check`` (test-only injected transports),
            there is no outage confirmation: the per-route auth failure is
            written and that provider stops, same as any other
            provider-scope rejection (BOD-297/S05-B, parent decision
            2026-10-19 item 16 -- the old majority-pool-of-failures
            heuristic was deleted; the coordinator's own provider stop
            already made it unreachable in its own fixtures).

            ``remaining_requests`` is the COORDINATOR's own remaining
            budget for this job (review finding 19, 2026-10-19: checking
            ``stats.requests`` here was wrong -- it stays 0 for the whole
            job until ``_run_chat_phase`` adds the coordinator's
            ``requests_made`` back AFTER the job returns, so the old check
            always saw a free budget and spent a live check the
            coordinator's own cap had already used up).

            Returns ``(result, requests_spent)``. ``requests_spent`` is 1 the
            FIRST time this cycle a live gateway_auth_check call is made, 0
            every time the memoized result is reused or no check was needed
            at all -- so the coordinator can fold the gate's own live
            request into ``requests_made`` (review finding 18b, 2026-10-19:
            the check must count against the cycle's request budget, or a
            following route could be admitted one request over it).

            ``result`` is ``None`` (review finding 18a, 2026-10-19) when the
            gate itself cannot run its own confirmation within the cycle's
            remaining request/wall budget: the failure must still never be
            written, but the job must stop for the REAL reason (request or
            wall cap), not report a CONFIRMED outage that nobody actually
            checked.
            """
            if self.gateway_auth_check is None:
                return False, 0
            spent = 0
            if self._gateway_auth_result is None:
                remaining_wall = self.max_wall_seconds - (self.monotonic() - self._cycle_started)
                if remaining_requests < 1 or remaining_wall <= 0:
                    stats.stopped_reason = "request_cap" if remaining_wall > 0 else "wall_cap"
                    return None, 0  # cannot confirm within budget: unconfirmed, no write
                # Not stats.requests += 1 here: the coordinator folds
                # `spent` into its own requests_made, and _run_chat_phase
                # adds that to stats.requests once, after this call returns.
                spent = 1
                try:
                    self._gateway_auth_result = self.gateway_auth_check(
                        min(self.probe_timeout_seconds, remaining_wall)
                    )
                except (TimeoutError, OSError, http.client.HTTPException):
                    self._gateway_auth_result = ProbeExchange(http_status=None, ok=False)
            return self._gateway_auth_result.http_status in (401, 403), spent

        coordinator = RefreshCoordinator(
            cache=self.cache,
            transport=self.transport,
            clock=self.clock,
            monotonic=self.monotonic,
            sleep=self.sleep,
            auth_gate=auth_gate,
        )
        config = replace(
            maintenance_from_env(),
            max_routes=max(1, min(self.max_requests // 2, HARD_MAX_ROUTES)),
            max_requests=min(self.max_requests, HARD_MAX_REQUESTS),
            wall_seconds=min(self.max_wall_seconds, HARD_MAX_WALL_SECONDS_MAINTENANCE),
        )

        if not rows:
            # Every prepaid route is already covered by the open cycle.
            self.cache.clear_cursor()
            self._save()
            stats.stopped_reason = stats.stopped_reason or "complete"
            return

        outcome = coordinator.refresh_for_consumer(
            RefreshSnapshot(rows=rows, gateway_origin="", scope=""),
            consumer=CONSUMER_PROVE_AT_REST,
            needed_ids=[row.route_id for row in rows],
            config=config,
            explicit=True,
        )
        stats.requests += outcome.requests_made
        stats.probed += outcome.probed
        stats.fresh += outcome.verified + outcome.alive
        stats.negative += outcome.failed + outcome.unavailable
        stats.skipped_bucket += sum(
            1 for o in outcome.route_outcomes.values() if o.refresh_reason == REASON_BUCKET
        )
        # A route is "covered" by this cycle once the coordinator either
        # probed it or explicitly decided not to (provider/route stop) --
        # only a route left at its default REASON_NOT_TESTED/cap reason is
        # truly unfinished and must carry over to the next cycle's cursor.
        covered = {
            rid
            for rid, o in outcome.route_outcomes.items()
            if o.probed or o.refresh_reason == REASON_PROVIDER_STOP
        }
        probed_ids.update(covered)
        uncovered = len(prepaid) - len(covered)
        if outcome.cap_reason == "auth_outage":
            stats.auth_outage = True
            stats.stopped_reason = "auth_outage"
            self._persist_cursor(probed_ids, open_cycle=True)
        elif uncovered > 0:
            stats.stopped_reason = outcome.cap_reason or "route_cap"
            self._persist_cursor(probed_ids, open_cycle=True)
        else:
            stats.stopped_reason = stats.stopped_reason or "complete"
            self.cache.clear_cursor()
        self._save()

    def _flush_auth_fail_buffer(self, stats: CycleStats) -> None:
        """Write every buffered auth-outage-window negative as a real failure."""
        for buffered_id, buffered_result, buffered_now in self._auth_fail_buffer:
            self.cache.record(buffered_id, buffered_result, buffered_now)
            stats.probed += 1
            stats.negative += 1
            if (
                buffered_result.failure_scope != "route"
                and buffered_result.category in POOL_COOLDOWN_CATEGORIES
            ):
                self._revoke_half_open_for_pool(buffered_result.pool or pool_of(buffered_id))
        self._auth_fail_buffer = []
        self._auth_fail_pools = []

    def _buffer_auth_failure(
        self, route: AdmittedRoute, result: ProbeResult, now: datetime, stats: CycleStats
    ) -> bool:
        """Buffer auth failures in either phase, discarding a systemic outage."""
        if result.http_status not in (401, 403):
            if self._auth_fail_buffer:
                self._flush_auth_fail_buffer(stats)
            return False
        self._auth_fail_buffer.append((route.route_id, result, now))
        self._auth_fail_pools.append(_pool_of(route))
        if self.gateway_auth_check is not None:
            if self._gateway_auth_result is None:
                remaining = self.max_wall_seconds - (self.monotonic() - self._cycle_started)
                if stats.requests >= self._request_cap or remaining <= 0:
                    # Confirmation cannot run within budget. Keep the buffer unwritten.
                    stats.stopped_reason = "request_cap" if remaining > 0 else "wall_cap"
                    return True
                stats.requests += 1
                try:
                    self._gateway_auth_result = self.gateway_auth_check(
                        min(self.probe_timeout_seconds, remaining)
                    )
                except (TimeoutError, OSError, http.client.HTTPException):
                    # Transport or protocol errors never prove an auth outage.
                    self._gateway_auth_result = ProbeExchange(http_status=None, ok=False)
            outage = self._gateway_auth_result.http_status in (401, 403)
            if not outage:
                self._flush_auth_fail_buffer(stats)
        else:
            outage = (
                len(self._auth_fail_pools) >= self.auth_outage_consecutive
                and len(set(self._auth_fail_pools)) >= self.auth_outage_distinct_pools
                and len(set(self._auth_fail_pools)) > len(self._cycle_pools) / 2
            )
        if outage:
            self._auth_fail_buffer = []
            self._auth_fail_pools = []
            stats.auth_outage = True
            stats.stopped_reason = "auth_outage"
        return True

    def _persist_cursor(self, probed_ids: set[str], *, open_cycle: bool) -> None:
        self.cache.set_cursor({"cycle_open": open_cycle, "probed_ids": sorted(probed_ids)})

    @staticmethod
    def _model_identity_matches(route_id: str, reported: str) -> bool:
        """True when *reported* is the expected model, tolerating provider-prefix.

        Delegates to the module-level :func:`model_identity_matches`; retained
        as a staticmethod for existing callers/tests.

        Rules (defect 4 fix):
        - Empty reported: not a mismatch (gateway chose not to echo).
        - Reported HAS a provider prefix: provider must equal the route's
          provider (exact match); suffix alone is insufficient.
        - Reported has NO provider prefix: suffix match is accepted (gateway
          stripped the prefix).
        """
        return model_identity_matches(route_id, reported)

    def _note_rate_limit(
        self, route: AdmittedRoute, exchange: ProbeExchange, now: datetime
    ) -> None:
        if category_for(exchange) != CATEGORY_RATE_LIMITED:
            return
        seconds = exchange.retry_after_seconds if exchange.retry_after_seconds else 60.0
        self.cache.zero_bucket(route.provider, now + timedelta(seconds=seconds), pool=route.pool)

    def _needs_agentic(self, route: AdmittedRoute, now: datetime) -> bool:
        """True when the route is FREE and its agentic probe is stale or missing.

        Uses ``agentic_checked_at`` (not ``checked_at``) so that regular
        single-call probes cannot keep an old agentic PASS from being
        re-probed indefinitely.
        """
        if route.capacity != CapacityClass.FREE.value:
            return False
        entry = self.cache.entry(route.route_id)
        if entry is None:
            return True
        if entry.agentic_checked_at is None:
            return True
        # Use the agentic-specific timestamp (set on every agentic attempt,
        # pass or fail), so a failed route is retried once per interval
        # instead of on every cycle.
        ref = entry.agentic_checked_at
        age = now - ref
        return age.total_seconds() > self.agentic_interval_hours * 3600

    def run_agentic_probes(
        self,
        stats: CycleStats,
        started: float | None = None,
        *,
        routes: Sequence[AdmittedRoute] | None = None,
    ) -> None:
        """Run 3-turn agentic probes for FREE routes that need one.

        Own budget: at most ``agentic_max_routes`` conversations within
        ``agentic_wall_seconds`` (default ``max_wall_seconds``) from *started*.
        Up to ``agentic_concurrency`` conversations run at once, at most
        ``agentic_pool_concurrency`` per credential pool. While auth failures
        await outage confirmation, one conversation runs at a time. A
        conversation cut short by the deadline or the stop signal is not
        tested and writes nothing.
        """
        if self.agentic_transport is None or stats.auth_outage:
            return
        phase_start = started if started is not None else self.monotonic()
        deadline = phase_start + (self.agentic_wall_seconds or self.max_wall_seconds)
        now = self.clock()
        snapshot = list(self.routes_loader()) if routes is None else list(routes)
        if not self._cycle_pools:
            self._cycle_pools = {_pool_of(route) for route in snapshot}
        pending = [
            route
            for route in agentic_candidates(
                snapshot, self.cache, now, include_cooled=self.screen_transport is not None
            )
            if self._needs_agentic(route, now)
        ]
        self._half_open_routes = set()
        self._screen_failed_pools = set()
        if self._stop.is_set():
            stats.agentic_stopped_reason = "stopped"
            return
        if self.monotonic() >= deadline:
            stats.agentic_stopped_reason = "wall_cap"
            return
        if self.screen_transport is not None:
            pending = self._screen(pending, stats, deadline)
            if stats.agentic_stopped_reason:
                return
        pending = pending[: self.agentic_max_routes]
        self._cycle_started = phase_start
        self._request_cap = stats.requests + AGENTIC_TURNS * len(pending) + 1
        if pending:
            # Prove the cache is writable before any dispatch, as the chat phase does.
            self._save()
        running: dict[Future[tuple[list[ProbeExchange], AgenticFakeFile, bool]], AdmittedRoute]
        running = {}
        busy: Counter[str] = Counter()
        with ThreadPoolExecutor(max_workers=self.agentic_concurrency) as pool:
            while pending or running:
                limit = 1 if self._auth_fail_buffer else self.agentic_concurrency
                if not stats.agentic_stopped_reason:
                    if self._stop.is_set():
                        stats.agentic_stopped_reason = "stopped"
                    elif self.monotonic() >= deadline:
                        stats.agentic_stopped_reason = "wall_cap"
                empty_pools: set[str] = set()
                for route in list(pending):
                    if stats.agentic_stopped_reason or len(running) >= limit:
                        break
                    route_pool = _pool_of(route)
                    if busy[route_pool] >= self.agentic_pool_concurrency:
                        continue
                    if route_pool in empty_pools:
                        continue
                    now = self.clock()
                    if route_pool in self._screen_failed_pools or route_pool in (
                        _cooled_pools(self.cache, now)
                    ):
                        # One-shot half-open only for pre-screen cooldowns with
                        # no fresh account-level failure this cycle.
                        if (
                            route_pool not in self._screen_failed_pools
                            and route.route_id in self._half_open_routes
                        ):
                            self._half_open_routes.discard(route.route_id)
                        else:
                            pending.remove(route)
                            continue
                    # Reserve every turn up front; an empty bucket keeps the
                    # route pending while other pools' conversations run.
                    if not self.cache.consume(
                        route.provider, now, pool=route.pool, amount=AGENTIC_TURNS
                    ):
                        empty_pools.add(route_pool)
                        continue
                    pending.remove(route)
                    busy[route_pool] += 1
                    running[pool.submit(self._converse, route.route_id, deadline)] = route
                if not running:
                    if not pending or stats.agentic_stopped_reason:
                        break
                    # Only empty buckets block: wait for the earliest refill
                    # inside the phase budget instead of ending the phase.
                    now = self.clock()
                    ready = [
                        self.cache.bucket_available_at(
                            route.provider, now, pool=route.pool, amount=AGENTIC_TURNS
                        )
                        for route in pending
                    ]
                    known = [at for at in ready if at is not None]
                    wait_seconds = (min(known) - now).total_seconds() if known else None
                    if wait_seconds is None or self.monotonic() + wait_seconds >= deadline:
                        stats.agentic_stopped_reason = "bucket_empty"
                        stats.skipped_bucket += len(pending)
                        break
                    self.sleep(max(wait_seconds, 0.05))
                    if self.clock() <= now:
                        # An injected clock that does not move can never refill a bucket.
                        stats.agentic_stopped_reason = "bucket_empty"
                        stats.skipped_bucket += len(pending)
                        break
                done, _ = wait(running, return_when=FIRST_COMPLETED)
                for future in done:
                    route = running.pop(future)
                    busy[_pool_of(route)] -= 1
                    exchanges, fake_file, finished = future.result()
                    stats.requests += len(exchanges)
                    if stats.auth_outage or not finished or not exchanges:
                        continue
                    self._record_agentic(route, exchanges, fake_file, stats)
                    if stats.auth_outage:
                        stats.agentic_stopped_reason = "auth_outage"
                        pending = []
                    try:
                        self._save()
                    except HealthCacheLockTimeoutError:
                        stats.agentic_stopped_reason = "lock_timeout"
                        pending = []
        if not stats.agentic_stopped_reason:
            stats.agentic_stopped_reason = "complete"
        if self._auth_fail_buffer and (
            self.gateway_auth_check is None or self._gateway_auth_result is not None
        ):
            self._flush_auth_fail_buffer(stats)
            self._save()

    def _screen(
        self, candidates: Sequence[AdmittedRoute], stats: CycleStats, deadline: float
    ) -> list[AdmittedRoute]:
        """Return the candidates that answered the cheap gateway screen, in order.

        One ``test-all`` call per provider/pool, at most ``agentic_max_routes``
        ids each; a cooled pool sends one id (half-open). Failures are recorded
        as negatives (account-level ones cool the pool), 429s zero the pool
        bucket, timeouts write nothing. A screen OK only selects a route for
        the agentic probe; it never admits one. A pool whose screen call
        failed keeps its open candidates unscreened.

        Stop, deadline, and local bucket boundaries are checked before each
        ``test-all`` submit. Screen requests reserve the pool bucket first so
        ADR-012's "budget exhaustion never launch another transport call"
        holds for the screen phase too.
        """
        assert self.screen_transport is not None
        now = self.clock()
        cooled_before = _cooled_pools(self.cache, now)
        groups: dict[tuple[str, str], list[AdmittedRoute]] = {}
        for route in candidates:
            key = (route.provider, _pool_of(route))
            limit = (
                1
                if key[1] in cooled_before
                else min(self.agentic_max_routes, SCREEN_TEST_ALL_MAX_MODELS)
            )
            group = groups.setdefault(key, [])
            if len(group) < limit:
                group.append(route)
        if not groups:
            return []
        remaining = deadline - self.monotonic()
        if self._stop.is_set():
            stats.agentic_stopped_reason = "stopped"
            return []
        if remaining <= 0:
            stats.agentic_stopped_reason = "wall_cap"
            return []
        screen = self.screen_transport

        def _dispatch_screen(
            provider: str, model_ids: list[str]
        ) -> Mapping[str, Mapping[str, Any]] | None:
            # Recheck at worker start: a queued submit must not call transport
            # after stop/deadline trips between submit and run. Timeout is the
            # remaining phase time at actual dispatch, not the stale pre-queue value.
            if self._stop.is_set() or self.monotonic() >= deadline:
                return None
            left = deadline - self.monotonic()
            if left <= 0:
                return None
            return screen(provider, model_ids, left / 2)

        futures: dict[tuple[str, str], Future[Mapping[str, Mapping[str, Any]] | None]] = {}
        with ThreadPoolExecutor(max_workers=self.agentic_concurrency) as pool:
            for key, group in groups.items():
                if self._stop.is_set():
                    stats.agentic_stopped_reason = "stopped"
                    break
                if self.monotonic() >= deadline:
                    stats.agentic_stopped_reason = "wall_cap"
                    break
                amount = len(group)
                now = self.clock()
                if not self.cache.consume(key[0], now, pool=key[1], amount=amount):
                    # Empty bucket: skip this pool's screen; do not launch.
                    continue
                futures[key] = pool.submit(_dispatch_screen, key[0], [r.route_id for r in group])
            outcomes: dict[tuple[str, str], Mapping[str, Mapping[str, Any]] | None] = {}
            for key, future in futures.items():
                try:
                    outcomes[key] = future.result()
                except (OSError, ValueError, http.client.HTTPException):
                    outcomes[key] = None
        survivors: set[str] = set()
        unscreened: set[str] = set()
        failed_pools: set[str] = set()
        for key, group in groups.items():
            if key not in outcomes:
                # Not dispatched (stop/deadline/bucket). Keep open candidates.
                if key[1] not in cooled_before:
                    unscreened.update(route.route_id for route in group)
                continue
            results = outcomes[key]
            if results is None:
                if key[1] not in cooled_before:
                    unscreened.update(route.route_id for route in group)
                continue
            stats.screened += len(group)
            stats.requests += len(results)
            for route in group:
                result = results.get(route.route_id)
                if result is None:
                    continue
                if self._record_screen(route, result, now):
                    survivors.add(route.route_id)
                elif _pool_of(route) in _cooled_pools(self.cache, now):
                    # Fresh account-level negative this screen: never half-open.
                    failed_pools.add(_pool_of(route))
        # Half-open only for pre-screen cooled pools with no fresh account
        # failure this screen. Ordering cannot re-grant a revoked shared pool.
        self._screen_failed_pools |= failed_pools
        half_open = {
            route.route_id
            for route in candidates
            if route.route_id in survivors
            and _pool_of(route) in cooled_before
            and _pool_of(route) not in self._screen_failed_pools
        }
        stats.screen_ok += len(survivors)
        self._half_open_routes = half_open
        if survivors or stats.screened:
            self._save()
        keep = survivors | unscreened
        return [route for route in candidates if route.route_id in keep]

    def _revoke_half_open_for_pool(self, pool: str) -> None:
        """Drop half-open grants after a fresh account-level failure for *pool*."""
        self._screen_failed_pools.add(pool)
        self._half_open_routes = {
            route_id for route_id in self._half_open_routes if pool_of(route_id) != pool
        }

    def _record_screen(
        self, route: AdmittedRoute, result: Mapping[str, Any], now: datetime
    ) -> bool:
        """Apply one ``test-all`` result; True when the route answered."""
        status = result.get("status")
        timed_out = bool(result.get("isTimeout"))
        if status == "ok" or (status == "slow" and not timed_out):
            return True
        code = result.get("statusCode")
        code = code if isinstance(code, int) else None
        if code is None:
            match = re.search(r"\[(\d{3})\]", str(result.get("error") or ""))
            code = int(match.group(1)) if match else None
        if bool(result.get("rateLimited")) or code == 429:
            self.cache.zero_bucket(route.provider, now + timedelta(seconds=60), pool=route.pool)
            return False
        if timed_out or result.get("isTransient") or result.get("skipped") or status != "error":
            return False
        exchange = ProbeExchange(
            http_status=code, ok=False, error_category=None if code else "upstream"
        )
        category = category_for(exchange)
        detail = str(result.get("error") or "")
        scoped = _detail_is_model_scoped(category, code, detail, route.route_id)
        self.cache.record(
            route.route_id,
            ProbeResult(
                category=category,
                chat_ok=False,
                tool_ok=False,
                http_status=code,
                pool=route.pool,
                capacity_evidence=route.capacity_evidence,
                failure_scope="route" if scoped else None,
            ),
            now,
        )
        # Account-level pool-cooling only; 404/timeout/model-scoped leave siblings open.
        if not scoped and category in POOL_COOLDOWN_CATEGORIES:
            self._revoke_half_open_for_pool(_pool_of(route))
        return False

    def _converse(
        self, route_id: str, deadline: float
    ) -> tuple[list[ProbeExchange], AgenticFakeFile, bool]:
        """One read/edit/read conversation. Runs in a worker thread; no cache access.

        Returns the exchanges, the fake file, and whether the conversation
        reached a verdict (all turns ran, or a turn failed) rather than being
        cut short by the deadline or the stop signal.
        """
        assert self.agentic_transport is not None
        fake_file = AgenticFakeFile()
        conversation: list[dict[str, Any]] = []
        exchanges: list[ProbeExchange] = []
        for payload_fn in (agentic_turn1_payload, agentic_turn2_payload, agentic_turn3_payload):
            remaining = deadline - self.monotonic()
            if self._stop.is_set() or remaining <= 0:
                return exchanges, fake_file, False
            payload = payload_fn(route_id, conversation)
            try:
                ex = self.agentic_transport(
                    route_id, payload, min(self.probe_timeout_seconds, remaining)
                )
            except TimeoutError:
                ex = ProbeExchange(http_status=None, ok=False, error_category="timeout")
            exchanges.append(ex)
            if not ex.ok:
                break
            # Simulate tool calls against the fake file and carry the whole
            # conversation (user + assistant + tool results) to the next turn.
            tool_results = _simulate_tool_calls(fake_file, _extract_tool_calls(ex.response_body))
            conversation = list(payload.get("messages", []))
            conversation.append(_extract_assistant_message(ex.response_body))
            conversation.extend(tool_results)
        return exchanges, fake_file, True

    def _record_agentic(
        self,
        route: AdmittedRoute,
        exchanges: Sequence[ProbeExchange],
        fake_file: AgenticFakeFile,
        stats: CycleStats,
    ) -> None:
        now = self.clock()
        last = exchanges[-1]
        self._note_rate_limit(route, last, now)
        if category_for(last) == CATEGORY_RATE_LIMITED:
            # A 429 says nothing about capability: not tested, retried after the bucket reopens.
            return
        passed = score_agentic_probe(exchanges) and fake_file.content == AGENTIC_PROBE_EXPECTED
        # A failed HTTP turn keeps its transport category (catalog_stale,
        # rate_limited, timeout, ...); "agentic_fail" means the model answered
        # but did not do the task.
        category = CATEGORY_OK if passed else ("agentic_fail" if last.ok else category_for(last))
        # An agentic PASS qualifies the selected route only when every turn
        # echoed that route's model. An absent echo can't prove identity.
        if passed and not all(
            ex.reported_model and self._model_identity_matches(route.route_id, ex.reported_model)
            for ex in exchanges
        ):
            passed = False
            category = CATEGORY_MODEL_MISMATCH
        latencies = [ex.latency_ms for ex in exchanges if ex.latency_ms is not None]
        result = ProbeResult(
            category=category,
            http_status=last.http_status,
            chat_ok=exchanges[0].ok,
            tool_ok=passed,
            latency_ms=sum(latencies) if latencies else None,
            retry_after_seconds=last.retry_after_seconds,
            probe_class="agentic",
            agentic_ok=passed,
            pool=route.pool,
            capacity_evidence=route.capacity_evidence,
            identity="verified" if passed else "",
            failure_scope=(
                "route"
                if not passed
                and _detail_is_model_scoped(category, last.http_status, last.detail, route.route_id)
                else None
            ),
        )
        stats.agentic_probed += 1
        stats.agentic_passed += int(passed)
        if self._buffer_auth_failure(route, result, now, stats):
            # Auth buffer may later flush as a real pool-cooling negative.
            # If this result is already a pool-cooling category and not
            # route-scoped, revoke half-open now so a sibling cannot start.
            if (
                not passed
                and result.failure_scope != "route"
                and result.category in POOL_COOLDOWN_CATEGORIES
            ):
                self._revoke_half_open_for_pool(_pool_of(route))
            return
        self.cache.record(route.route_id, result, now)
        stats.probed += 1
        if passed:
            stats.fresh += 1
        else:
            stats.negative += 1
            if result.failure_scope != "route" and result.category in POOL_COOLDOWN_CATEGORIES:
                self._revoke_half_open_for_pool(_pool_of(route))

    def run_forever(self) -> CycleStats | None:
        """Loop ``run_once`` until ``stop``. A cycle error retries next interval."""
        self._stop.clear()
        last: CycleStats | None = None
        while not self._stop.is_set():
            try:
                last = self.run_once()
            except Exception as exc:
                if self.on_cycle_error is not None:
                    self.on_cycle_error(exc)
            remaining = float(self.interval_seconds)
            while remaining > 0 and not self._stop.is_set():
                step = min(0.25, remaining)
                self.sleep(step)
                remaining -= step
        return last


# ---------------------------------------------------------------------------
# Admitted-set loading (same evidence path as `verdict eligibility`)
# ---------------------------------------------------------------------------


# Sentinel: omitted openrouter_free_set means "load from public catalog/cache".
# Explicit None means "no free-set assertion" (hermetic tests / callers without OR).
_OPENROUTER_FREE_SET_LOAD = object()


def load_openrouter_free_set_for_prober(
    *, now: datetime | None = None, cache_path: Path | None = None, ttl_seconds: float | None = None
) -> OpenRouterFreeSet | None:
    """Public OpenRouter 0/0 set for capacity classification (BOD-418).

    When ``cache_path`` is the caller-owned ``state_dir/openrouter-free-set.json``,
    this is the live refresh owner that ``build_selector`` later reads cache-only.
    """
    return load_openrouter_free_set(now=now, cache_path=cache_path, ttl_seconds=ttl_seconds)


def routes_from_evidence(
    inventory_rows: Sequence[Mapping[str, Any]],
    connections: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
    state_dir: Path | None = None,
    live_catalog: Mapping[str, Collection[str]] | None = None,
    openrouter_free_set: OpenRouterFreeSet | object | None = _OPENROUTER_FREE_SET_LOAD,
) -> tuple[AdmittedRoute, ...]:
    """Admit exactly the way ``verdict eligibility`` does, without probing.

    Uses ``admit`` over the live inventory and connections. Routes that fail
    admission are not probed. Capacity class comes from the same ladder
    classifier eligibility uses, so this prober and ``verdict eligibility``
    agree on FREE versus SUBSCRIPTION. ``live_catalog`` (provider -> served
    model ids) only sets the ``live_catalog`` ordering hint.

    ``openrouter_free_set`` (BOD-418) is the public OpenRouter 0/0 membership
    set. When omitted, it is loaded via the public catalog into
    ``state_dir/openrouter-free-set.json`` (same file ``build_selector`` reads
    cache-only). Pass ``None`` to skip the public free-set (no openrouter/
    FREE assertion). OpenRouter-family routes outside that set are never
    classified FREE.
    """
    from verdict.admission import admit, default_runtime_evidence
    from verdict.openrouter_free_set import openrouter_route_is_public_free
    from verdict.orchestration.credential_pools import is_non_chat_route, pool_of

    free_set: OpenRouterFreeSet | None
    if openrouter_free_set is _OPENROUTER_FREE_SET_LOAD:
        # Same caller-owned cache path build_selector reads (state_dir /
        # openrouter-free-set.json). Live refresh writes here; selector is
        # cache-only against the same file — one membership source.
        owned = (state_dir / "openrouter-free-set.json") if state_dir is not None else None
        free_set = load_openrouter_free_set_for_prober(now=now, cache_path=owned)
    else:
        # Explicit None or an OpenRouterFreeSet; never the load sentinel here.
        free_set = (
            openrouter_free_set if isinstance(openrouter_free_set, OpenRouterFreeSet) else None
        )
    evidence = default_runtime_evidence(now=now, state_dir=state_dir)
    admitted = admit(inventory_rows, connections, evidence, now=now)
    by_id: dict[str, Mapping[str, Any]] = {str(row.get("id", "")): row for row in inventory_rows}
    conn_by_provider: dict[str, Mapping[str, Any]] = {}
    for item in connections:
        name = str(item.get("provider", "")).lower()
        if name and name not in conn_by_provider:
            conn_by_provider[name] = item
    out: list[AdmittedRoute] = []
    for record in admitted.records:
        if not record.admitted:
            continue
        row: Mapping[str, Any] = by_id.get(record.route_id, {})
        provider = record.provider or _provider_of(record.route_id)
        conn: Mapping[str, Any] | None = conn_by_provider.get(provider.lower())
        capacity, plan = capacity_class_of(
            conn, row, route_id=record.route_id, openrouter_free_set=free_set
        )
        evidence_label = plan or None
        if capacity.value == "free" and openrouter_route_is_public_free(record.route_id, free_set):
            evidence_label = "openrouter_public_zero"
        out.append(
            AdmittedRoute(
                route_id=record.route_id,
                provider=provider,
                capacity=capacity.value,
                # BOD-297: credential-pool identity (alias-collapsed), so
                # order_cycle can probe one canonical route per pool and the
                # census report can group by pool.
                pool=pool_of(record.route_id),
                capacity_evidence=evidence_label,
                non_chat=is_non_chat_route(record.route_id, row),
                live_catalog=_served_by_gateway(record.route_id, row, live_catalog),
            )
        )
    return tuple(out)


def _served_by_gateway(
    route_id: str, row: Mapping[str, Any], live_catalog: Mapping[str, Collection[str]] | None
) -> bool | None:
    """True/False when the provider's synced catalog is known, else None."""
    provider = str(row.get("owned_by") or "").lower()
    if live_catalog is None or provider not in live_catalog:
        return None
    served = live_catalog[provider]
    model = route_id.split("/", 1)[1] if "/" in route_id else route_id
    return model in served or str(row.get("root") or "") in served


def fetch_live_catalog(
    origin: str, *, api_key: str | None, timeout: float = 30.0
) -> dict[str, frozenset[str]] | None:
    """Provider -> model ids the gateway serves (``/api/synced-available-models``).

    ``/v1/models`` also lists ids the gateway rejects with "not available in
    the active live catalog". This read is an ordering hint only, so an
    unreadable catalog returns None (no evidence) instead of failing the cycle.
    """
    from verdict.orchestration.run import _get_json

    try:
        raw = _get_json(
            origin.rstrip("/") + "/api/synced-available-models", api_key=api_key, timeout=timeout
        )
    except (OSError, ValueError, http.client.HTTPException):
        return None
    if not isinstance(raw, Mapping):
        return None
    return {
        str(provider).lower(): frozenset(
            str(item["id"]) for item in items if isinstance(item, Mapping) and item.get("id")
        )
        for provider, items in raw.items()
        if isinstance(items, list)
    }


def load_admitted_routes(
    gateway: str, *, api_key: str | None, now: datetime | None = None, timeout: float = 30.0
) -> tuple[AdmittedRoute, ...]:
    """Fetch inventory and connections, then admit. Same GETs as eligibility."""
    from verdict.orchestration.run import fetch_connections, fetch_inventory

    current = now or _now()
    origin = gateway.rstrip("/")
    if origin.endswith("/v1"):
        origin = origin[: -len("/v1")]
    rows = fetch_inventory(origin, api_key=api_key, timeout=timeout)
    connections = fetch_connections(origin, api_key=api_key, timeout=timeout)
    live = fetch_live_catalog(origin, api_key=api_key, timeout=timeout)
    return routes_from_evidence(rows, connections, now=current, live_catalog=live)


# ---------------------------------------------------------------------------
# Live transport and daemon construction
# ---------------------------------------------------------------------------


def _exchange_from_body(
    status: int | None,
    body: Mapping[str, Any] | None,
    *,
    phase: str,
    latency_ms: float | None,
    retry_after: float | None,
) -> ProbeExchange:
    if not isinstance(body, Mapping):
        body = {}
    # Capture the reported model identity before deciding ok (defect 3 fix).
    reported_model: str = str(body.get("model", "")) if isinstance(body, Mapping) else ""
    choices = body.get("choices")
    message: Mapping[str, Any] = {}
    if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
        raw_message = choices[0].get("message")
        if isinstance(raw_message, Mapping):
            message = raw_message
    content = message.get("content")
    text = content.strip() if isinstance(content, str) else ""
    tool_calls = message.get("tool_calls")
    called = False
    if isinstance(tool_calls, list):
        for call in tool_calls:
            if not isinstance(call, Mapping):
                continue
            fn = call.get("function")
            name = fn.get("name") if isinstance(fn, Mapping) else None
            if name == TOOL_NAME:
                called = True
    ok = status == 200
    return ProbeExchange(
        http_status=status,
        ok=ok,
        chat_exact=(phase == "chat" and text == "OK"),
        tool_called=(phase == "tool" and called),
        latency_ms=latency_ms,
        retry_after_seconds=retry_after,
        error_category=None,
        reported_model=reported_model,
    )


def _http_error_exchange(exc: urllib.error.HTTPError, latency_ms: float) -> ProbeExchange:
    """Map a gateway HTTP error; a 400 for an id the gateway does not serve is catalog_stale."""
    retry_after = None
    header = exc.headers.get("Retry-After") if exc.headers else None
    if header:
        try:
            retry_after = float(header)
        except ValueError:
            retry_after = None
    named = None
    try:
        body = exc.read(16_384).decode("utf-8", "replace") if exc.fp is not None else ""
    except OSError:
        body = ""
    if exc.code == 400 and is_catalog_stale_error(body):
        named = CATEGORY_CATALOG_STALE
    return ProbeExchange(
        http_status=exc.code,
        ok=False,
        latency_ms=latency_ms,
        retry_after_seconds=retry_after,
        error_category=category_for(
            ProbeExchange(http_status=exc.code, ok=False, error_category=named)
        ),
        detail=_error_detail(body),
    )


def _error_detail(body: str) -> str:
    """The provider message without URLs, for model-vs-account scope only."""
    message = body
    try:
        parsed = json.loads(body)
    except ValueError:
        parsed = None
    if isinstance(parsed, Mapping):
        error = parsed.get("error")
        if isinstance(error, Mapping) and isinstance(error.get("message"), str):
            message = error["message"]
    return re.sub(r"https?://\S+", "<url>", message)[:300]


def live_agentic_transport(
    base_url: str, *, api_key: str | None
) -> Callable[[str, dict[str, Any], float], ProbeExchange]:
    """OpenAI-compatible transport for agentic probes (arbitrary payloads).

    Built only for a consented live daemon behind ``--allow-live-probe``.
    """
    endpoint = base_url.rstrip("/")
    if endpoint.endswith("/v1"):
        endpoint = endpoint[: -len("/v1")]
    endpoint = endpoint + "/v1/chat/completions"

    def transport(route_id: str, payload: dict[str, Any], timeout_seconds: float) -> ProbeExchange:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        started = time.monotonic()
        try:
            with open_no_redirect(request, timeout=timeout_seconds) as response:
                raw = response.read(1_048_576)
                elapsed = (time.monotonic() - started) * 1000.0
                parsed = json.loads(raw) if raw else {}
                body = parsed if isinstance(parsed, Mapping) else {}
                return ProbeExchange(
                    http_status=response.status,
                    ok=200 <= response.status < 300,
                    latency_ms=elapsed,
                    response_body=body,
                    reported_model=str(body.get("model") or ""),
                )
        except urllib.error.HTTPError as exc:
            return _http_error_exchange(exc, (time.monotonic() - started) * 1000.0)
        except TimeoutError:
            return ProbeExchange(http_status=None, ok=False, error_category="timeout")

    return transport


# Hard cap on ids per /api/models/test-all call; larger screens time out upstream.
SCREEN_TEST_ALL_MAX_MODELS = 20


def live_screen_transport(base_url: str, *, api_key: str | None) -> ScreenTransportFn:
    """``POST /api/models/test-all`` with the M1 body (no auto-hide, rate limits respected)."""
    endpoint = base_url.rstrip("/")
    if endpoint.endswith("/v1"):
        endpoint = endpoint[: -len("/v1")]
    endpoint += "/api/models/test-all"

    def screen(
        provider_id: str, model_ids: Sequence[str], timeout_seconds: float
    ) -> Mapping[str, Mapping[str, Any]] | None:
        body = {
            "providerId": provider_id,
            "modelIds": list(model_ids)[:SCREEN_TEST_ALL_MAX_MODELS],
            "respectRateLimit": True,
            "autoHideFailed": False,
        }
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with open_no_redirect(request, timeout=timeout_seconds) as response:
                parsed = json.loads(response.read(4_194_304) or b"{}")
        except (OSError, ValueError, http.client.HTTPException):
            return None
        results = parsed.get("results") if isinstance(parsed, Mapping) else None
        if not isinstance(results, Mapping):
            return None
        return {str(k): v for k, v in results.items() if isinstance(v, Mapping)}

    return screen


def live_gateway_auth_check(
    base_url: str, *, api_key: str | None
) -> Callable[[float], ProbeExchange]:
    """Check gateway credentials without contacting a model or following redirects."""
    endpoint = base_url.rstrip("/")
    if not endpoint.endswith("/v1"):
        endpoint += "/v1"
    endpoint += "/models"

    def check(timeout_seconds: float) -> ProbeExchange:
        headers = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(endpoint, headers=headers, method="GET")
        try:
            with open_no_redirect(request, timeout=timeout_seconds) as response:
                return ProbeExchange(http_status=response.status, ok=response.status == 200)
        except urllib.error.HTTPError as exc:
            return ProbeExchange(http_status=exc.code, ok=False)
        except (OSError, http.client.HTTPException):
            return ProbeExchange(http_status=None, ok=False)

    return check


def live_transport(
    base_url: str, *, api_key: str | None, extra_headers: Mapping[str, str] | None = None
) -> ProbeTransportFn:
    """OpenAI-compatible transport. Built only for a consented live daemon.

    ``extra_headers`` is the census-only option for per-request probe headers
    (X-OmniRoute-Budget-Fallback: strict); prove-at-rest callers never pass it,
    so their request shape and policy are unchanged.
    """
    endpoint = base_url.rstrip("/")
    if endpoint.endswith("/v1"):
        endpoint = endpoint[: -len("/v1")]
    endpoint = endpoint + "/v1/chat/completions"

    def transport(route_id: str, phase: str, timeout_seconds: float) -> ProbeExchange:
        payload = chat_payload(route_id) if phase == "chat" else tool_payload(route_id)
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        if extra_headers:
            headers.update(extra_headers)
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        started = time.monotonic()
        try:
            with open_no_redirect(request, timeout=timeout_seconds) as response:
                raw = response.read(1_048_576)
                elapsed = (time.monotonic() - started) * 1000.0
                parsed = json.loads(raw) if raw else {}
                return _exchange_from_body(
                    response.status,
                    parsed if isinstance(parsed, Mapping) else {},
                    phase=phase,
                    latency_ms=elapsed,
                    retry_after=None,
                )
        except urllib.error.HTTPError as exc:
            return _http_error_exchange(exc, (time.monotonic() - started) * 1000.0)
        except TimeoutError:
            return ProbeExchange(http_status=None, ok=False, error_category="timeout")

    return transport


# ---------------------------------------------------------------------------
# Bounded full-probe API (BOD-292)
# ---------------------------------------------------------------------------
#
# The daemon's ``run_once`` cannot implement a safe, bounded, explicit-id
# refresh job (global cursor, liveness policy, multi-writer snapshot). This
# API probes one explicit route, chat then required tool, honouring the
# coordinator's deadline and cancellation checks BEFORE each dispatch and
# validating the reported model identity across BOTH phases. It never writes:
# the coordinator serializes every cache write through
# ``HealthCache.merge_and_save`` so daemon and joined workers cannot clobber
# each other. Daemon defaults and behaviour are unchanged.

# A failed probe whose category is one of these must NOT write route health:
# the request/infra was rejected before the model was ever contacted, so the
# route and provider stay as they were (no poison).
NO_HEALTH_WRITE_CATEGORIES = frozenset({CONTEXT_LENGTH_CATEGORY, "gateway_busy"})

# Why a full probe produced no health write.
NOWRITE_PARTIAL = "partial_not_tested"  # chat passed but tool never dispatched
NOWRITE_CONTEXT_LENGTH = CONTEXT_LENGTH_CATEGORY
NOWRITE_GATEWAY_BUSY = "gateway_busy"
NOWRITE_DEADLINE = "deadline"  # deadline elapsed before the chat dispatch
NOWRITE_CANCELLED = "cancelled"  # cancellation requested before the chat dispatch


def _no_write_category(exchange: ProbeExchange) -> str | None:
    """Return the no-write category for *exchange*, else None.

    Context-length overflow and gateway-local admission sheds are request or
    infrastructure problems, not route/provider health, so they never poison
    the cache.
    """
    named = (exchange.error_category or "").strip().lower()
    if named in {CONTEXT_LENGTH_CATEGORY, "context_length", "context_length_exceeded"}:
        return CONTEXT_LENGTH_CATEGORY
    if named in {"gateway_busy", "chat_admission_busy", "admission_busy"}:
        return "gateway_busy"
    return None


# Ambiguous account-wide errors must never leave sibling routes eligible.
_ACCOUNT_SCOPE_MARKERS = ("account", "all models", "this provider", "organization", "plan")


def _detail_is_model_scoped(
    category: str,
    http_status: int | None,
    detail: str,
    route_id: str = "",
    *,
    openrouter_free_set: OpenRouterFreeSet | None = None,
) -> bool:
    """Only a named route/model without account-wide markers is route-scoped.

    BOD-418: a Key-limit 403/402 on an OpenRouter route that is *not* in the
    public 0/0 free set is route-scoped so it cannot cool the FREE pool.
    The same error on a confirmed-free route stays account/provider scoped.
    """
    free_set = openrouter_free_set
    if free_set is None and route_id.split("/", 1)[0].lower() == "openrouter":
        free_set = load_openrouter_free_set_for_prober()
    if key_limit_on_non_free_is_route_scoped(
        route_id=route_id, http_status=http_status, detail=detail, free_set=free_set
    ):
        return True
    # OmniRoute prefixes every relayed error with "[<route_id>]"; that names
    # the requested route, not the model the provider refused.
    text = re.sub(r"^\[[^\]]*\]\s*", "", detail.strip()).lower()
    if not text or not route_id or any(marker in text for marker in _ACCOUNT_SCOPE_MARKERS):
        return False
    if http_status not in {403, 429} and category not in {
        CATEGORY_PERMISSION,
        CATEGORY_RATE_LIMITED,
    }:
        return False
    names = {route_id.lower(), route_id.split("/", 1)[-1].lower()}
    return any(
        re.search(r"(?<![\w/.-])" + re.escape(name) + r"(?![\w/.-])", text)
        for name in names
        if name
    )


def _aggregate_identity(reports: Sequence[str], route_id: str) -> str:
    """Fold the reported ids from every probe phase into one identity verdict.

    ``mismatch`` if any phase echoed a different model; ``verified`` if at
    least one phase echoed a matching id and none mismatched; ``not_reported``
    when no phase echoed any id.
    """
    saw_match = False
    for reported in reports:
        if not reported:
            continue
        if model_identity_matches(route_id, reported):
            saw_match = True
        else:
            return "mismatch"
    return "verified" if saw_match else "not_reported"


@dataclass(frozen=True)
class FullProbeOutcome:
    """Result of one bounded full probe, for the coordinator to persist.

    ``result`` is None exactly when ``no_write`` is True: nothing may be
    written to route health (a partial/cancelled/deadline probe, or a
    request/infra-scope rejection). ``requests_made`` is the actual number of
    HTTP calls dispatched (0, 1 or 2) so the coordinator can release the
    unused half of a two-request reservation. ``category`` is always the
    observed category, even when no write happens.
    """

    route_id: str
    result: ProbeResult | None
    requests_made: int
    tool_tested: bool
    no_write: bool
    no_write_reason: str
    category: str
    http_status: int | None = None
    retry_after_seconds: float | None = None
    latency_ms: float | None = None
    # Sanitized provider message, kept so the coordinator can tell an explicit
    # model-scoped denial / per-model quota from an account/provider blocker.
    detail: str = ""
    # True for a chat-only liveness probe (metered/unknown under explicit
    # consent): success is recorded via ``record_liveness`` (tool_ok stays
    # false), never as a coding-worker proof.
    liveness: bool = False

    @property
    def rate_limited(self) -> bool:
        return self.category == CATEGORY_RATE_LIMITED or self.http_status == 429

    @property
    def model_scoped(self) -> bool:
        """True when the failure names a single model, not the account/provider.

        A route-scoped 403 (explicit model denial) or 429 (per-model quota)
        leaves sibling routes on the same provider eligible (design s7:156,159).
        """
        return _detail_is_model_scoped(self.category, self.http_status, self.detail, self.route_id)


def probe_full(
    route: AdmittedRoute,
    transport: ProbeTransportFn,
    *,
    timeout_seconds: float,
    deadline_ok: Callable[[], bool] = lambda: True,
    cancelled: Callable[[], bool] = lambda: False,
) -> FullProbeOutcome:
    """Probe one route chat-then-tool, bounded by the coordinator's checks.

    ``deadline_ok`` returns True while time remains; ``cancelled`` returns True
    once a shared cancel is requested. Both are checked BEFORE each HTTP
    dispatch. This function performs no cache or bucket writes and no retries.
    """
    # Gate before the first dispatch.
    if cancelled():
        return FullProbeOutcome(
            route.route_id, None, 0, False, True, NOWRITE_CANCELLED, CATEGORY_OK
        )
    if not deadline_ok():
        return FullProbeOutcome(route.route_id, None, 0, False, True, NOWRITE_DEADLINE, CATEGORY_OK)

    try:
        chat = transport(route.route_id, "chat", timeout_seconds)
    except TimeoutError:
        chat = ProbeExchange(http_status=None, ok=False, error_category="timeout")
    requests_made = 1
    latency = chat.latency_ms
    chat_ok = bool(chat.ok and chat.chat_exact and chat.http_status == 200)

    # Request/infra-scope rejections never write health.
    nowrite = _no_write_category(chat)
    if nowrite is not None:
        return FullProbeOutcome(
            route.route_id,
            None,
            requests_made,
            False,
            True,
            nowrite,
            nowrite,
            http_status=chat.http_status,
            retry_after_seconds=chat.retry_after_seconds,
            latency_ms=latency,
        )

    category = category_for(chat)
    status = chat.http_status
    observed = chat
    # Chat-phase identity check: a wrong model voids the chat success and we do
    # not dispatch the tool call.
    if chat_ok and not model_identity_matches(route.route_id, chat.reported_model):
        category = CATEGORY_MODEL_MISMATCH
        chat_ok = False

    tool_ok = False
    tool_tested = False
    if chat_ok:
        # Deadline / cancel BEFORE the tool dispatch. A partial chat without a
        # tool call is NOT_TESTED full proof: leave prior proof unchanged, do
        # not record a negative or renew coding health.
        if cancelled():
            return FullProbeOutcome(
                route.route_id,
                None,
                requests_made,
                False,
                True,
                NOWRITE_CANCELLED,
                CATEGORY_OK,
                http_status=status,
                latency_ms=latency,
            )
        if not deadline_ok():
            return FullProbeOutcome(
                route.route_id,
                None,
                requests_made,
                False,
                True,
                NOWRITE_DEADLINE,
                CATEGORY_OK,
                http_status=status,
                latency_ms=latency,
            )
        try:
            tool = transport(route.route_id, "tool", timeout_seconds)
        except TimeoutError:
            tool = ProbeExchange(http_status=None, ok=False, error_category="timeout")
        requests_made = 2
        tool_tested = True
        tool_nowrite = _no_write_category(tool)
        if tool_nowrite is not None:
            # The tool phase hit a request/infra rejection: chat passed but the
            # tool was never truly tested, so leave prior proof unchanged.
            return FullProbeOutcome(
                route.route_id,
                None,
                requests_made,
                True,
                True,
                tool_nowrite,
                tool_nowrite,
                http_status=tool.http_status,
                retry_after_seconds=tool.retry_after_seconds,
                latency_ms=latency,
            )
        tool_ok = bool(tool.ok and tool.tool_called and tool.http_status == 200)
        if tool.latency_ms is not None:
            latency = (latency or 0.0) + tool.latency_ms
        if tool_ok and not model_identity_matches(route.route_id, tool.reported_model):
            tool_ok = False
            category = CATEGORY_MODEL_MISMATCH
            status = tool.http_status
            observed = tool
        elif not tool_ok:
            status = tool.http_status
            category = category_for(tool)
            observed = tool

    if chat_ok and tool_ok:
        category = CATEGORY_OK
        status = 200

    identity = _aggregate_identity(
        [chat.reported_model, observed.reported_model if tool_tested else ""], route.route_id
    )
    if category == CATEGORY_MODEL_MISMATCH:
        identity = "mismatch"

    result = ProbeResult(
        category=category,
        chat_ok=chat_ok,
        tool_ok=tool_ok,
        latency_ms=latency,
        http_status=status,
        retry_after_seconds=observed.retry_after_seconds,
        pool=route.pool,
        capacity_evidence=route.capacity_evidence,
        identity=identity,
    )
    return FullProbeOutcome(
        route.route_id,
        result,
        requests_made,
        tool_tested,
        False,
        "",
        category,
        http_status=status,
        retry_after_seconds=observed.retry_after_seconds,
        latency_ms=latency,
        detail=observed.detail,
    )


def probe_liveness(
    route: AdmittedRoute,
    transport: ProbeTransportFn,
    *,
    timeout_seconds: float,
    deadline_ok: Callable[[], bool] = lambda: True,
    cancelled: Callable[[], bool] = lambda: False,
) -> FullProbeOutcome:
    """Chat-only liveness probe (one request) for a metered/unknown route.

    Used ONLY by an explicitly consented manual plan: metered/unknown routes
    are never auto-probed, and even under consent they must not burn a second
    (tool) request. A chat success is a liveness success (``tool_ok`` false,
    recorded via ``record_liveness``), never a coding-worker proof. Identity,
    no-write (context-length/gateway-busy) and failure classification match
    :func:`probe_full`'s chat phase.
    """
    if cancelled():
        return FullProbeOutcome(
            route.route_id, None, 0, False, True, NOWRITE_CANCELLED, CATEGORY_OK, liveness=True
        )
    if not deadline_ok():
        return FullProbeOutcome(
            route.route_id, None, 0, False, True, NOWRITE_DEADLINE, CATEGORY_OK, liveness=True
        )
    try:
        chat = transport(route.route_id, "chat", timeout_seconds)
    except TimeoutError:
        chat = ProbeExchange(http_status=None, ok=False, error_category="timeout")
    latency = chat.latency_ms
    chat_ok = bool(chat.ok and chat.chat_exact and chat.http_status == 200)

    nowrite = _no_write_category(chat)
    if nowrite is not None:
        return FullProbeOutcome(
            route.route_id,
            None,
            1,
            False,
            True,
            nowrite,
            nowrite,
            http_status=chat.http_status,
            retry_after_seconds=chat.retry_after_seconds,
            latency_ms=latency,
            detail=chat.detail,
            liveness=True,
        )

    category = category_for(chat)
    status = chat.http_status
    if chat_ok and not model_identity_matches(route.route_id, chat.reported_model):
        category = CATEGORY_MODEL_MISMATCH
        chat_ok = False
    if chat_ok:
        category = CATEGORY_OK
        status = 200
    identity = _aggregate_identity([chat.reported_model], route.route_id)
    if category == CATEGORY_MODEL_MISMATCH:
        identity = "mismatch"
    result = ProbeResult(
        category=category,
        chat_ok=chat_ok,
        # Liveness never claims the required tool call; a chat-only success is
        # recorded through ``record_liveness`` so ``healthy`` tool gates exclude
        # it. A chat FAILURE still records a negative with tool_ok false.
        tool_ok=False,
        latency_ms=latency,
        http_status=status,
        retry_after_seconds=chat.retry_after_seconds,
        pool=route.pool,
        capacity_evidence=route.capacity_evidence,
        identity=identity,
    )
    return FullProbeOutcome(
        route.route_id,
        result,
        1,
        False,
        False,
        "",
        category,
        http_status=status,
        retry_after_seconds=chat.retry_after_seconds,
        latency_ms=latency,
        detail=chat.detail,
        liveness=True,
    )


@dataclass
class ProverDaemon:
    """Long-running prober wired to a gateway. Writes only the health cache."""

    prober: Prober
    consented: bool

    def run_once(self) -> CycleStats:
        if not self.consented:
            raise ProveError(
                "live prove-at-rest requires explicit consent; pass --allow-live-probe"
            )
        return self.prober.run_once()

    def run_forever(self) -> CycleStats | None:
        if not self.consented:
            raise ProveError(
                "live prove-at-rest requires explicit consent; pass --allow-live-probe"
            )
        return self.prober.run_forever()

    def stop(self) -> None:
        self.prober.stop()


def build_live_daemon(
    *,
    state_path: Path | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    probe_timeout_seconds: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
    allow_live_probe: bool = False,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    max_wall_seconds: float = DEFAULT_MAX_WALL_SECONDS,
    concurrency: int = DEFAULT_CONCURRENCY,
    transport: ProbeTransportFn | None = None,
    cache_path: Path | None = None,
    agentic_max_routes: int = DEFAULT_AGENTIC_MAX_ROUTES,
    agentic_concurrency: int = DEFAULT_AGENTIC_CONCURRENCY,
    agentic_pool_concurrency: int = DEFAULT_AGENTIC_POOL_CONCURRENCY,
    agentic_wall_seconds: float | None = None,
) -> ProverDaemon:
    """Construct the daemon. ``state_path`` is accepted and ignored.

    The legacy cycle file (``state_path`` / ``VERDICT_PROVE_AT_REST_STATE``)
    is not read and not written. The daemon writes ``cache_path`` or
    ``VERDICT_HEALTH_CACHE`` or ``$VERDICT_HOME/health-cache.json`` (default
    ``~/.verdict``); see ``health_cache.default_cache_path``.
    """
    del state_path  # legacy document: ignored, see module docstring
    from verdict.free_tier_admit import normalize_omniroute_origin, omniroute_endpoint_from_env

    if base_url and base_url.strip():
        origin = normalize_omniroute_origin(base_url.strip())
        key = api_key
    else:
        found = omniroute_endpoint_from_env()
        if found is None:
            raise ProveError(
                "OmniRoute endpoint required; set OMNIROUTE_BASE_URL or pass --base-url"
            )
        origin = normalize_omniroute_origin(found[0])
        key = api_key if api_key is not None else found[1]

    if key is None:
        from verdict.orchestration.run import resolve_api_key

        key = resolve_api_key()

    def loader() -> tuple[AdmittedRoute, ...]:
        return load_admitted_routes(origin, api_key=key)

    cache = HealthCache(cache_path or default_cache_path())
    chosen = transport if transport is not None else live_transport(origin, api_key=key)
    # Wire the agentic transport (same OmniRoute client) behind the same
    # --allow-live-probe opt-in the single-call probe uses.
    agentic = live_agentic_transport(origin, api_key=key) if allow_live_probe else None
    prober = Prober(
        cache=cache,
        routes_loader=loader,
        transport=chosen,
        max_requests=max_requests,
        max_wall_seconds=max_wall_seconds,
        concurrency=concurrency,
        probe_timeout_seconds=probe_timeout_seconds,
        interval_seconds=interval_seconds,
        agentic_transport=agentic,
        agentic_max_routes=agentic_max_routes,
        agentic_concurrency=agentic_concurrency,
        agentic_pool_concurrency=agentic_pool_concurrency,
        agentic_wall_seconds=agentic_wall_seconds,
        gateway_auth_check=(
            live_gateway_auth_check(origin, api_key=key) if transport is None else None
        ),
        screen_transport=(
            live_screen_transport(origin, api_key=key)
            if allow_live_probe and transport is None
            else None
        ),
    )
    return ProverDaemon(prober=prober, consented=allow_live_probe)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def import_sessions(
    ledger: str | Path, cache: HealthCache, *, now: datetime | None = None
) -> dict[str, Any]:
    """Import trusted controller outcomes as exact-route agentic evidence.

    Reuses BOD-299's importer. Session evidence does not refresh chat health,
    capacity, entitlement or identity. Future timestamps are never accepted.
    """
    from verdict.orchestration.session_evidence import import_worker_outcomes

    current = now or _now()
    outcomes = import_worker_outcomes(ledger)
    updated = 0

    def mutate(store: HealthCache) -> None:
        nonlocal updated
        for item in sorted(outcomes, key=lambda item: (item.at, item.outcome == "fail")):
            if item.verified_by_controller and item.at <= current:
                updated += store.record_agentic_evidence(
                    item.route_id,
                    passed=item.outcome == "pass",
                    at=item.at,
                    source=item.source,
                    child_id=item.child_id,
                )

    cache.merge_and_save(mutate)
    return {
        "cache_path": str(cache.path),
        "ledger": str(ledger),
        "read": len(outcomes),
        "updated": updated,
    }


def status_report(
    cache: HealthCache,
    *,
    now: datetime | None = None,
    capacity_class: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Counts by state and class, top healthy coding workers, cold providers."""
    current = now or _now()
    by_state = {STATE_FRESH: 0, STATE_STALE: 0, STATE_NEGATIVE: 0, STATE_UNPROBED: 0}
    by_class: dict[str, int] = {}
    agentic_by_class = dict.fromkeys(("free", "subscription", "metered", "unknown"), 0)
    workers: list[HealthEntry] = []
    providers: dict[str, set[str]] = {}
    for entry in cache.routes().values():
        state = entry.state_at(current)
        by_state[state] = by_state.get(state, 0) + 1
        evidence = entry.capacity_evidence or "unknown"
        proof = agentic_capability(entry, current)
        if proof["qualified"]:
            capacity = (
                capacity_class(entry.route_id)
                if capacity_class is not None
                else (entry.capacity_evidence or "unknown").lower()
            )
            agentic_by_class[capacity if capacity in agentic_by_class else "unknown"] += 1

        if state in {STATE_FRESH, STATE_STALE}:
            by_class[evidence] = by_class.get(evidence, 0) + 1
        provider = _provider_of(entry.route_id)
        providers.setdefault(provider, set()).add(state)
        if state in {STATE_FRESH, STATE_STALE} and entry.tool_ok:
            workers.append(entry)
    workers.sort(key=lambda item: (item.latency_ms is None, item.latency_ms or 0.0))
    cold = sorted(
        provider
        for provider, states in providers.items()
        if STATE_FRESH not in states and STATE_STALE not in states
    )
    # Providers that appear only as negatives, plus providers we were told
    # about through buckets but have no fresh route.
    return {
        "cache_path": str(cache.path),
        "schema_version": "1",
        "route_count": len(cache.routes()),
        "counts_by_state": by_state,
        "agentic_qualified_by_capacity_class": agentic_by_class,
        "agentic_capability_evidence": {
            entry.route_id: agentic_capability(entry, current)
            for entry in cache.routes().values()
            if entry.session_agentic_at is not None or entry.agentic_checked_at is not None
        },
        "healthy_by_capacity_evidence": by_class,
        "top_healthy_coding_workers": [
            {
                "route_id": entry.route_id,
                "latency_ms": entry.latency_ms,
                "pool": entry.pool,
                "capacity_evidence": entry.capacity_evidence,
                "checked_at": entry.checked_at.isoformat().replace("+00:00", "Z"),
                # Defect 5 fix: surface identity so callers can filter out
                # routes whose identity was never verified.
                "identity": entry.identity or "unknown",
            }
            for entry in workers[:10]
        ],
        "cold_providers": cold,
        "legacy_state": "ignored",
    }


# ---------------------------------------------------------------------------
# Census (BOD-297: pool-aware, read-only; no live calls)
# ---------------------------------------------------------------------------


def census_report(
    routes: Sequence[AdmittedRoute], cache: HealthCache, *, now: datetime | None = None
) -> dict[str, Any]:
    """Per-canonical-pool rollup over an admitted-route inventory and the cache.

    Read-only: reads ``cache`` and the given ``routes`` (a fixture inventory
    in tests, ``routes_from_evidence`` output otherwise). Never probes and
    never writes. Non-chat routes are tallied under ``non_chat_skipped`` and
    excluded from every pool's counts. Alias duplicates (BOD-297 collapse)
    are tallied under their pool's ``inherited_from_alias``: an inherited
    route borrows its canonical member's *reported* status for this report
    only -- nothing is written to the cache on its behalf.
    """
    from verdict.orchestration.credential_pools import collapse_alias_duplicates

    current = now or _now()
    chat_routes = [route for route in routes if not route.non_chat]
    non_chat_skipped = len(routes) - len(chat_routes)
    kept_ids, inherited = collapse_alias_duplicates([route.route_id for route in chat_routes])
    kept_id_set = set(kept_ids)

    def _status_of(route_id: str) -> tuple[str, str]:
        """Return ``(bucket, category)`` for one canonical route_id's cache state."""
        lookup = cache.lookup(route_id, current)
        entry = lookup.entry
        if entry is None or lookup.state == STATE_UNPROBED:
            return "unknown_unprobed", ""
        if lookup.healthy and entry.tool_ok:
            return "usable", CATEGORY_OK
        if lookup.healthy:
            return "usable_liveness_only", CATEGORY_OK
        return "unusable", entry.category or "unknown"

    pools: dict[str, dict[str, Any]] = {}

    def _pool_bucket(pool: str) -> dict[str, Any]:
        return pools.setdefault(
            pool,
            {
                "routes": 0,
                "canonical_probed": 0,
                "usable": 0,
                "agentic_ok": 0,
                "unusable": {},
                "unknown_unprobed": 0,
                "inherited_from_alias": 0,
            },
        )

    for route in chat_routes:
        pool = route.pool or route.provider
        bucket = _pool_bucket(pool)
        bucket["routes"] += 1
        if route.route_id not in kept_id_set:
            bucket["inherited_from_alias"] += 1
            continue
        status, category = _status_of(route.route_id)
        bucket["canonical_probed"] += 1
        if status in {"usable", "usable_liveness_only"}:
            bucket["usable"] += 1
        elif status == "unusable":
            bucket["unusable"][category] = bucket["unusable"].get(category, 0) + 1
        else:
            bucket["unknown_unprobed"] += 1
        entry = cache.entry(route.route_id)
        if entry is not None and entry.agentic_ok:
            bucket["agentic_ok"] += 1

    return {
        "schema_version": "1",
        "cache_path": str(cache.path),
        "non_chat_skipped": non_chat_skipped,
        "pools": dict(sorted(pools.items())),
        "inherited": dict(sorted(inherited.items())),
    }


__all__ = [
    "CHAT_PROBE_MESSAGE",
    "DEFAULT_CONCURRENCY",
    "DEFAULT_EPSILON",
    "DEFAULT_INTERVAL_SECONDS",
    "DEFAULT_MAX_REQUESTS",
    "DEFAULT_MAX_WALL_SECONDS",
    "DEFAULT_PROBE_TIMEOUT_SECONDS",
    "ENV_CACHE_PATH",
    "ENV_STATE_PATH",
    "LEGACY_STATE_RELATIVE",
    "NO_HEALTH_WRITE_CATEGORIES",
    "PROVE_AT_REST_SCHEMA_VERSION",
    "STATUS_FAILED",
    "STATUS_HEALTHY",
    "STATUS_SKIPPED",
    "TOOL_NAME",
    "AdmittedRoute",
    "CycleStats",
    "FullProbeOutcome",
    "ProbeExchange",
    "Prober",
    "ProofResult",
    "ProveAtRestCycle",
    "ProveAtRestDaemon",
    "ProveAtRestError",
    "ProveAtRestStore",
    "ProveError",
    "ProverDaemon",
    "build_live_daemon",
    "category_for",
    "census_report",
    "chat_payload",
    "default_state_path",
    "load_admitted_routes",
    "load_healthy_passports",
    "model_identity_matches",
    "order_cycle",
    "passport_from_probe",
    "probe_full",
    "routes_from_evidence",
    "status_report",
    "tool_payload",
]
