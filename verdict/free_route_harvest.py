"""KEEP harvest: free/concrete/compatible ids, then wait, then pick best LIVE."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from verdict.availability import is_opaque_route_id
from verdict.probes import ProbeBudget, ProbePolicy, ProbeRunner, ProbeTransport


@dataclass(frozen=True)
class TaskNeed:
    chat: bool = False
    min_context: int | None = None


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


def _pool_alias(model_id: str) -> bool:
    return model_id.rsplit("/", 1)[-1].lower() == "free"


def _chat_ok(row: Mapping[str, Any], need: TaskNeed) -> bool:
    if not need.chat:
        return True
    caps = row.get("capabilities")
    if not isinstance(caps, Mapping):
        return True  # omit → fail-open
    return bool(caps.get("chat"))


def _context_ok(row: Mapping[str, Any], need: TaskNeed) -> bool:
    if need.min_context is None:
        return True
    raw = row.get("context_length")
    if raw is None:
        return True  # omit → fail-open
    try:
        return int(raw) >= need.min_context
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


def keep_free_compatible(rows: Sequence[Mapping[str, Any]], need: TaskNeed) -> list[str]:
    kept: list[str] = []
    for row in rows:
        model_id = str(row.get("id") or "").strip()
        if not model_id:
            continue
        if is_opaque_route_id(model_id) or _pool_alias(model_id):
            continue
        if str(row.get("owned_by") or "").strip().lower() == "combo":
            continue
        if _positive_price(row.get("pricing")):
            continue
        if not _chat_ok(row, need) or not _context_ok(row, need):
            continue
        kept.append(model_id)
    return kept


def first_execute_need(*, catalog_n: int, keep_n: int) -> int:
    if catalog_n <= 0 or keep_n <= 0:
        return 0
    want = max(math.ceil(catalog_n * 0.10), math.ceil(keep_n * 0.25))
    # ponytail: floor 3 so a 10-model catalog does not fire after 1 LIVE
    return min(keep_n, max(want, 3))


def pick_best_live(observations: Sequence[Mapping[str, Any]]) -> str | None:
    best_id: str | None = None
    best_ms = math.inf
    for row in observations:
        model_id = str(row.get("model_id") or "")
        if ":free" not in model_id.lower():
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
