"""Pure EligibilityLadder for orchestration model selection."""

from __future__ import annotations

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
from verdict.subagent_selection import HealthResult

if TYPE_CHECKING:
    from verdict.admission import AdmittedSet

_OPAQUE_PREFIXES = ("auto/", "combo/", "router/", "virtual/")
_FRONTIER_MARKERS = ("opus", "gpt-5.6", "gpt-6-sol", "gpt-6-astra", "fable")
# Ranks an unknown-capability route after every known one (tiers span 0..3).
_UNKNOWN_SLACK = 4
_EFFORT_SUFFIXES = ("-low", "-medium", "-high", "-xhigh", "-max", "-ultra")
_CODING_MARKERS = ("code", "codex", "sonnet", "fable", "opus")
_CAPACITY_ORDER: Mapping[CapacityClass, int] = {
    CapacityClass.SUBSCRIPTION: 0,
    CapacityClass.FREE: 1,
    CapacityClass.METERED: 2,
    CapacityClass.UNKNOWN: 3,
}
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
    slack: int = 0  # tiers of capability beyond the task's sufficiency floor
    price: float = 0.0  # marginal metered price per 1M tokens (in + out)

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

    def verdict(self, rank: int | None = None) -> RouteVerdict:
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
        # Canonical admitted set: a route it excludes fails before any ladder
        # stage, ranking or probe. The ladder can only narrow it further.
        self._admitted = admitted
        # Where the admitted set (with live confirmations) is persisted.
        self._admission_receipt = admission_receipt

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
        return {
            "health": dict(health) if isinstance(health, dict) else {},
            "cooldowns": dict(cooldowns) if isinstance(cooldowns, dict) else {},
        }

    def _persist(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._state, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self._state_path)

    def _connection_for(self, provider: str) -> Mapping[str, Any] | None:
        fallback: Mapping[str, Any] | None = None
        for conn in self._connections:
            if str(conn.get("provider", "")).lower() != provider.lower():
                continue
            if fallback is None:
                fallback = conn
            if conn.get("isActive"):
                return conn
        return fallback

    def _capacity_class(
        self, conn: Mapping[str, Any] | None, row: Mapping[str, Any]
    ) -> tuple[CapacityClass, str]:
        if conn is None:
            return CapacityClass.UNKNOWN, ""
        plan_label = str(conn.get("plan_label", ""))
        plan_lower = plan_label.lower()
        auth_type = str(conn.get("authType", "")).lower()
        pricing = row.get("pricing")
        prices: list[float] = []
        if isinstance(pricing, Mapping):
            for value in pricing.values():
                if isinstance(value, (int, float)):
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
        provider = str(self._rows.get(route_id, {}).get("owned_by", "")).lower()
        provider = provider or route_id.split("/", 1)[0].lower()
        for key in (f"route:{route_id}", f"provider:{provider}"):
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
        provider = str(row.get("owned_by", "")).lower()
        conn = self._connection_for(provider)
        capacity, plan_label = self._capacity_class(conn, row)
        a = _Assessment(
            route_id=route_id, provider=provider, capacity=capacity, plan_label=plan_label
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
            until = self._active_cooldown(f"route:{route_id}", now)
            if until is not None:
                a.cooldown_until = _iso(until)
            return a

        for key, label in (
            (f"route:{route_id}", "cooldown:route"),
            (f"provider:{provider}", "cooldown:provider"),
        ):
            until = self._active_cooldown(key, now)
            if until is not None:
                a.failed_stage, a.reason = EligibilityStage.AVAILABLE, label
                a.cooldown_until = _iso(until)
                return a
        limited = self._rate_limited_until(conn, route_id, now)
        if limited is not None:
            a.failed_stage, a.reason = EligibilityStage.AVAILABLE, "provider_rate_limited"
            a.cooldown_until = _iso(limited)
            return a

        reason = self._task_gate(row, route_id, requirements)
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
            a.tier, a.slack = 3, _UNKNOWN_SLACK
        else:
            a.tier = tier
            if a.tier > requirements.max_capability_tier:
                # Insufficient for this work: dropped before ranking, however cheap.
                a.failed_stage = EligibilityStage.TASK_ELIGIBLE
                a.reason = "insufficient_capability"
                return a
            a.slack = requirements.max_capability_tier - a.tier
        a.price = _marginal_price(row)
        a.fit = self._fit(row, route_id, requirements)
        return a

    def _task_gate(self, row: Mapping[str, Any], route_id: str, req: TaskRequirements) -> str:
        caps = row.get("capabilities") or {}
        caps = caps if isinstance(caps, Mapping) else {}
        for needed in sorted(req.required_capabilities):
            key = {"tools": "tool_calling"}.get(needed, needed)
            if not caps.get(key):
                return f"missing_capability:{needed}"
        context = int(row.get("max_input_tokens") or row.get("context_length") or 0)
        if context < req.min_context_tokens:
            return "insufficient_context"
        if route_id in req.exclude_routes:
            return "excluded_route"
        if route_family(route_id) in req.exclude_families:
            return "excluded_family"
        lowered = route_id.lower()
        if any(m in lowered for m in _FRONTIER_MARKERS) and not req.frontier_worthy:
            return "frontier_restricted"
        for suffix in _EFFORT_SUFFIXES:
            if lowered.endswith(suffix) and route_id[: -len(suffix)] in self._rows:
                return "effort_duplicate"
        return ""

    def _fit(self, row: Mapping[str, Any], route_id: str, req: TaskRequirements) -> int:
        score = 0
        lowered = route_id.lower()
        if req.coding and any(m in lowered for m in _CODING_MARKERS):
            score += 2
        caps = row.get("capabilities") or {}
        if req.reasoning and isinstance(caps, Mapping) and caps.get("reasoning"):
            score += 1
        return score

    def _rank_key(self, a: _Assessment) -> tuple[int, int, float, int, int, int, str]:
        try:
            pref = self._prefer_providers.index(a.provider)
        except ValueError:
            pref = len(self._prefer_providers)
        # BOD-271: within a capacity class the cheapest SUFFICIENT route wins.
        # Capability beyond the task's floor (slack) and metered price both cost
        # something, so a stronger model never wins merely for being stronger.
        # Load comes before task fit: spreading concurrent nodes across equally
        # eligible routes of the preferred capacity beats piling onto one route.
        return (
            _CAPACITY_ORDER[a.capacity],
            a.slack,
            a.price,
            pref,
            self._load(a.route_id),
            -a.fit,
            a.route_id,
        )

    def _assess_all(
        self, requirements: TaskRequirements, now: datetime
    ) -> tuple[list[_Assessment], list[_Assessment]]:
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
        verdicts = tuple(a.verdict(rank=ranks.get(a.route_id)) for a in assessments)
        self._last_verdicts = verdicts
        return verdicts

    def select(
        self, requirements: TaskRequirements, *, now: datetime
    ) -> tuple[RouteVerdict | None, tuple[RouteVerdict, ...]]:
        assessments, candidates = self._assess_all(requirements, now)
        rank_of = {a.route_id: i for i, a in enumerate(candidates)}
        probes_used = 0
        chosen: _Assessment | None = None
        chosen_rank: int | None = None
        blocked: dict[str, str] = {}  # provider -> cooldown_until (set in this select)
        for a in self._probe_order(candidates):
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
                )
                verdicts.append(selected)
            else:
                verdicts.append(a.verdict(rank=ranks.get(a.route_id)))
        self._last_verdicts = tuple(verdicts)
        return selected, tuple(verdicts)

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
        self._state["health"][route_id] = {
            "healthy": result.healthy,
            "category": result.category,
            "checked_at": _iso(now),
        }
        if not result.healthy:
            seconds = cooldown_seconds_for(result.category, result.retry_after_seconds)
            entry = {"until": _iso(now + timedelta(seconds=seconds)), "category": result.category}
            self._state["cooldowns"][f"route:{route_id}"] = dict(entry)
            if provider and result.category in _PROVIDER_SCOPE_CATEGORIES:
                self._state["cooldowns"][f"provider:{provider}"] = dict(entry)
        self._persist()

    def record_failure(
        self, route_id: str, failure: FailureClassification, *, now: datetime
    ) -> None:
        retry_after = failure.cooldown_seconds if failure.cooldown_seconds > 0 else None
        seconds = cooldown_seconds_for(failure.category, retry_after)
        entry = {"until": _iso(now + timedelta(seconds=seconds)), "category": failure.category}
        if failure.scope in {"route", "provider"}:
            self._state["cooldowns"][f"route:{route_id}"] = dict(entry)
        if failure.scope == "provider":
            provider = str(self._rows.get(route_id, {}).get("owned_by", "")).lower()
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


def _marginal_price(row: Mapping[str, Any]) -> float:
    pricing = row.get("pricing")
    if not isinstance(pricing, Mapping):
        return 0.0
    total = 0.0
    for key in ("input", "output", "prompt", "completion"):
        value = pricing.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            total += float(value)
    return total
