"""Manual model-refresh actions: ``models.refresh.plan`` and ``.execute``.

These are the explicit, consent-bound path for metered/unknown routes or wider
coverage than the automatic prepaid refresh. Unit 3 owns and exports the two
handler functions; unit 2 registers them in the shared action registry.

Contract (design section 6):

* ``plan`` is a PURE read: supplied/local inventory/evidence only, zero
  provider GETs, no transport construction, no bucket consumption, no locks,
  markers or cache writes. It returns a digest-bound plan.
* ``execute`` requires ``confirmed=True``, the exact plan with a matching
  digest, an unexpired (120 s) plan and unchanged gateway/selected
  identities/capacity/gates. Any missing/False/invalid/expired/changed plan is
  refused with zero calls and zero writes. All execute paths use the same
  single-flight bounded API and persistence as the automatic refresh.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from verdict.actions.base import ActionResult
from verdict.orchestration.health_cache import (
    CATEGORY_AUTH,
    CATEGORY_PAYMENT,
    CATEGORY_PERMISSION,
    CATEGORY_RATE_LIMITED,
)
from verdict.orchestration.verified_refresh import (
    CAPACITY_FREE,
    CAPACITY_METERED,
    CAPACITY_ORDER,
    CAPACITY_SUBSCRIPTION,
    CAPACITY_UNKNOWN,
    PREPAID_CAPACITY_CLASSES,
    REQUESTS_PER_FULL_PROBE,
    RefreshConfig,
    RefreshConfigError,
    RefreshSnapshot,
    RowInput,
    capacity_group,
)
from verdict.prove_at_rest import chat_payload, tool_payload

# Availability-class negatives (auth / payment / permission / quota) surface as
# UNAVAILABLE in a refresh result; every other genuine negative is a diagnostic
# FAILED (design section 2, rules 2-3).
_UNAVAILABLE_CATEGORIES = frozenset(
    {CATEGORY_AUTH, CATEGORY_PAYMENT, CATEGORY_PERMISSION, CATEGORY_RATE_LIMITED}
)

PLAN_SCHEMA = "verdict.model-refresh-plan/v1"
PLAN_TTL_SECONDS = 120.0
SPEND_NOTE = (
    "SUBSCRIPTION and METERED probes may consume quota; FREE can also have "
    "provider limits; UNKNOWN may incur charges; currency estimate unavailable"
)

# Per-class spend disclosure for the plan's capacity groups.
GROUP_SPEND_NOTES = {
    CAPACITY_FREE: "provider limits may apply",
    CAPACITY_SUBSCRIPTION: "uses prepaid subscription quota",
    CAPACITY_UNKNOWN: "may incur charges",
    CAPACITY_METERED: "billed per token",
}
# Output-token ceiling per probe, taken from the probe payloads themselves.
_MAX_OUTPUT_TOKENS = {
    "full": int(chat_payload("")["max_tokens"]) + int(tool_payload("")["max_tokens"]),
    "liveness": int(chat_payload("")["max_tokens"]),
}
_REQUESTS = {"full": REQUESTS_PER_FULL_PROBE, "liveness": 1}

_USAGE = 2
_REFUSED = 4

# Statuses the manual path may refresh by default (same as the automatic path,
# plus the explicitly consented metered/unknown routes).
_REFRESHABLE_STATUSES = frozenset({"STALE", "UNVERIFIED"})
# Refresh reason for a planned route whose capacity group was not approved.
REASON_NOT_APPROVED = "not_approved"


@dataclass(frozen=True)
class PlanRoute:
    """One chosen route in a refresh plan (no secrets)."""

    route_id: str
    provider: str
    capacity_class: str
    probe_kind: str  # "full" (chat+tool) | "liveness" (chat only)

    def to_dict(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id,
            "provider": self.provider,
            "capacity_class": self.capacity_class,
            "probe_kind": self.probe_kind,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PlanRoute:
        return cls(
            route_id=str(value.get("route_id") or ""),
            provider=str(value.get("provider") or ""),
            capacity_class=str(value.get("capacity_class") or ""),
            probe_kind=str(value.get("probe_kind") or "full"),
        )


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def plan_digest(plan: Mapping[str, Any]) -> str:
    """SHA-256 over the canonical plan body, excluding the digest field itself.

    Binds endpoint, exact ids, filters, caps/kind/capacity, evidence generation
    and dates. It is accidental-change binding, never authentication, and never
    includes a token.
    """
    body = {k: v for k, v in plan.items() if k != "plan_digest"}
    return hashlib.sha256(_canonical_json(body).encode("utf-8")).hexdigest()


def _probe_kind_for(capacity_class: str) -> str:
    """Metered/unknown routes use a chat-only liveness probe; prepaid use full."""
    if capacity_class in {CAPACITY_METERED, CAPACITY_UNKNOWN}:
        return "liveness"
    return "full"


def _rows_from_snapshot(snapshot_rows: Sequence[Mapping[str, Any]]) -> tuple[RowInput, ...]:
    out: list[RowInput] = []
    for raw in snapshot_rows:
        out.append(
            RowInput(
                route_id=str(raw.get("route_id") or ""),
                provider=str(raw.get("provider") or ""),
                status=str(raw.get("status") or ""),
                capacity_class=str(raw.get("capacity_class") or ""),
                refreshable=bool(raw.get("refreshable")),
                refresh_reason=(
                    str(raw["refresh_reason"]) if raw.get("refresh_reason") is not None else None
                ),
                last_success_at=_parse_iso(raw.get("last_success_at")),
                rank_hint=(
                    int(raw["rank_hint"]) if isinstance(raw.get("rank_hint"), int) else None
                ),
                pool=str(raw["pool"]) if isinstance(raw.get("pool"), str) else None,
                capacity_evidence=(
                    str(raw["capacity_evidence"])
                    if isinstance(raw.get("capacity_evidence"), str)
                    else None
                ),
            )
        )
    return tuple(out)


def _price_of(value: Any) -> float | None:
    """A projection row's non-negative price per 1M tokens, else unknown."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        price = float(value)
        if math.isfinite(price) and price >= 0:
            return price
    return None


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _manual_candidates(
    rows: Sequence[RowInput], needed_ids: Sequence[str], *, force: bool, include_metered: bool
) -> list[RowInput]:
    """Candidate rows for the manual path, in capacity-group order.

    By default: the supplied ids whose status is stale/unverified. ``force``
    bypasses the stale/unverified gate (an explicitly disclosed manual force).
    ``include_metered`` lets metered/unknown routes in (they are never
    auto-probed, only shown here after explicit consent). FREE rows come first,
    then SUBSCRIPTION, UNKNOWN and METERED, so the route cap never drops a
    prepaid route in favour of one that may cost money.
    """
    by_id = {row.route_id: row for row in rows}
    wanted = [by_id[rid] for rid in dict.fromkeys(needed_ids) if rid in by_id]
    chosen: list[RowInput] = []
    for row in wanted:
        if not force and row.status not in _REFRESHABLE_STATUSES:
            continue
        if not include_metered and capacity_group(row.capacity_class) not in (
            PREPAID_CAPACITY_CLASSES
        ):
            continue
        chosen.append(row)
    return sorted(chosen, key=lambda row: CAPACITY_ORDER.index(capacity_group(row.capacity_class)))


def _estimated_spend(route_id: str, probe_kind: str, price: float) -> float:
    """Conservative USD estimate of one probe from a per-1M-token price (input + output).

    Tokens are counted as the request bodies' JSON length plus their output
    ``max_tokens``, all charged at the summed input and output price. Provider
    prompt templates can add tokens, so this is an estimate, not a hard cap.
    """
    payloads = [chat_payload(route_id)]
    if probe_kind == "full":
        payloads.append(tool_payload(route_id))
    tokens = sum(len(json.dumps(p)) for p in payloads) + _MAX_OUTPUT_TOKENS[probe_kind]
    return price * tokens / 1_000_000


def _capacity_groups(
    routes: Sequence[PlanRoute], prices: Mapping[str, float | None]
) -> list[dict[str, Any]]:
    """One group per capacity class present, in consent order, each confirmed separately.

    FREE and SUBSCRIPTION default to yes; UNKNOWN and METERED default to no and
    disclose an estimated spend in USD from inventory pricing. A route without
    pricing is counted as ``unpriced``: its cost cannot be estimated.
    """
    groups: list[dict[str, Any]] = []
    for capacity in CAPACITY_ORDER:
        members = [r for r in routes if capacity_group(r.capacity_class) == capacity]
        if not members:
            continue
        group: dict[str, Any] = {
            "capacity_class": capacity,
            "route_ids": [r.route_id for r in members],
            "estimated_requests": sum(_REQUESTS[r.probe_kind] for r in members),
            "default_approved": capacity in PREPAID_CAPACITY_CLASSES,
            "spend_note": GROUP_SPEND_NOTES[capacity],
        }
        if capacity not in PREPAID_CAPACITY_CLASSES:
            priced = [(r, prices.get(r.route_id)) for r in members]
            known = [
                _estimated_spend(r.route_id, r.probe_kind, p) for r, p in priced if p is not None
            ]
            group["unpriced_count"] = len(members) - len(known)
            # Round up so a tiny estimate never displays as zero.
            group["estimated_spend_usd"] = (
                math.ceil(sum(known) * 10_000) / 10_000 if known else None
            )
        groups.append(group)
    return groups


def _build_config(caps: Mapping[str, Any] | None) -> RefreshConfig:
    caps = caps or {}
    return RefreshConfig(
        max_routes=int(caps.get("max_routes", RefreshConfig.max_routes)),
        max_requests=int(caps.get("max_requests", RefreshConfig.max_requests)),
        wall_seconds=float(caps.get("wall_seconds", RefreshConfig.wall_seconds)),
        concurrency=int(caps.get("concurrency", RefreshConfig.concurrency)),
        auto_refresh=True,
    )


# ---------------------------------------------------------------------------
# models.refresh.plan  (PURE read; zero I/O beyond the supplied snapshot)
# ---------------------------------------------------------------------------


def action_models_refresh_plan(**kwargs: Any) -> ActionResult:
    """Build a digest-bound refresh plan from the supplied snapshot. No I/O.

    Required kwargs:
      * ``snapshot_rows``: the shown/filtered projection rows (list of dicts).
      * ``needed_ids``: the exact ids to consider (shown page or wider list).
    Optional kwargs: ``consumer``, ``gateway_origin``, ``evidence_generation``,
    ``filters``, ``page``, ``caps``, ``now`` (datetime), ``force`` (bool),
    ``include_metered`` (bool).
    """
    snapshot_rows = kwargs.get("snapshot_rows")
    needed_ids = kwargs.get("needed_ids")
    if not isinstance(snapshot_rows, Sequence) or not isinstance(needed_ids, Sequence):
        return ActionResult(
            data={"error": "plan requires snapshot_rows and needed_ids"}, ok=False, exit_code=_USAGE
        )
    now = kwargs.get("now")
    if not isinstance(now, datetime):
        now = datetime.now(timezone.utc)
    try:
        config = _build_config(kwargs.get("caps"))
    except (RefreshConfigError, ValueError) as exc:
        return ActionResult(data={"error": str(exc)}, ok=False, exit_code=_USAGE)

    raw_rows = [dict(r) for r in snapshot_rows]
    rows = _rows_from_snapshot(raw_rows)
    prices = {str(raw.get("route_id") or ""): _price_of(raw.get("price")) for raw in raw_rows}
    force = bool(kwargs.get("force"))
    include_metered = bool(kwargs.get("include_metered"))
    candidate_rows = _manual_candidates(
        rows, [str(i) for i in needed_ids], force=force, include_metered=include_metered
    )
    candidate_count = len(candidate_rows)
    chosen = candidate_rows[: config.max_routes]
    plan_routes = [
        PlanRoute(
            route_id=row.route_id,
            provider=row.provider,
            capacity_class=row.capacity_class,
            probe_kind=_probe_kind_for(row.capacity_class),
        )
        for row in chosen
    ]
    estimated_requests = REQUESTS_PER_FULL_PROBE * sum(
        1 for r in plan_routes if r.probe_kind == "full"
    ) + sum(1 for r in plan_routes if r.probe_kind == "liveness")

    created = now
    body: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "consumer": str(kwargs.get("consumer") or "manual"),
        "gateway_origin": str(kwargs.get("gateway_origin") or ""),
        "evidence_generation": str(kwargs.get("evidence_generation") or ""),
        "created_at": _iso(created),
        "valid_until": _iso(created + timedelta(seconds=PLAN_TTL_SECONDS)),
        "filters": dict(kwargs.get("filters") or {}),
        "page": kwargs.get("page"),
        "needed_ids": [str(i) for i in needed_ids],
        "candidate_count": candidate_count,
        "routes": [r.to_dict() for r in plan_routes],
        "selected_count": len(plan_routes),
        "omitted_count": max(0, candidate_count - len(plan_routes)),
        "caps": {
            "max_routes": config.max_routes,
            "max_requests": config.max_requests,
            "wall_seconds": config.wall_seconds,
            "concurrency": config.concurrency,
        },
        "estimated_requests": estimated_requests,
        "capacity_groups": _capacity_groups(plan_routes, prices),
        "spend_note": SPEND_NOTE,
        "requires_confirmation": True,
        "force": force,
        "include_metered": include_metered,
    }
    body["plan_digest"] = plan_digest(body)
    return ActionResult(data=body, ok=True, exit_code=0)


# ---------------------------------------------------------------------------
# models.refresh.execute  (mutation; requires confirmed=True + matching plan)
# ---------------------------------------------------------------------------


def _safe_code(value: object) -> str | None:
    """Expose only bounded machine codes, never free-form provider text."""
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    if not all(c.isascii() and (c.islower() or c.isdigit() or c == "_") for c in value):
        return None
    return value


def _route_results(
    plan_routes: Sequence[PlanRoute],
    snapshot: RefreshSnapshot,
    outcome: Any,
    declined: Collection[str] = (),
) -> dict[str, dict[str, Any]]:
    """Report every exact planned id, including narrowed, declined or untested ids."""
    current = snapshot.by_id()
    results: dict[str, dict[str, Any]] = {}
    for route in plan_routes:
        actual = outcome.route_outcomes.get(route.route_id)
        row = current.get(route.route_id)
        probed = bool(actual is not None and actual.probed)
        category = _safe_code(actual.category) if actual is not None else None
        reason = _safe_code(actual.refresh_reason) if actual is not None else None
        if route.route_id in declined:
            reason = REASON_NOT_APPROVED
        if not probed and reason is None and row is not None:
            reason = _safe_code(row.refresh_reason)
        status = row.status if row is not None else "UNAVAILABLE"
        if status not in {"STALE", "UNVERIFIED", "FAILED", "UNAVAILABLE", "VERIFIED"}:
            status = "UNAVAILABLE"
        alive = bool(actual is not None and actual.alive)
        if probed and actual is not None:
            # Full coding proof mints VERIFIED. A chat-only liveness success is
            # alive but NOT coding-verified: it keeps the prior display status
            # unchanged when that prior is already VERIFIED/STALE/UNVERIFIED --
            # it never upgrades to VERIFIED and never demotes an existing fresh
            # VERIFIED. Only when the prior was FAILED/UNAVAILABLE/missing does
            # the liveness success contradict that negative; it then becomes
            # UNVERIFIED (proof of life, but no tool proof). A genuine negative
            # demotes -- availability-class categories to UNAVAILABLE, every
            # other diagnostic negative to FAILED.
            if actual.verified:
                status = "VERIFIED"
            elif alive:
                if status not in {"VERIFIED", "STALE", "UNVERIFIED"}:
                    # alive-only: keep VERIFIED/STALE/UNVERIFIED prior; a
                    # contradicted FAILED/UNAVAILABLE or missing prior becomes
                    # UNVERIFIED (proof of life, but no tool proof).
                    status = "UNVERIFIED"
            elif actual.category in _UNAVAILABLE_CATEGORIES:
                status = "UNAVAILABLE"
            else:
                status = "FAILED"
        elif not probed and reason in {"wall_cap", "not_tested", "cancelled"}:
            # A no-write / untested / deadline outcome never establishes a new
            # result: keep the safe prior status, demote only a stale VERIFIED.
            if status == "VERIFIED":
                status = "STALE"
        elif status == "VERIFIED":
            # An unprobed refresh cannot establish a new VERIFIED result.
            status = "STALE"
        results[route.route_id] = {
            "status_after": status,
            "probed": probed,
            "alive": alive,
            "category": category,
            "http_status": (
                actual.http_status
                if actual is not None
                and type(actual.http_status) is int
                and 100 <= actual.http_status <= 599
                else None
            ),
            "refresh_reason": reason or ("not_tested" if not probed else None),
            "requests_made": (
                actual.requests_made
                if actual is not None
                and type(actual.requests_made) is int
                and 0 <= actual.requests_made <= 2
                else 0
            ),
        }
    return results


def _refuse(reason: str) -> ActionResult:
    return ActionResult(
        data={"error": "refresh execute refused", "reason": reason, "calls": 0, "writes": 0},
        ok=False,
        exit_code=_REFUSED,
    )


def action_models_refresh_execute(**kwargs: Any) -> ActionResult:
    """Execute a confirmed refresh plan through the single-flight bounded API.

    Required kwargs:
      * ``confirmed``: must be literally True.
      * ``plan``: the exact plan dict returned by ``plan`` (unchanged).
      * ``snapshot``: a :class:`RefreshSnapshot` of CURRENT evidence (rows).
      * ``cache``: a :class:`HealthCache`.
    Optional kwargs: ``transport``, ``clock``, ``monotonic``, ``sleep``,
    ``on_progress``, ``cancel``, ``lock_path``, ``marker_path``, ``now``, and
    ``approved_classes``: the exact capacity classes the operator approved
    (default FREE and SUBSCRIPTION). It only narrows the plan; declined routes
    report ``not_approved`` and an unrecognised class refuses execution.

    No live transport is constructed before confirmation; all gating runs with
    zero calls/writes until the plan validates.
    """
    if kwargs.get("confirmed") is not True:
        return _refuse("not_confirmed")
    plan = kwargs.get("plan")
    if not isinstance(plan, Mapping):
        return _refuse("missing_plan")
    if plan.get("schema") != PLAN_SCHEMA:
        return _refuse("wrong_schema")
    supplied_digest = plan.get("plan_digest")
    if not isinstance(supplied_digest, str) or supplied_digest != plan_digest(plan):
        return _refuse("digest_mismatch")

    now = kwargs.get("now")
    if not isinstance(now, datetime):
        now = datetime.now(timezone.utc)
    valid_until = _parse_iso(plan.get("valid_until"))
    if valid_until is None or now > valid_until:
        return _refuse("plan_expired")

    snapshot = kwargs.get("snapshot")
    if not isinstance(snapshot, RefreshSnapshot):
        return _refuse("missing_snapshot")

    plan_routes = [
        PlanRoute.from_dict(r) for r in (plan.get("routes") or []) if isinstance(r, Mapping)
    ]
    if not plan_routes:
        return _refuse("empty_plan")

    # Capacity groups the operator approved. Omitted means the prepaid groups
    # only: UNKNOWN and METERED run only when named explicitly.
    approved_raw = kwargs.get("approved_classes", PREPAID_CAPACITY_CLASSES)
    if (
        isinstance(approved_raw, (str, bytes))
        or not isinstance(approved_raw, Collection)
        or not all(isinstance(c, str) and c in CAPACITY_ORDER for c in approved_raw)
    ):
        return _refuse("invalid_approved_classes")
    approved = set(approved_raw)

    # Re-read gates: identities/capacity must be unchanged. Narrowing (a new
    # blocker or a declined capacity group) may skip candidates but must never
    # expand or raise spend.
    current = snapshot.by_id()
    executable_ids: list[str] = []
    declined: set[str] = set()
    for pr in plan_routes:
        row = current.get(pr.route_id)
        if row is None:
            continue  # route disappeared: narrow, never expand
        if row.provider != pr.provider or row.capacity_class != pr.capacity_class:
            return _refuse("identity_changed")
        if capacity_group(pr.capacity_class) not in approved:
            declined.add(pr.route_id)
            continue
        executable_ids.append(pr.route_id)

    # Re-read current generation and gateway endpoint; refuse a missing or
    # mismatched binding before ANY call (design s6:141-143). The plan digest
    # binds endpoint + generation, so a plan with a generation but a snapshot
    # with none (or a different one) must NOT silently run: that is exactly the
    # "stale/ambiguous evidence" case the binding exists to catch.
    plan_gen = str(plan.get("evidence_generation") or "")
    current_gen = str(snapshot.generation or "")
    if not plan_gen.strip() or not current_gen.strip() or plan_gen != current_gen:
        return _refuse("evidence_changed")

    plan_gateway = str(plan.get("gateway_origin") or "")
    current_gateway = str(snapshot.gateway_origin or "")
    if not plan_gateway.strip() or not current_gateway.strip() or plan_gateway != current_gateway:
        return _refuse("gateway_changed")

    if not executable_ids:
        # Everything narrowed away: nothing to probe, zero calls.
        return ActionResult(
            data={
                "schema": "verdict.model-refresh-result/v1",
                "outcome": "nothing_eligible",
                "requests_made": 0,
                "probed": 0,
                "route_outcomes": {
                    pr.route_id: {
                        "status_after": (
                            current[pr.route_id].status
                            if pr.route_id in declined
                            else "UNAVAILABLE"
                        ),
                        "probed": False,
                        "category": None,
                        "http_status": None,
                        "refresh_reason": (
                            REASON_NOT_APPROVED if pr.route_id in declined else "not_tested"
                        ),
                        "requests_made": 0,
                    }
                    for pr in plan_routes
                },
            },
            ok=True,
            exit_code=0,
        )

    # Build the bounded config from the plan's own caps (never widened here).
    try:
        config = _build_config(plan.get("caps"))
    except (RefreshConfigError, ValueError) as exc:
        return _refuse(f"invalid_caps:{exc}")

    # Local imports keep plan a pure read with no coordinator/cache dependency.
    from verdict.orchestration.verified_refresh import RefreshCoordinator

    cache = kwargs.get("cache")
    if cache is None:
        return _refuse("missing_cache")

    coordinator = RefreshCoordinator(
        cache=cache,
        transport=kwargs.get("transport"),
        clock=kwargs.get("clock") or (lambda: datetime.now(timezone.utc)),
        monotonic=kwargs.get("monotonic") or time.monotonic,
        sleep=kwargs.get("sleep") or time.sleep,
        lock_path=kwargs.get("lock_path"),
        marker_path=kwargs.get("marker_path"),
    )
    outcome = coordinator.refresh_for_consumer(
        snapshot,
        consumer=str(plan.get("consumer") or "manual"),
        needed_ids=executable_ids,
        config=config,
        # A confirmed plan is a separately typed consent authorization: it is
        # NOT an automatic trigger (so VERDICT_AUTO_REFRESH does not gate it)
        # and it lifts the prepaid-only rule so its exact METERED/UNKNOWN ids
        # DO execute (chat-only liveness). ``explicit`` still marks the ids as
        # top priority within the plan.
        explicit=True,
        authorized=True,
        on_progress=kwargs.get("on_progress"),
        cancel=kwargs.get("cancel"),
    )
    # Hard invariant: a confirmed job never exceeds 2 requests per selected route.
    assert outcome.requests_made <= REQUESTS_PER_FULL_PROBE * len(executable_ids)
    return ActionResult(
        data={
            "schema": "verdict.model-refresh-result/v1",
            "outcome": outcome.outcome,
            "job_id": outcome.job_id,
            "probed": outcome.probed,
            "verified": outcome.verified,
            "alive": outcome.alive,
            "failed": outcome.failed,
            "unavailable": outcome.unavailable,
            "requests_made": outcome.requests_made,
            "complete": outcome.complete,
            "cap_reason": outcome.cap_reason,
            "approved_classes": [c for c in CAPACITY_ORDER if c in approved],
            "route_outcomes": _route_results(plan_routes, snapshot, outcome, declined),
        },
        ok=True,
        exit_code=0,
    )


__all__ = [
    "GROUP_SPEND_NOTES",
    "PLAN_SCHEMA",
    "PLAN_TTL_SECONDS",
    "REASON_NOT_APPROVED",
    "SPEND_NOTE",
    "PlanRoute",
    "action_models_refresh_execute",
    "action_models_refresh_plan",
    "plan_digest",
]
