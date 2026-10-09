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
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from verdict.actions.base import ActionResult
from verdict.orchestration.verified_refresh import (
    CAPACITY_METERED,
    CAPACITY_UNKNOWN,
    REQUESTS_PER_FULL_PROBE,
    RefreshConfig,
    RefreshConfigError,
    RefreshSnapshot,
    RowInput,
)

PLAN_SCHEMA = "verdict.model-refresh-plan/v1"
PLAN_TTL_SECONDS = 120.0
SPEND_NOTE = (
    "SUBSCRIPTION and METERED probes may consume quota; FREE can also have "
    "provider limits; UNKNOWN may incur charges; currency estimate unavailable"
)

_USAGE = 2
_REFUSED = 4

# Statuses the manual path may refresh by default (same as the automatic path,
# plus the explicitly consented metered/unknown routes).
_REFRESHABLE_STATUSES = frozenset({"STALE", "UNVERIFIED"})


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
    """Candidate rows for the manual path.

    By default: the supplied ids whose status is stale/unverified. ``force``
    bypasses the stale/unverified gate (an explicitly disclosed manual force).
    ``include_metered`` lets metered/unknown routes in (they are never
    auto-probed, only shown here after explicit consent).
    """
    by_id = {row.route_id: row for row in rows}
    wanted = [by_id[rid] for rid in dict.fromkeys(needed_ids) if rid in by_id]
    chosen: list[RowInput] = []
    for row in wanted:
        if not force and row.status not in _REFRESHABLE_STATUSES:
            continue
        if not include_metered and row.capacity_class in {CAPACITY_METERED, CAPACITY_UNKNOWN}:
            continue
        chosen.append(row)
    return chosen


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

    rows = _rows_from_snapshot([dict(r) for r in snapshot_rows])
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
    plan_routes: Sequence[PlanRoute], snapshot: RefreshSnapshot, outcome: Any
) -> dict[str, dict[str, Any]]:
    """Report every exact planned id, including narrowed or untested ids."""
    current = snapshot.by_id()
    results: dict[str, dict[str, Any]] = {}
    for route in plan_routes:
        actual = outcome.route_outcomes.get(route.route_id)
        row = current.get(route.route_id)
        probed = bool(actual is not None and actual.probed)
        category = _safe_code(actual.category) if actual is not None else None
        reason = _safe_code(actual.refresh_reason) if actual is not None else None
        if not probed and reason is None and row is not None:
            reason = _safe_code(row.refresh_reason)
        status = row.status if row is not None else "UNAVAILABLE"
        if status not in {"STALE", "UNVERIFIED", "FAILED", "UNAVAILABLE", "VERIFIED"}:
            status = "UNAVAILABLE"
        if probed:
            status = "VERIFIED" if actual.verified else "FAILED"
        elif status == "VERIFIED":
            # An unprobed refresh cannot establish a new VERIFIED result.
            status = "STALE"
        results[route.route_id] = {
            "status_after": status,
            "probed": probed,
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
    ``on_progress``, ``cancel``, ``lock_path``, ``marker_path``, ``now``.

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

    # Re-read gates: identities/capacity must be unchanged. Narrowing (a new
    # blocker) may skip candidates but must never expand or raise spend.
    current = snapshot.by_id()
    executable_ids: list[str] = []
    for pr in plan_routes:
        row = current.get(pr.route_id)
        if row is None:
            continue  # route disappeared: narrow, never expand
        if row.provider != pr.provider or row.capacity_class != pr.capacity_class:
            return _refuse("identity_changed")
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
    if (
        not plan_gateway.strip()
        or not current_gateway.strip()
        or plan_gateway != current_gateway
    ):
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
                        "status_after": "UNAVAILABLE",
                        "probed": False,
                        "category": None,
                        "http_status": None,
                        "refresh_reason": "not_tested",
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
            "failed": outcome.failed,
            "unavailable": outcome.unavailable,
            "requests_made": outcome.requests_made,
            "complete": outcome.complete,
            "cap_reason": outcome.cap_reason,
            "route_outcomes": _route_results(plan_routes, snapshot, outcome),
        },
        ok=True,
        exit_code=0,
    )


__all__ = [
    "PLAN_SCHEMA",
    "PLAN_TTL_SECONDS",
    "SPEND_NOTE",
    "PlanRoute",
    "action_models_refresh_execute",
    "action_models_refresh_plan",
    "plan_digest",
]
