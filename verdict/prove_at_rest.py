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

import json
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from verdict.http_safety import open_no_redirect
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
    STATE_FRESH,
    STATE_NEGATIVE,
    STATE_STALE,
    STATE_UNPROBED,
    HealthCache,
    HealthCacheError,
    HealthCacheLockTimeoutError,
    HealthEntry,
    ProbeResult,
    default_cache_path,
)

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
AGENTIC_PROBE_FILE = "/tmp/verdict_agentic_probe.txt"
AGENTIC_PROBE_ORIGINAL = "line one\nline two\nline three\n"
AGENTIC_PROBE_EXPECTED = "line one\nLINE TWO\nline three\n"
DEFAULT_AGENTIC_INTERVAL_HOURS = 24
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


@dataclass
class CycleStats:
    """Counters for one cycle. ``requests`` counts chat and tool calls."""

    requests: int = 0
    probed: int = 0
    fresh: int = 0
    negative: int = 0
    skipped_bucket: int = 0
    stopped_reason: str = ""
    # BOD-297: True when the systemic-auth-outage breaker stopped the cycle
    # (see ``Prober._note_auth_signal``). ``stopped_reason`` is also set to
    # ``"auth_outage"`` in that case; this flag lets a caller branch on it by
    # name instead of matching the reason string.
    auth_outage: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "probed": self.probed,
            "fresh": self.fresh,
            "negative": self.negative,
            "skipped_bucket": self.skipped_bucket,
            "stopped_reason": self.stopped_reason,
            "auth_outage": self.auth_outage,
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


def _pool_of(route: AdmittedRoute) -> str | None:
    return route.pool


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
    candidates = [route for route in chat_routes if route.route_id in kept_id_set]
    candidate_ids = {route.route_id for route in candidates}

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
    ordered.extend((route, "full") for route in _round_robin(free_new))
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


def _round_robin(routes: Sequence[AdmittedRoute]) -> list[AdmittedRoute]:
    """Spread never-probed FREE routes across canonical credential buckets."""
    groups: dict[str, list[AdmittedRoute]] = {}
    for route in routes:
        key = route.pool or route.provider
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
    # BOD-297 systemic-auth-outage breaker: a burst of 401/403 across many
    # pools in one cycle means the gateway key itself is broken, not the
    # routes (the 2026-10-09 controller census lesson). Thresholds are
    # tunable for tests; the defaults match the story's acceptance numbers.
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

    def stop(self) -> None:
        self._stop.set()

    def _save_for_cycle(self, started: float) -> None:
        self.cache.save(
            deadline=started + self.max_wall_seconds, monotonic=self.monotonic, sleep=self.sleep
        )

    def run_once(self) -> CycleStats:
        """Probe until request/wall/lock bounds, returning a typed stop reason."""
        started = self.monotonic()
        stats = CycleStats()
        self._auth_fail_pools = []  # fresh auth-outage window per cycle
        self._auth_fail_buffer = []
        try:
            return self._run_once(started, stats)
        except HealthCacheLockTimeoutError:
            stats.stopped_reason = stats.stopped_reason or "lock_timeout"
            return stats

    def _run_once(self, started: float, stats: CycleStats) -> CycleStats:
        now = self.clock()
        routes = list(self.routes_loader())
        ordered = order_cycle(routes, self.cache, now, epsilon=self.epsilon)
        # Defect 2 fix: resume by route_id set, not by index.
        # If routes reorder or disappear between cycles the index-based cursor
        # would skip unprobed routes; a probed-ids set is order-independent.
        cursor = self.cache.cursor
        probed_ids: set[str] = set()
        if cursor.get("cycle_open"):
            raw_ids = cursor.get("probed_ids") or []
            if isinstance(raw_ids, list):
                probed_ids = set(str(rid) for rid in raw_ids if isinstance(rid, str))
        pending = [item for item in ordered if item[0].route_id not in probed_ids]
        batch_size = self.concurrency

        def over_budget(extra: int = 0) -> str:
            if stats.requests + extra > self.max_requests:
                return "request_cap"
            if self.monotonic() - started >= self.max_wall_seconds:
                return "wall_cap"
            return ""

        while pending:
            reason = over_budget()
            if reason:
                stats.stopped_reason = reason
                self._persist_cursor(probed_ids, open_cycle=True)
                self._save_for_cycle(started)
                break
            batch = pending[:batch_size]
            pending = pending[batch_size:]
            self._persist_cursor(probed_ids, open_cycle=True)
            self._save_for_cycle(started)
            newly_probed = self._run_batch(batch, stats, started)
            probed_ids.update(newly_probed)
            self._persist_cursor(probed_ids, open_cycle=True)
            self._save_for_cycle(started)
            if stats.stopped_reason:
                # Cap fired inside the batch: do not drain remaining pending.
                break
        else:
            stats.stopped_reason = stats.stopped_reason or "complete"
            self.cache.clear_cursor()
            self._save_for_cycle(started)
        # Keep the outage window across both phases; a broken gateway key
        # must not poison routes through agentic probes after chat probing stops.
        if not stats.auth_outage and stats.requests < self.max_requests:
            self.run_agentic_probes(stats, started=started, planned_routes=[r for r, _ in ordered])
        if not stats.auth_outage:
            self._flush_auth_fail_buffer(stats)
            self._save_for_cycle(started)
        return stats

    def _flush_auth_fail_buffer(self, stats: CycleStats) -> None:
        """Write every buffered auth-outage-window negative as a real failure."""
        for buffered_id, buffered_result, buffered_now in self._auth_fail_buffer:
            self.cache.record(buffered_id, buffered_result, buffered_now)
            stats.probed += 1
            stats.negative += 1
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
        self._auth_fail_pools.append(route.pool or route.provider)
        if (
            len(self._auth_fail_pools) >= self.auth_outage_consecutive
            and len(set(self._auth_fail_pools)) >= self.auth_outage_distinct_pools
        ):
            self._auth_fail_buffer = []
            self._auth_fail_pools = []
            stats.auth_outage = True
            stats.stopped_reason = "auth_outage"
        return True

    def _persist_cursor(self, probed_ids: set[str], *, open_cycle: bool) -> None:
        self.cache.set_cursor({"cycle_open": open_cycle, "probed_ids": sorted(probed_ids)})

    def _run_batch(
        self, batch: Sequence[tuple[AdmittedRoute, str]], stats: CycleStats, started: float
    ) -> set[str]:
        """Probe every route in *batch*; return the set of route_ids started.

        Returns only the route_ids actually dispatched.  When a cap fires
        mid-batch the remaining routes are excluded so the caller can update
        the probed-ids cursor correctly (defect 2 fix).
        """
        probed: set[str] = set()
        for route, kind in batch:
            if stats.requests >= self.max_requests:
                stats.stopped_reason = stats.stopped_reason or "request_cap"
                return probed
            if self.monotonic() - started >= self.max_wall_seconds:
                stats.stopped_reason = "wall_cap"
                return probed
            before = stats.requests
            completed = self._probe_route(route, kind, stats, started=started)
            if stats.requests != before:
                self._save_for_cycle(started)
            if not completed:
                # The probe stopped part-way (a cap or bucket fired between the
                # chat and the tool call). The route was left unchanged, so it
                # stays pending and the next cycle resumes with it.
                return probed
            probed.add(route.route_id)
        return probed

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

    def _probe_route(
        self, route: AdmittedRoute, kind: str, stats: CycleStats, *, started: float
    ) -> bool:
        now = self.clock()
        if not self.cache.consume(route.provider, now, pool=route.pool):
            stats.skipped_bucket += 1
            return True  # no token: skipped this cycle, entry unchanged
        stats.requests += 1
        try:
            chat = self.transport(route.route_id, "chat", self.probe_timeout_seconds)
        except TimeoutError:
            chat = ProbeExchange(http_status=None, ok=False, error_category="timeout")
        self._note_rate_limit(route, chat, now)
        chat_ok = bool(chat.ok and chat.chat_exact and chat.http_status == 200)
        tool_ok = False
        latency = chat.latency_ms
        status = chat.http_status
        category = category_for(chat)
        observed = chat

        # Validate chat-phase model identity (defect 3 fix): check right after
        # chat so we do not make the tool call if the gateway returned the wrong
        # model.  An empty reported_model is treated as "matches" (provider
        # chose not to echo).
        if chat_ok and not self._model_identity_matches(route.route_id, chat.reported_model):
            category = CATEGORY_MODEL_MISMATCH
            status = chat.http_status
            chat_ok = False

        if chat_ok and kind == "full" and stats.requests >= self.max_requests:
            # Request cap hit between chat and tool: chat succeeded but tool was
            # never tested. Leave the route unchanged; do NOT record a negative
            # (defect 1 fix: request-cap path).
            stats.stopped_reason = stats.stopped_reason or "request_cap"
            return False
        if chat_ok and kind == "full":
            # Check wall deadline before making the second HTTP call (defect 5).
            if self.monotonic() - started >= self.max_wall_seconds:
                # Wall cap hit between chat and tool: chat succeeded but tool was
                # never tested. Leave the route unchanged (do not record a negative).
                stats.stopped_reason = "wall_cap"
                return False
            now_tool = self.clock()
            if self.cache.consume(route.provider, now_tool, pool=route.pool):
                stats.requests += 1
                try:
                    tool = self.transport(route.route_id, "tool", self.probe_timeout_seconds)
                except TimeoutError:
                    tool = ProbeExchange(http_status=None, ok=False, error_category="timeout")
                self._note_rate_limit(route, tool, now_tool)
                tool_ok = bool(tool.ok and tool.tool_called and tool.http_status == 200)
                if tool.latency_ms is not None:
                    latency = (latency or 0.0) + tool.latency_ms
                # Validate tool-phase model identity too.
                if tool_ok and not self._model_identity_matches(
                    route.route_id, tool.reported_model
                ):
                    tool_ok = False
                    category = CATEGORY_MODEL_MISMATCH
                    status = tool.http_status
                    observed = tool
                elif not tool_ok:
                    status = tool.http_status
                    category = category_for(tool)
                    observed = tool
            else:
                # No bucket token for the tool call: chat succeeded but tool was
                # never tested. Leave the route unchanged; do NOT record a
                # negative (defect 1 fix).
                stats.skipped_bucket += 1
                return True  # no tool token: entry unchanged, retried next cycle
        if chat_ok and (tool_ok or kind == "liveness"):
            category = CATEGORY_OK
            status = 200
        # Defect 5 fix: compute identity from the observed reported_model.
        # An empty reported_model means the gateway did not echo the id back;
        # record it as "not_reported" so status output can distinguish it from
        # a route whose identity was actively verified.
        _reported = observed.reported_model
        if not _reported:
            _identity = "not_reported"
        elif self._model_identity_matches(route.route_id, _reported):
            _identity = "verified"
        else:
            _identity = "mismatch"
        result = ProbeResult(
            category=category,
            chat_ok=chat_ok,
            tool_ok=tool_ok,
            latency_ms=latency,
            http_status=status,
            retry_after_seconds=observed.retry_after_seconds,
            pool=route.pool,
            capacity_evidence=route.capacity_evidence,
            identity=_identity,
        )
        if self._buffer_auth_failure(route, result, now, stats):
            return not stats.auth_outage
        # Liveness success is healthy for the cache state machine but not a
        # coding worker: tool_ok stays false. ``ProbeResult.healthy`` requires
        # tool_ok, so record it as a successful liveness entry directly.
        if chat_ok and kind == "liveness":
            self._record_liveness(route, result, now)
        else:
            self.cache.record(route.route_id, result, now)
        stats.probed += 1
        entry = self.cache.entry(route.route_id)
        if entry is not None and entry.healthy:
            stats.fresh += 1
        elif entry is not None and not entry.healthy:
            stats.negative += 1
        return True

    def _record_liveness(self, route: AdmittedRoute, result: ProbeResult, now: datetime) -> None:
        """Chat-only success: fresh for liveness, but not a coding worker."""
        self.cache.record_liveness(
            route.route_id,
            latency_ms=result.latency_ms,
            pool=route.pool,
            capacity_evidence=route.capacity_evidence,
            now=now,
            identity=result.identity,
        )

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
        planned_routes: Sequence[AdmittedRoute] | None = None,
    ) -> None:
        """Run agentic probes for FREE routes that need them. Bounded.

        Enforces the cycle's wall-time budget and the stop signal before
        each probe and each turn.
        """
        if self.agentic_transport is None or stats.auth_outage:
            return
        wall_start = started if started is not None else self.monotonic()
        now = self.clock()
        if planned_routes is None:
            planned_routes = [
                route
                for route, _kind in order_cycle(
                    self.routes_loader(), self.cache, now, epsilon=self.epsilon
                )
            ]
        routes = [r for r in planned_routes if self._needs_agentic(r, now)]
        probed = 0
        max_agentic = min(8, self.max_requests - stats.requests)
        for route in routes[:max_agentic]:
            # Check stop signal and wall-time before each probe.
            if self._stop.is_set():
                break
            if self.monotonic() - wall_start >= self.max_wall_seconds:
                stats.stopped_reason = stats.stopped_reason or "wall_cap"
                break
            if stats.requests + 3 > self.max_requests:
                break
            now = self.clock()
            # Consume one bucket token before each turn, stop when empty.
            if not self.cache.consume(route.provider, now, pool=route.pool):
                continue
            fake_file = AgenticFakeFile()
            conversation: list[dict[str, Any]] = []
            exchanges: list[ProbeExchange] = []
            budget_exhausted = False
            turn_payloads = [agentic_turn1_payload, agentic_turn2_payload, agentic_turn3_payload]
            try:
                for turn_idx, payload_fn in enumerate(turn_payloads):
                    if turn_idx > 0:
                        # Consume a bucket token before each subsequent turn.
                        now = self.clock()
                        if not self.cache.consume(route.provider, now, pool=route.pool):
                            budget_exhausted = True
                            break
                        # Check stop/wall between turns.
                        if (
                            not exchanges[-1].ok
                            or stats.requests >= self.max_requests
                            or self._stop.is_set()
                            or self.monotonic() - wall_start >= self.max_wall_seconds
                        ):
                            break
                    payload = payload_fn(route.route_id, conversation)
                    ex = self.agentic_transport(route.route_id, payload, self.probe_timeout_seconds)
                    exchanges.append(ex)
                    stats.requests += 1
                    if not ex.ok:
                        break
                    # Simulate tool calls against the fake file and build
                    # conversation history for the next turn.
                    assistant_msg = _extract_assistant_message(ex.response_body)
                    tool_calls = _extract_tool_calls(ex.response_body)
                    tool_results = _simulate_tool_calls(fake_file, tool_calls)
                    # Build cumulative conversation: user + assistant + tool results.
                    conversation = list(payload.get("messages", []))
                    conversation.append(assistant_msg)
                    conversation.extend(tool_results)
            except TimeoutError:
                exchanges.append(
                    ProbeExchange(http_status=None, ok=False, error_category="timeout")
                )
            if budget_exhausted:
                # Record as "not tested" — the route is not failed, just untested.
                stats.probed += 1
                continue
            passed = score_agentic_probe(exchanges)
            # Also verify the fake file reached the expected state.
            if passed:
                passed = fake_file.content == AGENTIC_PROBE_EXPECTED
            # An agentic PASS qualifies the selected route only when every turn
            # echoed that route's model. An absent echo can't prove identity.
            category = CATEGORY_OK if passed else "agentic_fail"
            if passed and not all(
                ex.reported_model
                and self._model_identity_matches(route.route_id, ex.reported_model)
                for ex in exchanges
            ):
                passed = False
                category = CATEGORY_MODEL_MISMATCH
            status = exchanges[-1].http_status if exchanges else None
            if status in (401, 403):
                category = category_for(exchanges[-1])
            result = ProbeResult(
                category=category,
                http_status=status,
                chat_ok=len(exchanges) >= 1 and exchanges[0].ok,
                tool_ok=passed,
                probe_class="agentic",
                agentic_ok=passed,
                pool=route.pool,
                capacity_evidence=route.capacity_evidence,
            )
            if self._buffer_auth_failure(route, result, now, stats):
                if stats.auth_outage:
                    return
                self._save_for_cycle(wall_start)
                continue
            self.cache.record(route.route_id, result, now)
            try:
                self._save_for_cycle(wall_start)
            except HealthCacheLockTimeoutError:
                stats.stopped_reason = (
                    "wall_cap"
                    if self.monotonic() - wall_start >= self.max_wall_seconds
                    else "lock_timeout"
                )
                return
            probed += 1
            stats.probed += 1
            if passed:
                stats.fresh += 1
            else:
                stats.negative += 1
        if self._auth_fail_buffer:
            self._flush_auth_fail_buffer(stats)
            self._save_for_cycle(wall_start)

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


def routes_from_evidence(
    inventory_rows: Sequence[Mapping[str, Any]],
    connections: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
    state_dir: Path | None = None,
) -> tuple[AdmittedRoute, ...]:
    """Admit exactly the way ``verdict eligibility`` does, without probing.

    Uses ``admit`` over the live inventory and connections. Routes that fail
    admission are not probed. Capacity class comes from the same ladder
    classifier eligibility uses, so this prober and ``verdict eligibility``
    agree on FREE versus SUBSCRIPTION.
    """
    from verdict.admission import admit, default_runtime_evidence
    from verdict.orchestration.credential_pools import is_non_chat_route, pool_of

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
        capacity, plan = capacity_class_of(conn, row)
        out.append(
            AdmittedRoute(
                route_id=record.route_id,
                provider=provider,
                capacity=capacity.value,
                # BOD-297: credential-pool identity (alias-collapsed), so
                # order_cycle can probe one canonical route per pool and the
                # census report can group by pool.
                pool=pool_of(record.route_id),
                capacity_evidence=plan or None,
                non_chat=is_non_chat_route(record.route_id, row),
            )
        )
    return tuple(out)


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
    return routes_from_evidence(rows, connections, now=current)


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


def live_agentic_transport(
    base_url: str, *, api_key: str | None
) -> Callable[[str, dict[str, Any], float], ProbeExchange]:
    """OpenAI-compatible transport for agentic probes (arbitrary payloads).

    Built only for a consented live daemon behind ``--allow-live-probe``.
    """
    import urllib.error
    import urllib.request

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
            elapsed = (time.monotonic() - started) * 1000.0
            retry_after = None
            header = exc.headers.get("Retry-After") if exc.headers else None
            if header:
                try:
                    retry_after = float(header)
                except ValueError:
                    retry_after = None
            return ProbeExchange(
                http_status=exc.code,
                ok=False,
                latency_ms=elapsed,
                retry_after_seconds=retry_after,
                error_category=category_for(ProbeExchange(http_status=exc.code, ok=False)),
            )
        except TimeoutError:
            return ProbeExchange(http_status=None, ok=False, error_category="timeout")

    return transport


def live_transport(base_url: str, *, api_key: str | None) -> ProbeTransportFn:
    """OpenAI-compatible transport. Built only for a consented live daemon."""
    import json
    import urllib.error
    import urllib.request

    endpoint = base_url.rstrip("/")
    if endpoint.endswith("/v1"):
        endpoint = endpoint[: -len("/v1")]
    endpoint = endpoint + "/v1/chat/completions"

    def transport(route_id: str, phase: str, timeout_seconds: float) -> ProbeExchange:
        payload = chat_payload(route_id) if phase == "chat" else tool_payload(route_id)
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
                return _exchange_from_body(
                    response.status,
                    parsed if isinstance(parsed, Mapping) else {},
                    phase=phase,
                    latency_ms=elapsed,
                    retry_after=None,
                )
        except urllib.error.HTTPError as exc:
            elapsed = (time.monotonic() - started) * 1000.0
            retry_after = None
            header = exc.headers.get("Retry-After") if exc.headers else None
            if header:
                try:
                    retry_after = float(header)
                except ValueError:
                    retry_after = None
            return ProbeExchange(
                http_status=exc.code,
                ok=False,
                latency_ms=elapsed,
                retry_after_seconds=retry_after,
                error_category=category_for(ProbeExchange(http_status=exc.code, ok=False)),
            )
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
    category: str, http_status: int | None, detail: str, route_id: str = ""
) -> bool:
    """Only a named route/model without account-wide markers is route-scoped."""
    text = detail.strip().lower()
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
) -> ProverDaemon:
    """Construct the daemon. ``state_path`` is accepted and ignored.

    The legacy cycle file (``state_path`` / ``VERDICT_PROVE_AT_REST_STATE``)
    is not read and not written. The daemon writes ``cache_path`` or
    ``VERDICT_HEALTH_CACHE`` or ``~/.verdict/health-cache.json``.
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

    def loader() -> tuple[AdmittedRoute, ...]:
        from verdict.orchestration.run import resolve_api_key

        resolved = key if key is not None else resolve_api_key()
        return load_admitted_routes(origin, api_key=resolved)

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
    )
    return ProverDaemon(prober=prober, consented=allow_live_probe)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def status_report(cache: HealthCache, *, now: datetime | None = None) -> dict[str, Any]:
    """Counts by state and class, top healthy coding workers, cold providers."""
    current = now or _now()
    by_state = {STATE_FRESH: 0, STATE_STALE: 0, STATE_NEGATIVE: 0, STATE_UNPROBED: 0}
    by_class: dict[str, int] = {}
    workers: list[HealthEntry] = []
    providers: dict[str, set[str]] = {}
    for entry in cache.routes().values():
        state = entry.state_at(current)
        by_state[state] = by_state.get(state, 0) + 1
        evidence = entry.capacity_evidence or "unknown"
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
        "counts_by_state": by_state,
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
