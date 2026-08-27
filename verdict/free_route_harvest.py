"""KEEP harvest: free/concrete/compatible ids, then wait, then pick best LIVE."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from verdict.availability import is_opaque_route_id
from verdict.headroom import affordability
from verdict.probes import ProbeBudget, ProbePolicy, ProbeRunner, ProbeTransport


@dataclass(frozen=True)
class TaskNeed:
    """Capability predicates derived from one work unit (FR-030)."""

    chat: bool = False
    min_context: int | None = None
    tools: bool = False
    modality: str | None = None
    token_budget: int | None = None

    @property
    def context_floor(self) -> int | None:
        """Largest stated context requirement: explicit floor or the unit's token budget."""
        floors = [value for value in (self.min_context, self.token_budget) if value]
        return max(floors) if floors else None

    def required_capabilities(self) -> tuple[str, ...]:
        names = []
        if self.chat:
            names.append("chat")
        if self.tools:
            names.append("tools")
        if self.modality:
            names.append(self.modality)
        return tuple(names)


MODALITIES = frozenset({"chat", "completion", "embedding", "vision", "audio", "image"})

# Live catalogs describe text generation by omission: they enumerate extras
# (tools/vision/reasoning) and never state `chat`. Only a row that positively
# declares a different modality can be treated as stating chat's absence.
NON_CHAT_MODALITIES = frozenset({"embedding", "image", "audio"})


def _positive_price(pricing: object) -> bool:
    if not isinstance(pricing, Mapping):
        return False
    for value in pricing.values():
        try:
            if float(value) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def free_status(row: Mapping[str, Any], free_ids: Collection[str] | None = None) -> str:
    """Free-ness from an adapter facet or the id, never from a missing price (FR-036).

    Live catalogs omit ``pricing`` on every row, so its absence carries no information.
    ``free_ids`` is whatever the negotiated gateway adapter positively declared free; a
    gateway that declares nothing leaves every unmarked id ``UNKNOWN``.
    """
    model_id = str(row.get("id") or "")
    if free_ids is not None and model_id in free_ids:
        return "free"
    if _positive_price(row.get("pricing")):
        return "paid"
    if ":free" in model_id.lower():
        return "free"
    return "UNKNOWN"


def _pool_alias(model_id: str) -> bool:
    """FR-035: 'free' occupying any path segment of the identifier counts as an alias.

    Prior check only rejected an id whose entire leaf equaled 'free', missing an id
    like 'openrouter/pool/free/v2' whose alias segment is not the leaf. A non-terminal
    segment equal to 'free' is always an alias. The terminal segment is compared whole,
    with its ``:tier`` suffix intact: a real model may itself be named 'free' and carry
    a ':free' tier suffix ('slow/free:free'), which is concrete, not the resolver alias
    'openrouter/free'.
    """
    segments = model_id.lower().split("/")
    return "free" in segments[:-1] or segments[-1] == "free"


def _capabilities_ok(row: Mapping[str, Any], need: TaskNeed) -> bool:
    """Stated absence of a required capability excludes; an omitted one fails open.

    A capabilities map that names any modality enumerates them exhaustively, so a
    required modality missing from it is a stated absence. Feature flags such as
    tool calling are not enumerated that way and fail open when unstated.
    """
    caps = row.get("capabilities")
    if not isinstance(caps, Mapping):
        return True  # omit → fail-open
    for name in need.required_capabilities():
        stated = caps.get(name)
        if stated is None:
            if name == "chat" and any(caps.get(other) for other in NON_CHAT_MODALITIES):
                return False  # positively declares a different modality
            continue  # unstated → fail-open
        if not stated:
            return False
    return True


def _context_ok(row: Mapping[str, Any], need: TaskNeed) -> bool:
    floor = need.context_floor
    if floor is None:
        return True
    raw = row.get("context_length")
    if raw is None:
        return True  # omit → fail-open
    try:
        return int(raw) >= floor
    except (TypeError, ValueError):
        return True


def catalog_rows_from_payload(payload: object) -> list[Mapping[str, Any]]:
    raw: object
    if isinstance(payload, Mapping):
        raw = payload.get("data", payload.get("models", payload.get("items", [])))
    else:
        raw = payload
    if not isinstance(raw, list):
        return []
    return [row for row in raw if isinstance(row, Mapping) and isinstance(row.get("id"), str)]


def keep_free_compatible(
    rows: Sequence[Mapping[str, Any]],
    need: TaskNeed,
    free_ids: Collection[str] | None = None,
    remaining_tokens: Mapping[str, int] | None = None,
) -> list[str]:
    """Kept identities, positively-free first then UNKNOWN-cost (FR-036).

    UNKNOWN stays kept and probe-eligible — a gateway that declares nothing about price
    must not empty the catalog — but it never outranks an identity known to be free.

    ``remaining_tokens`` is whatever capacity the gateway actually disclosed. An identity
    with observed capacity below the unit's estimated cost is excluded (FR-029); an
    identity with no observation stays eligible and is recorded ``UNKNOWN``.
    """
    free_first: list[str] = []
    unknown: list[str] = []
    for row in rows:
        model_id = str(row.get("id") or "").strip()
        if not model_id:
            continue
        if is_opaque_route_id(model_id) or _pool_alias(model_id):
            continue
        if str(row.get("owned_by") or "").strip().lower() == "combo":
            continue
        status = free_status(row, free_ids)
        if status == "paid":
            continue
        if not _capabilities_ok(row, need) or not _context_ok(row, need):
            continue
        if (
            remaining_tokens is not None
            and not affordability(
                estimated_tokens=need.token_budget, remaining_tokens=remaining_tokens.get(model_id)
            ).admitted
        ):
            continue
        (free_first if status == "free" else unknown).append(model_id)
    return free_first + unknown


def first_execute_need(*, catalog_n: int, keep_n: int) -> int:
    if catalog_n <= 0 or keep_n <= 0:
        return 0
    want = max(math.ceil(catalog_n * 0.10), math.ceil(keep_n * 0.25))
    # ponytail: floor 3 so a 10-model catalog does not fire after 1 LIVE
    return min(keep_n, max(want, 3))


def pick_best_live(
    observations: Sequence[Mapping[str, Any]], free_ids: Collection[str] | None = None
) -> str | None:
    """Lowest-latency ready identity that is free, by id shape or adapter declaration.

    ``free_ids`` lets a gateway-negotiated facet (FR-034/FR-038) recognize a free
    identity that carries no ``:free`` suffix; without it, only the id shape is used.
    """
    best_id: str | None = None
    best_ms = math.inf
    for row in observations:
        model_id = str(row.get("model_id") or "")
        is_free = ":free" in model_id.lower() or (free_ids is not None and model_id in free_ids)
        if not is_free:
            continue
        if str(row.get("availability_state") or "").lower() != "ready":
            continue
        raw_ms = row.get("latency_ms")
        if raw_ms is None:
            continue
        try:
            latency = float(raw_ms)
        except (TypeError, ValueError):
            continue
        if latency < best_ms:
            best_ms = latency
            best_id = model_id
    return best_id


def _obs_maps(observations: Sequence[Any]) -> list[dict[str, Any]]:
    return [
        {
            "model_id": item.model_id,
            "availability_state": item.availability_state,
            "latency_ms": item.latency_ms,
        }
        for item in observations
    ]


def _probe_ids(model_ids: Sequence[str], transport: ProbeTransport) -> list[Any]:
    n = max(len(model_ids), 1)
    duration = max(120.0, float(n) * 15.0)
    return list(
        ProbeRunner(
            ProbePolicy(max_models_per_run=n, timeout_seconds=15.0, max_duration_seconds=duration)
        ).run(
            list(model_ids),
            transport,
            provider="harvest",
            budget=ProbeBudget(
                provider="harvest", max_requests=n, max_tokens=n, max_duration_seconds=duration
            ),
        )
    )


def drain_keep_probes(pending: Sequence[str], transport: ProbeTransport) -> None:
    if pending:
        _probe_ids(pending, transport)


def harvest_live_route(
    rows: Sequence[Mapping[str, Any]], transport: ProbeTransport, need: TaskNeed | None = None
) -> dict[str, Any]:
    """Filter KEEP, probe until need + LIVE pick, leave remainder for drain."""
    task = need or TaskNeed(chat=True)
    keep = keep_free_compatible(rows, task)
    if not keep:
        return {}
    n = len(keep)
    need_n = first_execute_need(catalog_n=len(rows), keep_n=n)
    observations: list[Any] = []
    pending = list(keep)
    while pending:
        picked = pick_best_live(_obs_maps(observations)) if observations else None
        if len(observations) >= need_n and picked:
            break
        nxt = pending.pop(0)
        observations.extend(_probe_ids([nxt], transport))
    picked = pick_best_live(_obs_maps(observations))
    if picked is None:
        return {}
    blob = json.dumps(
        {
            "keep": keep,
            "need": need_n,
            "picked": picked,
            "probed": [item.model_id for item in observations],
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    digest = "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()
    candidate_routes = []
    for item in observations:
        ready = item.availability_state == "ready"
        state = (
            "eligible"
            if ready
            else ("rate_limited" if getattr(item, "http_status", None) == 429 else "unavailable")
        )
        candidate_routes.append(
            {
                "requested_identity": item.model_id,
                "actual_identity": item.model_id,
                "admitted": ready,
                "availability_state": state,
                "evidence_digest": digest,
            }
        )
    return {
        "requested_identity": picked,
        "actual_identity": picked,
        "admitted": True,
        "evidence_digest": digest,
        "keep_n": n,
        "probed_n": len(observations),
        "first_execute_need": need_n,
        "pending_keep": pending,
        "candidate_routes": candidate_routes,
    }
