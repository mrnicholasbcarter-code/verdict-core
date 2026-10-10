"""Pure EligibilityLadder for orchestration model selection."""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from verdict.orchestration.contracts import (
    CapacityClass,
    EligibilityStage,
    FailureClassification,
    RouteVerdict,
    TaskRequirements,
    route_family,
)
from verdict.orchestration.credential_pools import pool_of
from verdict.orchestration.provider_catalog import (
    CATALOG_STALE_COOLDOWN_SECONDS,
    aliased_pools_for,
    backend_pool,
    connection_signals_free,
    has_free_suffix,
    is_catalog_stale_error,
    is_not_free_signal,
    record_not_free_override,
    resolve_provider,
    sanitized_plan_label,
)
from verdict.subagent_selection import HealthResult

if TYPE_CHECKING:
    from verdict.admission import AdmittedSet
    from verdict.orchestration.session_evidence import SessionLedger, SessionStats

_OPAQUE_PREFIXES = ("auto/", "combo/", "router/", "virtual/")
# Capacity that costs nothing extra per call (already paid for or free).
_PREPAID_CAPACITY = frozenset({CapacityClass.SUBSCRIPTION, CapacityClass.FREE})
# Ranks an unknown-capability route after every known one (tiers span 0..3).
_UNKNOWN_SLACK = 4
_EFFORT_SUFFIXES = ("-low", "-medium", "-high", "-xhigh", "-max", "-ultra")
_CODING_MARKERS = ("code", "codex", "sonnet", "fable", "opus")
# All roles rank free capacity first after the capability/admission gates.
_CAPACITY_ORDER: Mapping[CapacityClass, int] = {
    CapacityClass.FREE: 0,
    CapacityClass.SUBSCRIPTION: 1,
    CapacityClass.METERED: 2,
    CapacityClass.UNKNOWN: 3,
}
_WORKER_CAPACITY_ORDER = _CAPACITY_ORDER
ENV_ALLOW_UNKNOWN = "VERDICT_ALLOW_UNKNOWN_CAPACITY"
_CATEGORY_COOLDOWN_SECONDS: Mapping[str, float] = {
    "rate_limited": 60.0,
    "quota_exhausted": 3600.0,
    "authentication": 3600.0,
    "payment_required": 3600.0,
    "permission": 3600.0,
    "unsupported": 86400.0,
    "unservable": 21600.0,
    "timeout": 60.0,
    "upstream_temporary": 60.0,
    "transport_temporary": 60.0,
    # Catalog ghosts: ids in /v1/models but not in the provider's live catalog.
    # Long TTL because the live catalog changes infrequently.
    "catalog_stale": CATALOG_STALE_COOLDOWN_SECONDS,
}
_DEFAULT_COOLDOWN_SECONDS = 60.0
# Probe outcomes that describe the provider account (billing, entitlement), not
# one route: every sibling route would fail the same way, so the whole provider
# is cooled down and its remaining candidates are skipped without probing.
_PROVIDER_SCOPE_CATEGORIES = frozenset({"payment_required", "permission", "authentication"})


# Source recorded on an admission record confirmed by the ladder's own probe.
LADDER_CONFIRMATION_SOURCE = "ladder_probe:openai_health_probe"

_ADMISSION_TO_LADDER: Mapping[str, EligibilityStage] = {
    "DISCOVERED": EligibilityStage.DISCOVERED,
    "ENTITLED": EligibilityStage.ENTITLED,
    "HEALTHY": EligibilityStage.HEALTHY,
    "AVAILABLE": EligibilityStage.AVAILABLE,
}


class HarnessVisibility:
    """Harness gate: which route ids the worker harness can actually spawn.

    ``ids`` is None when no inventory source was reachable. The gate then fails
    closed and the ladder reports ``harness_inventory_unavailable`` instead of
    ``not_harness_visible``.
    """

    def __init__(self, ids: frozenset[str] | None, *, source: str) -> None:
        self.ids = ids
        self.source = source

    @property
    def available(self) -> bool:
        return self.ids is not None

    def __call__(self, route_id: str) -> bool:
        return self.ids is not None and route_id in self.ids


def _parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def cooldown_seconds_for(category: str, retry_after_seconds: float | None) -> float:
    if retry_after_seconds is not None and retry_after_seconds > 0:
        return float(retry_after_seconds)
    return _CATEGORY_COOLDOWN_SECONDS.get(category, _DEFAULT_COOLDOWN_SECONDS)


@dataclass
class _Assessment:
    route_id: str
    provider: str
    capacity: CapacityClass
    plan_label: str
    failed_stage: EligibilityStage | None = None
    reason: str = ""
    cooldown_until: str | None = None
    health: str = "unprobed"  # healthy | unhealthy | unprobed | stale
    health_category: str = ""
    fit: int = 0
    tier: int = 2  # capability tier (0 = frontier ... 3 = small)
    tier_known: bool = False  # False means classifier/inventory could not tier this route
    slack: int = 0  # tiers of capability beyond the task's sufficiency floor
    price: float = 0.0  # marginal metered price per 1M tokens (in + out)
    price_known: bool = False  # True only when explicit pricing was found on the row
    # BOD-277: capability facts pulled from the inventory row (already read).
    context_window: int | None = None
    supports_tools: bool | None = None
    supports_structured_output: bool | None = None
    # BOD-177: backend pool identity and capacity classification evidence.
    pool: str = ""
    capacity_evidence: str = ""
    # AC6: the cooldown key under which this route is currently blocked
    # (never an email, token, or account id).
    cooldown_scope: str = ""

    @property
    def reached(self) -> EligibilityStage | None:
        order = (
            EligibilityStage.DISCOVERED,
            EligibilityStage.ENTITLED,
            EligibilityStage.HEALTHY,
            EligibilityStage.AVAILABLE,
            EligibilityStage.TASK_ELIGIBLE,
        )
        reached: EligibilityStage | None = None
        for stage in order:
            if self.failed_stage is stage:
                break
            if stage is EligibilityStage.HEALTHY and self.health != "healthy":
                break
            reached = stage
        return reached

    def selectable(self) -> bool:
        """Passes every hard gate; health may still be pending a lazy probe."""
        return self.failed_stage is None and self.health != "unhealthy"

    def verdict(
        self, rank: int | None = None, *, rank_components: Mapping[str, Any] | None = None
    ) -> RouteVerdict:
        reason = self.reason
        if not reason:
            reason = "unprobed" if self.health != "healthy" else "ranked"
        return RouteVerdict(
            route_id=self.route_id,
            provider=self.provider,
            reached=self.reached,
            failed_stage=self.failed_stage,
            reason=reason,
            capacity_class=self.capacity,
            plan_label=self.plan_label,
            cooldown_until=self.cooldown_until,
            rank=rank,
            # BOD-277: expose the exact values the sort key used, plus the
            # capability columns the assessment already has. Unknowns are
            # None (never fabricated defaults); tier/price are hidden when
            # the assessment never got that far (e.g. failed at HEALTHY).
            rank_components=dict(rank_components) if rank_components is not None else None,
            capability_tier=self.tier if self.tier_known else None,
            context_window=self.context_window,
            supports_tools=self.supports_tools,
            supports_structured_output=self.supports_structured_output,
            price=self.price if self.price_known else None,
            # AC6: non-secret provenance
            pool=self.pool,
            capacity_evidence=self.capacity_evidence,
            cooldown_scope=self.cooldown_scope,
        )


class EligibilityLadder:
    def __init__(
        self,
        inventory_rows: Sequence[Mapping[str, Any]],
        connections: Sequence[Mapping[str, Any]],
        probe: Callable[[str], HealthResult],
        state_path: Path,
        *,
        healthy_ttl_seconds: float = 300.0,
        harness_visible: Callable[[str], bool] | None = None,
        prefer_providers: tuple[str, ...] = ("claude",),
        load: Callable[[str], int] | None = None,
        max_per_route: int = 2,
        max_probes_per_select: int = 8,
        admitted: AdmittedSet | None = None,
        admission_receipt: Path | None = None,
        health_cache: Any | None = None,
        session_ledger: SessionLedger | None = None,
        allow_unknown_capacity: bool | None = None,
        refresh_hook: Callable[[Sequence[str], datetime], Mapping[str, str] | None] | None = None,
    ) -> None:
        self._rows = {str(r.get("id", "")): r for r in inventory_rows if r.get("id")}
        self._connections = list(connections)
        self._probe = probe
        self._state_path = Path(state_path)
        self._ttl = timedelta(seconds=healthy_ttl_seconds)
        self._harness_visible = harness_visible
        self._prefer_providers = tuple(p.lower() for p in prefer_providers)
        self._load = load or (lambda _route: 0)
        self._max_per_route = max_per_route
        self._max_probes = max_probes_per_select
        self._state: dict[str, dict[str, dict[str, Any]]] = self._load_state()
        self._last_verdicts: tuple[RouteVerdict, ...] = ()
        self._last_select_stats: dict[str, int] = {}
        # Canonical admitted set: a route it excludes fails before any ladder
        # stage, ranking or probe. The ladder can only narrow it further.
        self._admitted = admitted
        # Where the admitted set (with live confirmations) is persisted.
        self._admission_receipt = admission_receipt
        # Health cache (from #742 prove-at-rest daemon). Read-only in selection.
        self._health_cache = health_cache
        # BOD-299 phase 1: optional real-session evidence, read-only in selection.
        self._session_ledger = session_ledger
        self._session_evidence_error: str | None = None
        # UNKNOWN capacity opt-in: env override if not set explicitly.
        if allow_unknown_capacity is not None:
            self._allow_unknown = allow_unknown_capacity
        else:
            self._allow_unknown = os.environ.get(ENV_ALLOW_UNKNOWN, "").lower() in (
                "1",
                "true",
                "yes",
            )
        # BOD-292: optional selection-before-dispatch refresh hook. When set,
        # ``select`` calls it with the candidate ids BEFORE probing so the
        # bounded coordinator can refresh needed non-fresh prepaid evidence.
        # Default None keeps behaviour identical; the existing exact ladder
        # confirmation always still runs regardless of the hook.
        self._refresh_hook = refresh_hook

    @property
    def admitted(self) -> AdmittedSet | None:
        return self._admitted

    def require_launchable(self, route_id: str, *, surface: str) -> None:
        """Launch precondition for callers that bind a selected route.

        Raises ``AdmissionBypassError`` when an admitted set is attached and
        ``route_id`` is neither proven healthy nor confirmed live.
        """
        if self._admitted is not None:
            self._admitted.require_launchable(route_id, surface=surface)

    def _record_confirmation(self, route_id: str, result: HealthResult, now: datetime) -> None:
        """Fold one confirm probe into the admitted set and persist the receipt.

        Success records the confirmation source and time. Failure drops the
        route at ``HEALTHY``. A failed confirmation only narrows the set.
        """
        if self._admitted is None:
            return
        self._admitted = self._admitted.record_confirmation(
            route_id,
            healthy=result.healthy,
            source=LADDER_CONFIRMATION_SOURCE,
            observed_at=_iso(now),
            category=result.category,
        )
        if self._admission_receipt is not None:
            self._admitted.write_receipt(self._admission_receipt)

    def _load_state(self) -> dict[str, dict[str, dict[str, Any]]]:
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        health = raw.get("health") if isinstance(raw, dict) else None
        cooldowns = raw.get("cooldowns") if isinstance(raw, dict) else None
        not_free = raw.get("not_free_overrides") if isinstance(raw, dict) else None
        return {
            "health": dict(health) if isinstance(health, dict) else {},
            "cooldowns": dict(cooldowns) if isinstance(cooldowns, dict) else {},
            "not_free_overrides": dict(not_free) if isinstance(not_free, dict) else {},
        }

    def _persist(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._state, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self._state_path)

    def _connection_for(self, provider: str) -> Mapping[str, Any] | None:
        # One pass over the connections per provider, not one per route: a
        # large catalog has thousands of routes and a few dozen connections.
        # Same rule as before: the first active connection for the provider,
        # else its first connection.
        key = provider.lower()
        index = self.__dict__.get("_connection_index")
        if index is None or index[0] is not self._connections:
            table: dict[str, Mapping[str, Any] | None] = {}
            for conn in self._connections:
                name = str(conn.get("provider", "")).lower()
                current = table.get(name)
                if current is None or (not current.get("isActive") and conn.get("isActive")):
                    table[name] = conn
            index = (self._connections, table)
            self.__dict__["_connection_index"] = index
        return index[1].get(key)

    def capacity_class(self, route_id: str) -> CapacityClass:
        """Economic class from the same account evidence used by selection."""
        row = self._rows.get(route_id, {})
        raw_provider = str(row.get("owned_by", "")).lower()
        provider = resolve_provider(raw_provider) if raw_provider else route_id.split("/", 1)[0]
        conn = self._connection_for(provider)
        if conn is None and raw_provider and raw_provider != provider:
            conn = self._connection_for(raw_provider)
        return self._capacity_class(conn, row, route_id=route_id)[0]

    def _capacity_class(
        self, conn: Mapping[str, Any] | None, row: Mapping[str, Any], route_id: str = ""
    ) -> tuple[CapacityClass, str, str]:
        """Classify capacity with evidence.

        Returns ``(capacity, plan_label, evidence_rule)`` where
        ``evidence_rule`` names the classification rule that fired.

        Priority order (design §B, plus origin/main signals restored):
        (0) conn is None → UNKNOWN (no connection data; checked before all
            other rules, so even a ``:free``-suffixed route without a
            connection returns UNKNOWN, not FREE).
        (a) ``:free`` suffix → FREE (overrides oauth → subscription).
        (b) Connection free signals (``importFreeModelsOnly``,
            ``import_free_only``, free tier/plan, ``plan_label`` containing
            ``free`` for any auth type) → FREE.
        (c) oauth without a free signal → SUBSCRIPTION.
        (d) all-zero explicit pricing → FREE.
        (e) apikey with positive pricing → METERED.
        (f) otherwise UNKNOWN with a named reason.
        """
        if conn is None:
            return CapacityClass.UNKNOWN, "", "no_connection"
        plan_label = sanitized_plan_label(conn.get("plan_label", ""))
        auth_type = str(conn.get("authType", "")).strip().lower()
        auth_evidence = auth_type if auth_type in {"apikey", "oauth", "none"} else "other"

        # (a) :free suffix overrides oauth → subscription (fixes D5: 72
        # kilocode :free misclassed as SUBSCRIPTION).
        if route_id and has_free_suffix(route_id):
            return CapacityClass.FREE, plan_label, "free_suffix"

        # (b) Connection-level free signals.
        is_free, signal = connection_signals_free(conn)
        if is_free:
            return CapacityClass.FREE, plan_label, signal

        # (c) oauth without a free signal → SUBSCRIPTION.
        if auth_type == "oauth":
            return CapacityClass.SUBSCRIPTION, plan_label, "oauth_subscription"

        # Pricing evidence for apikey connections.
        pricing = row.get("pricing")
        prices: list[float] = []
        if isinstance(pricing, Mapping):
            for value in pricing.values():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    prices.append(float(value))
        all_zero = bool(prices) and all(p == 0 for p in prices)
        positive = any(p > 0 for p in prices)

        # All-zero explicit pricing is FREE even for apikey.
        if all_zero:
            return CapacityClass.FREE, plan_label, "all_zero_pricing"

        # (e) apikey with positive pricing → METERED.
        if auth_type == "apikey" and positive:
            return CapacityClass.METERED, plan_label, "apikey_positive_pricing"

        # (f) UNKNOWN with a named, bounded reason. Never persist the raw
        # connection authType: inventory fields are untrusted and may contain
        # account identifiers or credentials.
        reason = f"no_pricing_data:auth={auth_evidence}"
        return CapacityClass.UNKNOWN, plan_label, reason

    def _health_status(self, route_id: str, now: datetime) -> tuple[str, str]:
        entry = self._state["health"].get(route_id)
        if not isinstance(entry, dict):
            return "unprobed", ""
        checked = _parse_iso(str(entry.get("checked_at", "")))
        if checked is None or now - checked > self._ttl:
            return "stale", str(entry.get("category", ""))
        if entry.get("healthy"):
            return "healthy", ""
        return "unhealthy", str(entry.get("category", "unknown"))

    def dispatch_blocker(self, route_id: str, *, now: datetime) -> str | None:
        """Launch-critical re-check immediately before dispatch (BOD-223).

        Evidence can change between selection and spawn: another node, the
        root supervisor or a worker runtime may have cooled this route or its
        provider in the shared ladder state. Reload persisted cooldowns (merge,
        never drop in-memory ones), then return the blocking key, or ``None``
        when the route may still launch. A blocked route must not be launched;
        the caller reselects from this refreshed evidence.
        """
        fresh = self._load_state()["cooldowns"]
        for key, entry in fresh.items():
            current = self._state["cooldowns"].get(key)
            if not isinstance(current, dict) or str(entry.get("until", "")) > str(
                current.get("until", "")
            ):
                self._state["cooldowns"][key] = entry
        raw_provider = str(self._rows.get(route_id, {}).get("owned_by", "")).lower()
        provider = resolve_provider(raw_provider) if raw_provider else raw_provider
        provider = provider or route_id.split("/", 1)[0].lower()
        for key in (f"pool:{pool_of(route_id)}", f"route:{route_id}", f"provider:{provider}"):
            if self._active_cooldown(key, now) is not None:
                return key
        return None

    def _active_cooldown(self, key: str, now: datetime) -> datetime | None:
        entry = self._state["cooldowns"].get(key)
        if not isinstance(entry, dict):
            return None
        until = _parse_iso(str(entry.get("until", "")))
        if until is not None and until > now:
            return until
        return None

    def _rate_limited_until(
        self, conn: Mapping[str, Any] | None, route_id: str, now: datetime
    ) -> datetime | None:
        if conn is None:
            return None
        windows = conn.get("rate_limited_until") or {}
        if not isinstance(windows, Mapping):
            return None
        blocked: datetime | None = None
        for key, value in windows.items():
            if "/" in str(key) and str(key) != route_id:
                continue
            until = _parse_iso(str(value))
            if until is not None and until > now and (blocked is None or until > blocked):
                blocked = until
        return blocked

    def _assess(self, route_id: str, requirements: TaskRequirements, now: datetime) -> _Assessment:
        row = self._rows[route_id]
        raw_provider = str(row.get("owned_by", "")).lower()
        provider = resolve_provider(raw_provider) if raw_provider else raw_provider
        if not provider:
            provider = route_id.split("/", 1)[0].lower()
        conn = self._connection_for(provider)
        # Fall back to raw owned_by if alias resolution found no connection.
        if conn is None and raw_provider and raw_provider != provider:
            conn = self._connection_for(raw_provider)
            if conn is not None:
                provider = raw_provider
        capacity, plan_label, capacity_evidence = self._capacity_class(conn, row, route_id=route_id)
        pool = pool_of(route_id)
        a = _Assessment(
            route_id=route_id,
            provider=provider,
            capacity=capacity,
            plan_label=plan_label,
            pool=pool,
            capacity_evidence=capacity_evidence,
        )

        if self._admitted is not None and route_id not in self._admitted:
            record = self._admitted.first_failure(route_id)
            stage = _ADMISSION_TO_LADDER.get(
                record.first_failed_stage.value if record.first_failed_stage else "",
                EligibilityStage.TASK_ELIGIBLE,
            )
            a.failed_stage = stage
            a.reason = f"admission:{record.reason}"
            a.cooldown_until = record.until
            return a
        if conn is None or not conn.get("isActive"):
            a.failed_stage, a.reason = EligibilityStage.ENTITLED, "no_active_account"
            return a
        if self._harness_visible is not None and not self._harness_visible(route_id):
            unavailable = getattr(self._harness_visible, "available", True) is False
            a.failed_stage = EligibilityStage.ENTITLED
            a.reason = "harness_inventory_unavailable" if unavailable else "not_harness_visible"
            return a

        a.health, a.health_category = self._health_status(route_id, now)
        if a.health == "unhealthy":
            a.failed_stage, a.reason = EligibilityStage.HEALTHY, a.health_category
            _ck = f"route:{route_id}"
            until = self._active_cooldown(_ck, now)
            if until is not None:
                a.cooldown_until = _iso(until)
                a.cooldown_scope = _ck  # AC6: non-secret scope key
            return a

        for key, label in (
            (f"pool:{pool}", "cooldown:pool"),
            (f"route:{route_id}", "cooldown:route"),
            (f"provider:{provider}", "cooldown:provider"),
        ):
            until = self._active_cooldown(key, now)
            if until is not None:
                a.failed_stage, a.reason = EligibilityStage.AVAILABLE, label
                a.cooldown_until = _iso(until)
                a.cooldown_scope = key  # AC6: non-secret scope key
                return a
        limited = self._rate_limited_until(conn, route_id, now)
        if limited is not None:
            a.failed_stage, a.reason = EligibilityStage.AVAILABLE, "provider_rate_limited"
            a.cooldown_until = _iso(limited)
            return a

        # BOD-277: pull the capability facts the assessment already has access
        # to onto the assessment so RouteVerdict can expose them. None means
        # unknown (rendered as UNKNOWN downstream); we never invent a value.
        a.context_window, a.supports_tools, a.supports_structured_output = _capability_facts(row)

        reason = self._task_gate(row, route_id, requirements, a.capacity, now=now)
        if reason:
            a.failed_stage, a.reason = EligibilityStage.TASK_ELIGIBLE, reason
            return a
        tier = _capability_tier(row, route_id)
        if tier is None:
            # Unknown capability is never promoted to sufficient. It only
            # passes when the work accepts any tier, and then ranks after every
            # route whose capability is known (it cannot win on a guess).
            if requirements.max_capability_tier < 3:
                a.failed_stage, a.reason = EligibilityStage.TASK_ELIGIBLE, "unknown_capability"
                return a
            a.tier, a.slack, a.tier_known = 3, _UNKNOWN_SLACK, False
        else:
            a.tier, a.tier_known = tier, True
            if a.tier > requirements.max_capability_tier:
                # Insufficient for this work: dropped before ranking, however cheap.
                a.failed_stage = EligibilityStage.TASK_ELIGIBLE
                a.reason = "insufficient_capability"
                return a
            a.slack = requirements.max_capability_tier - a.tier
        a.price, a.price_known = _marginal_price(row)
        a.fit = self._fit(row, route_id, requirements)
        return a

    def _task_gate(
        self,
        row: Mapping[str, Any],
        route_id: str,
        req: TaskRequirements,
        capacity: CapacityClass = CapacityClass.UNKNOWN,
        now: datetime | None = None,
    ) -> str:
        caps = row.get("capabilities") or {}
        caps = caps if isinstance(caps, Mapping) else {}
        for needed in sorted(req.required_capabilities):
            key = {"tools": "tool_calling"}.get(needed, needed)
            if not caps.get(key):
                return f"missing_capability:{needed}"
        context = int(row.get("max_input_tokens") or row.get("context_length") or 0)
        if context < req.min_context_tokens:
            return "insufficient_context"
        if pool_of(route_id) in req.exclude_pools:
            return "excluded_pool"
        if route_id in req.exclude_routes:
            return "excluded_route"
        if route_family(route_id) in req.exclude_families:
            return "excluded_family"
        # Pool-aware independence: aliased backend pools (agy≡antigravity,
        # kc/kilocode/openrouter :free) are excluded by pool id, not prefix.
        if backend_pool(route_id) in req.exclude_families:
            return "excluded_family"
        if backend_pool(route_id) in aliased_pools_for(req.exclude_routes):
            return "excluded_family"
        lowered = route_id.lower()
        # Spend guard: frontier capability (classifier tier 0, or a declared
        # tier 0) is kept off non-frontier-worthy work only when it would cost
        # per call. Subscription/free frontier capacity is already paid for, so
        # it may serve as a fallback; the capability-slack ranking still
        # prefers the cheapest sufficient route, so it never wins by strength.
        if (
            not req.frontier_worthy
            and _capability_tier(row, route_id) == 0
            and capacity not in _PREPAID_CAPACITY
        ):
            return "frontier_restricted"
        for suffix in _EFFORT_SUFFIXES:
            if lowered.endswith(suffix) and route_id[: -len(suffix)] in self._rows:
                return "effort_duplicate"
        # UNKNOWN capacity gate: never for implementation unless opted in.
        if (
            capacity == CapacityClass.UNKNOWN
            and not req.frontier_worthy
            and not getattr(self, "_allow_unknown", False)
        ):
            return "unknown_capacity_not_opted_in"
        # Agentic gate: a FREE route qualifies as an implementation worker
        # only when a FRESH AGENTIC probe PASS is in the health cache.
        # A single-call PASS alone qualifies for chat/summary (frontier_worthy).
        if capacity == CapacityClass.FREE and not req.frontier_worthy:
            cache = getattr(self, "_health_cache", None)
            if cache is None:
                return "no_health_cache"
            from verdict.orchestration.health_cache import FRESH_SECONDS

            gate_now = now or datetime.now(timezone.utc)
            lookup = cache.lookup(route_id, gate_now)
            if lookup.entry is None or not lookup.entry.agentic_ok:
                return "no_agentic_probe"
            # The agentic gate uses agentic_checked_at (not the general
            # checked_at that single-call probes refresh). Only FRESH is
            # accepted: stale means the agentic qualification expired.
            ack = lookup.entry.agentic_checked_at
            if ack is None:
                return "agentic_probe_stale"
            age = (gate_now - ack).total_seconds()
            if age > FRESH_SECONDS:
                return "agentic_probe_stale"
            # Real session outcomes can narrow, never replace, probe qualification.
            stats = self._session_summary(route_id, gate_now)
            if stats is not None and stats.passes + stats.fails:
                recent = self._session_summary(route_id, gate_now, window_days=7)
                if recent is not None:
                    if recent.false_claims:
                        return "recent_false_claim"
                    if stats.score < 0.5:
                        return "session_evidence_insufficient"
        return ""

    def _session_summary(
        self, route_id: str, now: datetime, window_days: int = 14
    ) -> SessionStats | None:
        """Unreadable evidence fails open once per selection, not once per route."""
        ledger: SessionLedger | None = getattr(self, "_session_ledger", None)
        if ledger is None or getattr(self, "_session_evidence_error", None) is not None:
            return None
        try:
            return ledger.summarize_pool(route_id, now, window_days=window_days)
        except OSError:
            self._session_evidence_error = "unreadable"
            return None

    def _fit(self, row: Mapping[str, Any], route_id: str, req: TaskRequirements) -> int:
        score = 0
        lowered = route_id.lower()
        if req.coding and any(m in lowered for m in _CODING_MARKERS):
            score += 2
        caps = row.get("capabilities") or {}
        if req.reasoning and isinstance(caps, Mapping) and caps.get("reasoning"):
            score += 1
        return score

    def _rank_components(self, a: _Assessment) -> dict[str, Any]:
        """Compute all ranking components used to sort candidates.

        This is the single source of truth for ranking values. Returns an ordered
        dict with these keys (in sort order):

        - ``capacity_order``: int from _CAPACITY_ORDER mapping
        - ``slack``: capability tiers beyond task floor
        - ``price_for_rank``: float, always 0.0 when price_known is False
        - ``provider_pref``: index in prefer_providers or len() if not listed
        - ``load``: current assignment count for this route
        - ``fit``: capability tiers from task floor to route tier (positive)
        - ``route_id``: lexicographic tiebreaker
        - ``price_known``: bool, whether inventory had explicit pricing

        The sort tuple built by :meth:`_rank_key` negates ``fit`` so stronger
        routes rank lower (smaller is better). Exposed ``rank_components`` in
        :class:`RouteVerdict` shows ``price`` as ``None`` when ``price_known``
        is False (display truth) and ``fit`` as positive (capability distance).

        Uses ``getattr`` with defaults so partially-mocked ladders (BOD-203
        post-probe tests that bypass ``__init__``) keep working.
        """
        preferred = getattr(self, "_prefer_providers", ())
        try:
            pref = preferred.index(a.provider)
        except ValueError:
            pref = len(preferred)
        load_fn = getattr(self, "_load", None)
        load_value = int(load_fn(a.route_id)) if callable(load_fn) else 0
        price_known = bool(getattr(a, "price_known", False))
        price_value = getattr(a, "price", 0.0)
        cap_order = _CAPACITY_ORDER.get(a.capacity, 3)
        # Health cache freshness for the receipt (read-only, never written).
        cache = getattr(self, "_health_cache", None)
        probe_class = "none"
        cache_checked_at: str | None = None
        cache_freshness: str | None = None
        rc_now = getattr(self, "_current_now", None) or datetime.now(timezone.utc)
        session = self._session_summary(a.route_id, rc_now)
        if cache is not None:
            lookup = cache.lookup(a.route_id, rc_now)
            if lookup.entry is not None:
                probe_class = lookup.entry.probe_class
                cache_checked_at = lookup.entry.checked_at.isoformat()
                cache_freshness = lookup.state
        components = {
            "capacity_order": cap_order,
            "slack": getattr(a, "slack", 0),
            "price_for_rank": price_value if price_known else 0.0,
            "provider_pref": pref,
            "load": load_value,
            "fit": getattr(a, "fit", 0),
            "route_id": a.route_id,
            "price_known": price_known,
            "probe_class": probe_class,
            "cache_checked_at": cache_checked_at,
            "cache_freshness": cache_freshness,
            # Observational only in phase 1; _rank_key intentionally ignores these.
            "session_score": session.score if session is not None else None,
            "session_passes": session.passes if session is not None else None,
            "session_fails": session.fails if session is not None else None,
        }
        error = getattr(self, "_session_evidence_error", None)
        if error is not None:
            components["session_evidence_error"] = error
        return components

    def _rank_key(self, a: _Assessment) -> tuple[int, int, float, int, int, int, str]:
        """Build the sort tuple from :meth:`_rank_components`.

        Negates ``fit`` so stronger routes rank lower (smaller tuple wins).
        """
        c = self._rank_components(a)
        return (
            c["capacity_order"],
            c["slack"],
            c["price_for_rank"],
            c["provider_pref"],
            c["load"],
            -c["fit"],  # negate: stronger routes (higher fit) should rank lower
            c["route_id"],
        )

    def _rank_components_for_display(self, components: dict[str, Any]) -> dict[str, Any]:
        """Transform internal rank_components to consumer-facing format.

        Internal format has ``price_for_rank`` (always a float, 0.0 when unknown)
        and ``price_known`` (bool). Consumer format replaces these with ``price``
        (None when unknown, float otherwise) and omits ``price_known``.
        """
        price_known = components.get("price_known", False)
        price_value = components.get("price_for_rank", 0.0)
        display = {
            "capacity_order": components["capacity_order"],
            "slack": components["slack"],
            "price": price_value if price_known else None,
            "provider_pref": components["provider_pref"],
            "load": components["load"],
            "fit": components["fit"],  # stays positive
            "route_id": components["route_id"],
            "probe_class": components.get("probe_class", "none"),
            "cache_checked_at": components.get("cache_checked_at"),
            "cache_freshness": components.get("cache_freshness"),
            "session_score": components.get("session_score"),
            "session_passes": components.get("session_passes"),
            "session_fails": components.get("session_fails"),
        }
        if "session_evidence_error" in components:
            display["session_evidence_error"] = components["session_evidence_error"]
        return display

    def _assess_all(
        self, requirements: TaskRequirements, now: datetime
    ) -> tuple[list[_Assessment], list[_Assessment]]:
        self._current_requirements = requirements
        self._current_now = now
        self._session_evidence_error = None
        assessments: list[_Assessment] = []
        for route_id in sorted(self._rows):
            row = self._rows[route_id]
            lowered = route_id.lower()
            if (
                lowered.startswith(_OPAQUE_PREFIXES)
                or str(row.get("owned_by", "")).lower() == "combo"
            ):
                continue  # opaque routers are never DISCOVERED
            assessments.append(self._assess(route_id, requirements, now))
        candidates: list[_Assessment] = []
        for a in assessments:
            if not a.selectable():
                continue
            if self._load(a.route_id) >= self._max_per_route:
                a.failed_stage, a.reason = EligibilityStage.SELECTED, "at_capacity"
                continue
            candidates.append(a)
        candidates.sort(key=self._rank_key)
        return assessments, candidates

    def evaluate(
        self, requirements: TaskRequirements, *, now: datetime
    ) -> tuple[RouteVerdict, ...]:
        assessments, candidates = self._assess_all(requirements, now)
        ranks = {a.route_id: i for i, a in enumerate(candidates)}
        # BOD-277: candidates already carry the assessment fields we expose.
        components = {a.route_id: self._rank_components(a) for a in candidates}
        verdicts = tuple(
            a.verdict(
                rank=ranks.get(a.route_id),
                rank_components=(
                    self._rank_components_for_display(components[a.route_id])
                    if a.route_id in components
                    else None
                ),
            )
            for a in assessments
        )
        self._last_verdicts = verdicts
        return verdicts

    @property
    def last_select_stats(self) -> dict[str, int]:
        """Post-probe statistics from the most recent :meth:`select` call.

        Keys: ``probed``, ``stale``, ``healthy_after_probe``,
        ``eligible_after_probe``.  Empty dict before the first call.
        """
        return dict(self._last_select_stats)

    def select(
        self, requirements: TaskRequirements, *, now: datetime
    ) -> tuple[RouteVerdict | None, tuple[RouteVerdict, ...]]:
        assessments, candidates = self._assess_all(requirements, now)
        # BOD-292 selection-before-dispatch: give the bounded refresh coordinator
        # the candidate ids so it can refresh needed non-fresh prepaid evidence
        # before we probe. The hook waits for its bounded job and never mutates
        # selection; exact confirmation below is unchanged. A hook failure must
        # not break selection, so it is best-effort.
        refresh_hook = getattr(self, "_refresh_hook", None)
        refresh_results: Mapping[str, str] | None = None
        if refresh_hook is not None and candidates:
            with contextlib.suppress(Exception):
                refresh_results = refresh_hook([a.route_id for a in candidates], now)
        if isinstance(refresh_results, Mapping):
            for candidate in candidates:
                status = refresh_results.get(candidate.route_id)
                if isinstance(status, str) and status.lower() in {"failed", "unavailable"}:
                    candidate.failed_stage = EligibilityStage.HEALTHY
                    candidate.reason = status.lower()
        rank_of = {a.route_id: i for i, a in enumerate(candidates)}
        probes_used = 0
        chosen: _Assessment | None = None
        chosen_rank: int | None = None
        blocked: dict[str, str] = {}  # provider -> cooldown_until (set in this select)
        for a in self._probe_order(candidates):
            if a.failed_stage is not None:
                continue  # refresh reported failed/unavailable: never confirm
            if a.provider in blocked:
                a.failed_stage, a.reason = EligibilityStage.AVAILABLE, "cooldown:provider"
                a.cooldown_until = blocked[a.provider]
                continue
            # Launch gate: an admitted route that is not proven healthy needs a
            # bounded live confirmation for this exact route. A cached healthy
            # hit in the ladder state is not proof for an unverified route.
            confirming = self._admitted is not None and not self._admitted.launchable(a.route_id)
            if a.health != "healthy" or confirming:
                if probes_used >= self._max_probes:
                    a.reason = "probe_budget_exhausted"
                    continue
                probes_used += 1
                result = self._probe(a.route_id)
                self._record_health(a.route_id, result, now, provider=a.provider)
                if confirming:
                    self._record_confirmation(a.route_id, result, now)
                if not result.healthy:
                    a.health = "unhealthy"
                    a.failed_stage = EligibilityStage.HEALTHY
                    a.reason = result.category
                    entry = self._state["cooldowns"].get(f"route:{a.route_id}")
                    if isinstance(entry, dict):
                        a.cooldown_until = str(entry.get("until"))
                    if result.category in _PROVIDER_SCOPE_CATEGORIES:
                        until = self._active_cooldown(f"provider:{a.provider}", now)
                        blocked[a.provider] = _iso(until) if until else str(a.cooldown_until)
                    continue
                a.health, a.reason = "healthy", ""
            if self._admitted is not None:
                # Asserted precondition: admitted AND (proven healthy OR confirmed).
                self._admitted.require_launchable(a.route_id, surface="EligibilityLadder.select")
            chosen, chosen_rank = a, rank_of[a.route_id]
            break
        for a in candidates:  # routes not reached before the break still inherit the block
            if a.provider in blocked and a.failed_stage is None and a is not chosen:
                a.failed_stage, a.reason = EligibilityStage.AVAILABLE, "cooldown:provider"
                a.cooldown_until = blocked[a.provider]
        ranks = {c.route_id: i for i, c in enumerate(candidates)}
        # BOD-277: same rank_components map the pre-probe evaluate() builds.
        # Only candidates (routes that reached ranking) have components.
        components = {a.route_id: self._rank_components(a) for a in candidates}
        verdicts: list[RouteVerdict] = []
        selected: RouteVerdict | None = None
        for a in assessments:
            if chosen is not None and a.route_id == chosen.route_id:
                selected = RouteVerdict(
                    route_id=a.route_id,
                    provider=a.provider,
                    reached=EligibilityStage.SELECTED,
                    failed_stage=None,
                    reason="selected",
                    capacity_class=a.capacity,
                    plan_label=a.plan_label,
                    cooldown_until=None,
                    rank=chosen_rank,
                    rank_components=(
                        self._rank_components_for_display(components[a.route_id])
                        if a.route_id in components
                        else None
                    ),
                    capability_tier=a.tier if a.tier_known else None,
                    context_window=a.context_window,
                    supports_tools=a.supports_tools,
                    supports_structured_output=a.supports_structured_output,
                    price=a.price if a.price_known else None,
                    # AC6: non-secret provenance
                    pool=a.pool,
                    capacity_evidence=a.capacity_evidence,
                    cooldown_scope=a.cooldown_scope,
                )
                verdicts.append(selected)
            else:
                verdicts.append(
                    a.verdict(
                        rank=ranks.get(a.route_id),
                        rank_components=(
                            self._rank_components_for_display(components[a.route_id])
                            if a.route_id in components
                            else None
                        ),
                    )
                )
        self._last_verdicts = tuple(verdicts)
        final_verdicts = tuple(verdicts)
        # Compute post-probe stats for callers that need them (e.g. event emitters).
        # Stale count comes from pre-probe assessments (RouteVerdict has no health field).
        stale = sum(1 for a in assessments if a.health == "stale")
        healthy_after = sum(
            1
            for v in final_verdicts
            if v.reached is not None
            and v.reached.value in ("HEALTHY", "AVAILABLE", "TASK_ELIGIBLE", "SELECTED")
        )
        eligible_after = sum(
            1
            for v in final_verdicts
            if v.reached is not None and v.reached.value in ("TASK_ELIGIBLE", "SELECTED")
        )
        self._last_select_stats = {
            "probed": probes_used,
            "stale": stale,
            "healthy_after_probe": healthy_after,
            "eligible_after_probe": eligible_after,
        }
        return selected, final_verdicts

    def _probe_order(self, candidates: list[_Assessment]) -> list[_Assessment]:
        """Rank order, but round-robin across providers within each capacity class.

        One provider's failing top routes can no longer consume the whole probe
        budget. Capacity classes stay in order, so spreading never trades a
        SUBSCRIPTION route for a METERED one.
        """
        order: list[_Assessment] = []
        tiers: dict[CapacityClass, dict[str, list[_Assessment]]] = {}
        for a in candidates:  # already rank-sorted; dicts keep first-seen order
            tiers.setdefault(a.capacity, {}).setdefault(a.provider, []).append(a)
        for by_provider in tiers.values():
            queues = list(by_provider.values())
            depth = max(len(q) for q in queues)
            for i in range(depth):
                order.extend(q[i] for q in queues if i < len(q))
        return order

    def _record_health(
        self, route_id: str, result: HealthResult, now: datetime, *, provider: str = ""
    ) -> None:
        # Reclassify catalog-stale errors: OmniRoute returns bad_request with
        # a specific message for ids not in the live catalog.
        category = result.category
        if (
            not result.healthy
            and category in ("bad_request", "unservable", "unsupported")
            and is_catalog_stale_error(result.error_message)
        ):
            category = "catalog_stale"
        self._state["health"][route_id] = {
            "healthy": result.healthy,
            "category": category,
            "checked_at": _iso(now),
        }
        if not result.healthy:
            seconds = cooldown_seconds_for(category, result.retry_after_seconds)
            entry = {"until": _iso(now + timedelta(seconds=seconds)), "category": category}
            self._state["cooldowns"][f"route:{route_id}"] = dict(entry)
            if provider and category in _PROVIDER_SCOPE_CATEGORIES:
                self._state["cooldowns"][f"pool:{pool_of(route_id)}"] = dict(entry)
                self._state["cooldowns"][f"provider:{provider}"] = dict(entry)
            # Live-evidence not-free override: mark the route and pool so
            # capacity classification reflects the override (design §B, item 3).
            if is_not_free_signal(
                category=category, status_code=getattr(result, "status_code", None)
            ):
                pool = backend_pool(route_id)
                until = _iso(
                    now
                    + timedelta(seconds=cooldown_seconds_for(category, result.retry_after_seconds))
                )
                record_not_free_override(
                    self._state, route_id, pool=pool, until_iso=until, reason=category
                )
        self._persist()

    def record_failure(
        self, route_id: str, failure: FailureClassification, *, now: datetime
    ) -> None:
        retry_after = failure.cooldown_seconds if failure.cooldown_seconds > 0 else None
        seconds = cooldown_seconds_for(failure.category, retry_after)
        entry = {"until": _iso(now + timedelta(seconds=seconds)), "category": failure.category}
        if failure.scope in {"route", "provider"}:
            self._state["cooldowns"][f"pool:{pool_of(route_id)}"] = dict(entry)
            self._state["cooldowns"][f"route:{route_id}"] = dict(entry)
        if failure.scope == "provider":
            raw_provider = str(self._rows.get(route_id, {}).get("owned_by", "")).lower()
            provider = resolve_provider(raw_provider) if raw_provider else raw_provider
            if not provider:
                provider = route_id.split("/", 1)[0].lower()
            self._state["cooldowns"][f"provider:{provider}"] = dict(entry)
        self._persist()

    def record_success(self, route_id: str, *, now: datetime) -> None:
        self._state["cooldowns"].pop(f"route:{route_id}", None)
        self._state["health"][route_id] = {"healthy": True, "category": "", "checked_at": _iso(now)}
        self._persist()

    def summary(self) -> dict[str, int]:
        order = list(EligibilityStage)

        def at_least(stage: EligibilityStage) -> int:
            return sum(
                1
                for v in self._last_verdicts
                if v.reached is not None and order.index(v.reached) >= order.index(stage)
            )

        return {
            "discovered": at_least(EligibilityStage.DISCOVERED),
            "entitled": at_least(EligibilityStage.ENTITLED),
            "healthy": at_least(EligibilityStage.HEALTHY),
            "available": at_least(EligibilityStage.AVAILABLE),
            "eligible": at_least(EligibilityStage.TASK_ELIGIBLE),
        }


def _capability_tier(row: Mapping[str, Any], route_id: str) -> int | None:
    """Declared capability tier from inventory metadata, else the id classifier.

    ``None`` means unknown: neither the inventory nor the classifier knows it.
    """
    declared = row.get("capability_tier")
    if isinstance(declared, int) and not isinstance(declared, bool) and 0 <= declared <= 3:
        return declared
    from verdict.classifier import classify_known

    return classify_known(route_id)


def _capability_facts(row: Mapping[str, Any]) -> tuple[int | None, bool | None, bool | None]:
    """Read context window and capability booleans from an inventory row.

    Returns ``(context_window, supports_tools, supports_structured_output)``.
    Any value that is not present or not parseable becomes ``None`` so that
    ``RouteVerdict`` can render UNKNOWN rather than a fabricated default.
    ``context_length`` and ``max_input_tokens`` are the two inventory shapes
    the task gate already reads (see ``_task_gate``); we mirror that here.
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
        # ``_task_gate`` maps required capability "tools" -> catalog key
        # "tool_calling"; that is the authoritative name for tool support.
        if "tool_calling" in caps_raw:
            tools = bool(caps_raw.get("tool_calling"))
        if "structured_output" in caps_raw:
            structured = bool(caps_raw.get("structured_output"))
    return context_window, tools, structured


def _marginal_price(row: Mapping[str, Any]) -> tuple[float, bool]:
    """Return ``(price_for_ranking, price_known)``.

    Ranking treats an unknown price as zero (so unpriced capacity such as
    subscription/free still ranks by capacity class and slack). BOD-277 also
    needs to distinguish "known and free" from "no pricing metadata"; the
    boolean flag captures whether any explicit non-negative numeric was found
    on the inventory row (mirrors ``free_tier_admit._catalog_prices``).
    """
    pricing = row.get("pricing")
    source = pricing if isinstance(pricing, Mapping) else None
    if source is None:
        return 0.0, False
    known = False
    total = 0.0
    for key in ("input", "output", "prompt", "completion"):
        value = source.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            known = True
            if value > 0:
                total += float(value)
    return total, known


def capacity_class_of(
    conn: Mapping[str, Any] | None, row: Mapping[str, Any]
) -> tuple[CapacityClass, str]:
    """Classify a route's capacity without instantiating a ladder.

    Applies the same rules as ``EligibilityLadder._capacity_class`` but
    without the :free-suffix and connection-level-signal extensions added on
    origin/main (which require provider_catalog helpers used only by the
    ladder).  Used by ``prove_at_rest`` and any caller that must not touch the
    ladder's state file.  ``True``/``False`` values in ``pricing`` are not
    treated as prices.
    """
    if conn is None:
        return CapacityClass.UNKNOWN, ""
    plan_label = str(conn.get("plan_label", ""))
    plan_lower = plan_label.lower()
    auth_type = str(conn.get("authType", "")).lower()
    pricing = row.get("pricing")
    prices: list[float] = []
    if isinstance(pricing, Mapping):
        for value in pricing.values():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                prices.append(float(value))
    all_zero = bool(prices) and all(p == 0 for p in prices)
    positive = any(p > 0 for p in prices)
    if auth_type == "oauth" and "free" not in plan_lower:
        return CapacityClass.SUBSCRIPTION, plan_label
    if bool(conn.get("import_free_only")) or "free" in plan_lower or all_zero:
        return CapacityClass.FREE, plan_label
    if auth_type == "apikey" and positive:
        return CapacityClass.METERED, plan_label
    return CapacityClass.UNKNOWN, plan_label
