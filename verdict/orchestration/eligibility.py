"""Pure EligibilityLadder for orchestration model selection."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from verdict.capacity import CAPACITY_ORDER, billing_capacity_override, classify_route_capacity
from verdict.identify_rank import RatedModel, rate_model, rating_key
from verdict.openrouter_free_set import (
    OpenRouterFreeSet,
    is_openrouter_catalog_route,
    key_limit_on_non_free_is_route_scoped,
    openrouter_route_is_public_free,
)
from verdict.orchestration.contracts import (
    CapacityClass,
    EligibilityStage,
    FailureClassification,
    RouteVerdict,
    TaskRequirements,
    WorkerTerminal,
    route_family,
)
from verdict.orchestration.credential_pools import POOL_COOLDOWN_CATEGORIES, pool_of
from verdict.orchestration.provider_catalog import (
    CATALOG_STALE_COOLDOWN_SECONDS,
    aliased_pools_for,
    backend_pool,
    is_catalog_stale_error,
    is_not_free_signal,
    record_not_free_override,
    resolve_provider,
)
from verdict.subagent_selection import HealthResult

if TYPE_CHECKING:
    from verdict.admission import AdmittedSet
    from verdict.metadata.mapping import IdentityMap
    from verdict.metadata.store import MetadataSnapshot
    from verdict.orchestration.session_evidence import SessionLedger, SessionStats

_LOG = logging.getLogger(__name__)

_OPAQUE_PREFIXES = ("auto/", "combo/", "router/", "virtual/")
# Ranks an unknown-capability route after every known one (tiers span 0..3).
_UNKNOWN_SLACK = 4
_EFFORT_SUFFIXES = ("-low", "-medium", "-high", "-xhigh", "-max", "-ultra")
_CODING_MARKERS = ("code", "codex", "sonnet", "fable", "opus")
_CAPACITY_ORDER = CAPACITY_ORDER
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
# Sentinel: no snapshot passed, so the ladder reads the default metadata store.
_DEFAULT_METADATA = object()
_DEFAULT_ARENA = object()
# Probe outcomes that describe the provider account (billing, entitlement), not
# one route: every sibling route would fail the same way, so the whole provider
# is cooled down and its remaining candidates are skipped without probing.
_PROVIDER_SCOPE_CATEGORIES = frozenset({"payment_required", "permission", "authentication"})


# Source recorded on an admission record confirmed by the ladder's own probe.
LADDER_CONFIRMATION_SOURCE = "ladder_probe:openai_health_probe"


def _is_inband_refusal(result: HealthResult) -> bool:
    """An HTTP 200 credit refusal is about one route, never the pool or provider.

    Free siblings on the same key still answer, so this must not cool them. A real
    HTTP 402 keeps its pool and provider cooldown (BOD-334).
    """
    return result.category == "payment_required" and result.status_code == 200


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


def read_only_valid_proof(route_ids: Sequence[str], *, now: datetime) -> dict[str, dict[str, Any]]:
    """Passport rows whose exact-route proof is valid for read-only (--no-confirm) choose.

    Operator ruling: --no-confirm accepts proof older than the 10-minute permit
    window while the passport is still valid. The validity test is the normal one,
    ``passport_confirmation(...)["admitted"]``, evaluated at the passport's own
    ``qualified_at`` (so only the permit window is waived), plus ``expires_at`` in
    the future. Newer negative health evidence drops the passport inside
    ``passport_confirmation``, so it still excludes.
    """
    from verdict.admit_prove_confirm import passport_confirmation

    valid: dict[str, dict[str, Any]] = {}
    for row in passport_confirmation(list(route_ids), now=now)["passport"]:
        route_id = str(row.get("identity_id", ""))
        qualified = _parse_iso(str(row.get("qualified_at", "")))
        expires_at = _parse_iso(str(row.get("expires_at", "")))
        if qualified is None or expires_at is None or expires_at <= now:
            continue
        if route_id in passport_confirmation([route_id], now=qualified)["admitted"]:
            valid[route_id] = row
    return valid


def _read_only_stale_reason(route_id: str, row: Mapping[str, Any], *, now: datetime) -> str:
    """Name why a route's proof is not valid for read-only choose."""
    if not row.get("qualified_at"):
        # passport_confirmation dropped or never found a passport: tell missing
        # apart from superseded by newer negative evidence.
        from verdict.prove_at_rest_legacy import load_healthy_passports

        return (
            "passport_superseded"
            if route_id in load_healthy_passports(None)
            else "passport_missing"
        )
    expires_at = _parse_iso(str(row.get("expires_at", "")))
    if expires_at is None or expires_at <= now:
        return "passport_expired"
    if row.get("auth_state") != "authorized":
        return "passport_unauthorized"
    if row.get("availability_state") != "eligible":
        return "passport_ineligible"
    return "passport_stale"


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def cooldown_seconds_for(category: str, retry_after_seconds: float | None) -> float:
    if retry_after_seconds is not None and retry_after_seconds > 0:
        return float(retry_after_seconds)
    return _CATEGORY_COOLDOWN_SECONDS.get(category, _DEFAULT_COOLDOWN_SECONDS)


DEFAULT_MIN_QUOTA_PERCENT = 5.0
_QUOTA_BUCKET = 20.0


def quota_remaining(conn: Mapping[str, Any] | None, route_id: str) -> float | None:
    """Measured remaining quota for a route: its own model window, else the account's."""
    if conn is None:
        return None
    model = route_id.split("/", 1)[-1]
    per_model = conn.get("quota_models")
    if isinstance(per_model, Mapping):
        for name in (model, re.sub(r"-(low|medium|high|xhigh|tiered)$", "", model)):
            if isinstance(per_model.get(name), (int, float)):
                return float(per_model[name])
    value = conn.get("quota_percent")
    return float(value) if isinstance(value, (int, float)) else None


def quota_rank(percent: float | None) -> int:
    """Coarse bucket so only material quota differences reorder routes.

    Unmeasured quota ranks with the middle bucket: never preferred over a route
    with plenty measured, never pushed behind one that is nearly exhausted.
    """
    if percent is None:
        return 2
    return 4 - min(4, int(percent // _QUOTA_BUCKET))


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
    agentic_capability: Mapping[str, Any] | None = None
    # Measured remaining subscription quota (percent), None when not reported.
    quota_percent: float | None = None
    # Task rating from Core's metadata store; the within-tier ranking component.
    rating: RatedModel | None = None
    evidence_level: str | None = None

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
            agentic_capability=self.agentic_capability,
        )


def selection_accounting(
    routes: Sequence[Mapping[str, Any]],
    *,
    attempted: Sequence[str] = (),
    authority_expired: bool = False,
) -> dict[str, Any]:
    """Project existing gate evidence and actual sends; never admit or rank routes.

    Actual free sends take precedence over later negatives on the same route.
    Only explicit authority termination marks otherwise eligible unsent supply.
    """
    sent = set(attempted)
    classes: dict[str, str] = {}
    reasons: dict[str, str] = {}
    negative = {
        "quota_exhausted",
        "quota_low",
        "payment_required",
        "authentication",
        "permission",
        "catalog_stale",
        "provider_rate_limited",
        "rate_limited",
        "newer_negative",
        "unavailable",
        "failed",
    }
    for row in routes:
        route = str(row.get("route_id") or row.get("model_id") or "")
        if not route or route.lower().startswith(_OPAQUE_PREFIXES):
            continue
        capacity = row.get("capacity_class")
        free = capacity == CapacityClass.FREE or row.get("capacity_order") == 0
        reason = str(row.get("reason") or "")
        code = reason.removeprefix("admission:")
        eligible = row.get("admitted") is True or (
            row.get("failed_stage") is None and row.get("reached") in ("TASK_ELIGIBLE", "SELECTED")
        )
        if route in sent and free:
            category = "eligible_free_attempted"
        elif code in negative or code.startswith("cooldown:"):
            category = "known_unavailable"
        elif (
            row.get("failed_stage")
            and code
            not in {
                "at_capacity",
                "excluded_route",
                "excluded_pool",
                "excluded_family",
                "effort_duplicate",
            }
        ) or (row.get("admitted") is False and reason):
            category = "unqualified_supply"
        elif authority_expired and eligible and route not in sent:
            category = "eligible_unattempted_authority_expired"
        else:
            continue
        if classes.get(route) == "eligible_free_attempted":
            continue
        classes[route] = category
        reasons[route] = reason
    block: dict[str, Any] = {
        key: sorted(route for route, category in classes.items() if category == key)
        for key in (
            "eligible_free_attempted",
            "known_unavailable",
            "unqualified_supply",
            "eligible_unattempted_authority_expired",
        )
    }
    for category, key in (
        ("known_unavailable", "known_unavailable_reasons"),
        ("unqualified_supply", "unqualified_reasons"),
    ):
        block[key] = [{"route_id": route, "reason": reasons[route]} for route in block[category]]
    return block


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
        min_quota_percent: float = DEFAULT_MIN_QUOTA_PERCENT,
        openrouter_free_set: OpenRouterFreeSet | None = None,
        metadata: Any = _DEFAULT_METADATA,
        identity_map: IdentityMap | None = None,
        rankings: Mapping[str, int] | None = None,
        rankings_omission: str | None = None,
        rankings_category: str | None = None,
        rankings_provenance: Mapping[str, Mapping[str, Any]] | None = None,
        rankings_received_at: str | None = None,
        read_only: bool = False,
        arena_snapshot: Any = _DEFAULT_ARENA,
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
        self._read_only = read_only
        self._state: dict[str, dict[str, dict[str, Any]]] = self._load_state()
        # BOD-341: select() runs concurrently on worker threads. Every _state
        # mutation and its _persist hold this lock (never across a probe).
        self._persist_lock = threading.RLock()
        self._last_verdicts: tuple[RouteVerdict, ...] = ()
        # Per thread: concurrent select() calls (BOD-341) must each read their own stats.
        self._select_stats = threading.local()
        # Canonical admitted set: a route it excludes fails before any ladder
        # stage, ranking or probe. The ladder can only narrow it further.
        self._admitted = admitted
        # Where the admitted set (with live confirmations) is persisted.
        self._admission_receipt = admission_receipt
        # Health cache (from #742 prove-at-rest daemon). Read-only in selection.
        self._health_cache = health_cache
        # S3b: discovery registry proof cache.  Loaded lazily on the first
        # agentic-gate check; None once loaded when the registry is absent or
        # stale.  The race between concurrent select() calls is intentionally
        # benign: both threads load the same file and the second write wins.
        self._discovery_registry_loaded: bool = False
        self._discovery_registry_cache: Any = None
        # U1: discovery metered-override cache (routes proven to charge per-request).
        # Loaded lazily on the first _capacity_class call; same benign-race rule.
        self._metered_overrides_loaded: bool = False
        self._metered_overrides: frozenset[str] = frozenset()
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
        self._min_quota_percent = min_quota_percent
        # BOD-418: public OpenRouter 0/0 pricing set. When present, openrouter-
        # family FREE membership requires membership in this set.
        self.openrouter_free_set = openrouter_free_set
        # Route ids whose health the last select() re-read upgraded after refresh.
        self._last_refresh_upgrades: tuple[str, ...] = ()
        # Rating evidence: the default Core metadata store unless the caller passes
        # a snapshot (or None for no ratings). Loaded once, on first ranking.
        self._metadata_arg = metadata
        self._identity_map = identity_map
        self._rankings = rankings
        self._rankings_omission = rankings_omission
        self._rankings_category = rankings_category
        self._rankings_provenance = rankings_provenance or {}
        self._rankings_received_at = rankings_received_at
        self._rating_evidence_cache: tuple[MetadataSnapshot | None, IdentityMap | None] | None = (
            None
        )
        # Arena advisory snapshot: loaded once on first use; None disables arena rankings.
        self._arena_snapshot_arg = arena_snapshot
        self._arena_snapshot_cache: Any = _DEFAULT_ARENA  # sentinel = not yet loaded

    @property
    def admitted(self) -> AdmittedSet | None:
        return self._admitted

    def _rating_evidence(self) -> tuple[MetadataSnapshot | None, IdentityMap | None]:
        """Metadata snapshot and identity map for ratings, loaded once per ladder."""
        cached: tuple[MetadataSnapshot | None, IdentityMap | None] | None = getattr(
            self, "_rating_evidence_cache", None
        )
        if cached is not None:
            return cached
        from verdict.metadata.mapping import load_identity_map
        from verdict.metadata.records import ModelMetadataError
        from verdict.metadata.store import load_store

        snapshot = getattr(self, "_metadata_arg", None)
        if snapshot is _DEFAULT_METADATA:
            try:
                snapshot = load_store()
            except (OSError, ValueError):  # missing or unreadable store: no ratings
                snapshot = None
        mapping = getattr(self, "_identity_map", None)
        if mapping is None and snapshot is not None:
            try:
                mapping = load_identity_map()
            except (OSError, ModelMetadataError):
                mapping = None
        self._rating_evidence_cache = (snapshot, mapping)
        return snapshot, mapping

    def _arena_evidence(self) -> Any:
        """Arena snapshot for advisory VR augmentation, loaded once per ladder.

        Returns None when disabled; returns the loaded snapshot otherwise.
        """
        cached = self._arena_snapshot_cache
        if cached is not _DEFAULT_ARENA:
            return cached
        arg = self._arena_snapshot_arg
        if arg is _DEFAULT_ARENA:
            # Default: try to load the on-disk snapshot; None if missing.
            try:
                from verdict.arena.snapshot import load_snapshot

                loaded = load_snapshot()
            except Exception:
                loaded = None
            if loaded is not None:
                # Only cache a successful load; leave the sentinel so a later
                # call retries when the file appears or is refreshed.
                self._arena_snapshot_cache = loaded
            return loaded
        # Explicit: None disables arena rankings, a snapshot object enables them.
        self._arena_snapshot_cache = arg
        return arg

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
        # Callers hold _persist_lock across their state mutation; holding it here
        # too keeps snapshot and replace atomic, so an older snapshot never lands last.
        with self._persist_lock:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            # BOD-341: a unique tmp name per call avoids concurrent select() calls
            # (now running on separate threads) colliding on the same tmp path
            # and racing os.replace with FileNotFoundError.
            tmp = self._state_path.with_suffix(
                f"{self._state_path.suffix}.{threading.get_ident()}.tmp"
            )
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
        """Classify from specific account evidence; absent pricing is assumed free.

        Missing connections remain UNKNOWN and cannot pass ENTITLED. Public
        OpenRouter free membership takes precedence; other OpenRouter routes
        and the Anthropic API-key connection are specifically metered.
        """
        cache = getattr(self, "_health_cache", None)
        if cache is not None and billing_capacity_override(
            cache, route_id, getattr(self, "_current_now", None) or datetime.now(timezone.utc)
        ):
            return CapacityClass.METERED, "", "billing_402_metered"
        # U1: a route proven to charge per-request during a discovery probe is
        # permanently METERED and must never be admitted as free-tier again.
        if not self._metered_overrides_loaded:
            try:
                from verdict.discovery.metered import load_metered_overrides as _load_metered

                self._metered_overrides = frozenset(_load_metered().keys())
            except Exception:
                self._metered_overrides = frozenset()
            self._metered_overrides_loaded = True
        if route_id in self._metered_overrides:
            return CapacityClass.METERED, "", "discovery_observed_charge"
        return _classify_capacity(
            conn,
            row,
            route_id=route_id,
            openrouter_free_set=getattr(self, "openrouter_free_set", None),
        )

    def _health_status(self, route_id: str, now: datetime) -> tuple[str, str]:
        cache = getattr(self, "_health_cache", None)
        expired_negative = None
        if cache is not None:
            lookup = cache.lookup(route_id, now)
            if lookup.entry is not None and not lookup.entry.healthy:
                if lookup.state == "negative":
                    return "unhealthy", lookup.entry.category
                # Expired negative is half-open: only a newer validated ladder
                # success may supersede it. Older/equal positives stay unprobed.
                expired_negative = lookup.entry
            elif lookup.state == "fresh" and lookup.entry is not None and lookup.entry.healthy:
                return "healthy", ""
        if self._admitted is not None and self._admitted.proven_healthy(route_id):
            return "healthy", ""
        entry = self._state["health"].get(route_id)
        if not isinstance(entry, dict):
            category = expired_negative.category if expired_negative is not None else ""
            return "unprobed", category
        checked = _parse_iso(str(entry.get("checked_at", "")))
        if expired_negative is not None and (
            not entry.get("healthy") or checked is None or checked <= expired_negative.checked_at
        ):
            return "unprobed", expired_negative.category
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
        cache = getattr(self, "_health_cache", None)
        if cache is not None and cache.path.exists():
            cache.reload()
        fresh = self._load_state()["cooldowns"]
        with self._persist_lock:
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
            if self._active_cooldown(key, now, route_id) is not None:
                return key
        if cache is not None and cache.lookup(route_id, now).state == "negative":
            return f"route:{route_id}"
        return None

    def _active_cooldown(
        self, key: str, now: datetime, route_id: str | None = None
    ) -> datetime | None:
        entry = self._state["cooldowns"].get(key)
        until = _parse_iso(str(entry.get("until", ""))) if isinstance(entry, dict) else None
        cache = getattr(self, "_health_cache", None)
        cooldown = cache.cooldown_for(key, now) if cache is not None else None
        if cooldown is not None and route_id is not None and key.startswith("provider:"):
            # BOD-407: same refutation as canonical admission. A cached provider
            # rate-limit cooldown is not current once the gateway tested a viable
            # connection after it and measures quota left.
            from verdict.admission import (
                RATE_LIMIT_COOLDOWN_CATEGORIES,
                live_quota_refutes_rate_limit,
            )

            provider = key.partition(":")[2]
            if cooldown.category in RATE_LIMIT_COOLDOWN_CATEGORIES and any(
                str(c.get("provider", "")).lower() == provider
                and c.get("isActive") is True
                and live_quota_refutes_rate_limit(
                    c, route_id=route_id, observed_at=cooldown.checked_at, now=now
                )
                for c in self._connections
            ):
                cooldown = None
        if cooldown is not None and (until is None or cooldown.until > until):
            until = cooldown.until
        return until if until is not None and until > now else None

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

        from verdict.orchestration.health_cache import agentic_capability

        cache = getattr(self, "_health_cache", None)
        a.agentic_capability = agentic_capability(cache.entry(route_id) if cache else None, now)

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
            until = self._active_cooldown(key, now, route_id)
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
        measured = [
            q
            for c in self._connections
            if str(c.get("provider", "")).lower() == provider and c.get("isActive")
            for q in [quota_remaining(c, route_id)]
            if q is not None
        ]
        a.quota_percent = min(measured) if measured else None
        floor = getattr(self, "_min_quota_percent", DEFAULT_MIN_QUOTA_PERCENT)
        if measured and max(measured) < floor:
            # Measured near-exhaustion: dispatching here fails mid-task on a 429.
            a.failed_stage, a.reason = EligibilityStage.AVAILABLE, "quota_low"
            resets = [
                _parse_iso(str(c.get("quota_reset_at") or ""))
                for c in self._connections
                if str(c.get("provider", "")).lower() == provider and c.get("isActive")
            ]
            reset = min((r for r in resets if r is not None), default=None)
            a.cooldown_until = _iso(reset) if reset is not None and reset > now else None
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
        # Effective tier ceiling: the task floor plus any escalation ceiling.
        # Escalation never widens (it only narrows within the admitted set).
        _esc = requirements.escalation_max_tier
        _effective_max_tier = (
            min(requirements.max_capability_tier, _esc)
            if _esc is not None
            else requirements.max_capability_tier
        )
        if tier is None:
            # When escalation is active, unknown-tier routes are treated as weaker
            # than any known tier and must not be selected: the runtime is explicitly
            # seeking a route with measured capability to escape repeated failures.
            # This check must come first so it fires for every escalation ceiling,
            # including ceilings below 3 (e.g. ceiling=2 still excludes unknown-tier
            # routes with reason "unknown_capability_escalating", not "unknown_capability").
            if requirements.escalation_max_tier is not None:
                a.failed_stage, a.reason = (
                    EligibilityStage.TASK_ELIGIBLE,
                    "unknown_capability_escalating",
                )
                return a
            # Unknown capability is never promoted to sufficient. It only
            # passes when the work accepts any tier, and then ranks after every
            # route whose capability is known (it cannot win on a guess).
            if _effective_max_tier < 3:
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
            if a.tier > _effective_max_tier:
                # Below the escalation ceiling: too weak for the current escalated attempt.
                a.failed_stage = EligibilityStage.TASK_ELIGIBLE
                a.reason = "below_escalation_floor"
                return a
            # Capacity class restriction for escalation: when not None, only routes
            # whose capacity class is in the allowed set are eligible.  This prevents
            # METERED/UNKNOWN tier-0 routes from being selected through the escalation
            # path.  Applied only when a restriction is active (None = no restriction).
            # An empty frozenset means "nothing allowed": all routes are excluded and
            # _drive will emit 'escalation_exhausted' (e.g. --capacity metered).
            if (
                requirements.escalation_capacity_classes is not None
                and a.capacity not in requirements.escalation_capacity_classes
            ):
                a.failed_stage = EligibilityStage.TASK_ELIGIBLE
                a.reason = "escalation_capacity_excluded"
                return a
            a.slack = requirements.max_capability_tier - a.tier
        if (
            a.capacity == CapacityClass.FREE
            and not requirements.frontier_worthy
            and requirements.max_capability_tier == 3
        ):
            a.evidence_level = "probation"
        a.price, a.price_known = _marginal_price(row)
        a.fit = self._fit(row, route_id, requirements)
        snapshot, mapping = self._rating_evidence()
        a.rating = rate_model(
            route_id,
            metadata=snapshot,
            task_family="code_edit" if requirements.coding else "general",
            declared_tier=_declared_tier(row),
            identity_map=mapping,
            now=now,
        )
        # Arena advisory augmentation: reorder among admitted candidates only.
        # Only applied when the route has a declared capability tier (tier_known)
        # so an unknown-tier route cannot leapfrog a known-tier route via Arena.
        arena_role = requirements.arena_role
        if arena_role is not None and a.tier_known:
            arena_snap = self._arena_evidence()
            if arena_snap is not None:
                try:
                    from dataclasses import replace

                    from verdict.arena.rank import vr_for_route

                    arena_vr = vr_for_route(route_id, arena_snap, arena_role, mapping, now)
                    if arena_vr is not None:
                        a.rating = replace(
                            a.rating,
                            score=arena_vr.vr,
                            metric=arena_vr.metric,
                            source="arena_elo",
                            as_of=arena_vr.published,
                            basis="score",
                        )
                except Exception as exc:  # Arena is advisory; never block routing on its failure
                    _LOG.debug("arena: vr augmentation failed: %s: %s", type(exc).__name__, exc)
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
        for suffix in _EFFORT_SUFFIXES:
            if lowered.endswith(suffix) and route_id[: -len(suffix)] in self._rows:
                return "effort_duplicate"
        # Agentic gate: FREE and UNKNOWN routes qualify as implementation workers
        # only when a FRESH AGENTIC probe PASS is in the health cache. UNKNOWN
        # routes must prove themselves the same way FREE routes do — the flag
        # _allow_unknown gates opt-in to routing, but it does not waive the
        # proof requirement. A single-call PASS alone qualifies for chat/summary
        # (frontier_worthy reasoning-only). Tool-requiring work needs agentic
        # proof even when frontier_worthy is set.
        if capacity in {CapacityClass.FREE, CapacityClass.UNKNOWN} and (
            not req.frontier_worthy or "tools" in req.required_capabilities
        ):
            cache = getattr(self, "_health_cache", None)
            if cache is None:
                return "no_health_cache"
            from verdict.orchestration.health_cache import agentic_capability

            gate_now = now or datetime.now(timezone.utc)
            lookup = cache.lookup(route_id, gate_now)
            proof = agentic_capability(lookup.entry, gate_now)
            if not proof["qualified"]:
                # A fresh measured agentic FAILURE (agentic_checked_at within
                # AGENTIC_PROBE_TTL_SECONDS and agentic_ok=False) is stronger
                # negative evidence than any registry entry.  It must not be
                # overridden by a registry tool_ok=True.
                from verdict.orchestration.health_cache import (
                    AGENTIC_PROBE_TTL_SECONDS as _AGENTIC_TTL,
                )

                _e = lookup.entry
                _fresh_agentic_failure = (
                    proof["source"] == "agentic_probe"
                    and _e is not None
                    and _e.agentic_checked_at is not None
                    # No lower bound: a future-dated failure (clock skew) is
                    # anomalous evidence and must fail closed.
                    and (gate_now - _e.agentic_checked_at).total_seconds() <= _AGENTIC_TTL
                    and not _e.agentic_ok
                )
                if _fresh_agentic_failure:
                    return "no_agentic_probe"
                if lookup.entry is None or (
                    proof["source"] == "agentic_probe" and not lookup.entry.agentic_ok
                ):
                    # S3b/U1: before rejecting as no_agentic_probe, check the
                    # discovery registry for a fresh tool_ok=True proof.
                    # FREE and UNKNOWN capacity both qualify — UNKNOWN is free
                    # until a probe charge proves otherwise (operator D31).
                    if capacity in {CapacityClass.FREE, CapacityClass.UNKNOWN}:
                        from verdict.discovery.registry import load_registry as _load_registry
                        from verdict.discovery.registry import tool_ok_proof as _tool_ok_proof

                        if not self._discovery_registry_loaded:
                            self._discovery_registry_cache = _load_registry()
                            self._discovery_registry_loaded = True
                        reg_result = _tool_ok_proof(
                            route_id, self._discovery_registry_cache, gate_now
                        )
                        if not reg_result["qualified"]:
                            return "no_agentic_probe"
                        # Registry proves tool capability — fall through to
                        # the session-evidence check below.
                    else:
                        return "no_agentic_probe"
                else:
                    return (
                        "session_capability_expired_or_revoked"
                        if proof["source"] == "session_evidence"
                        else "agentic_probe_stale"
                    )
            # Current liveness remains the separate HEALTHY stage. Unknown
            # or stale health may use the existing bounded cheap probe.
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

        This is the single source of truth for ranking values.  The ``_rank_key``
        method assembles these into a sort tuple whose order depends on the role:

        * **cheapest_sufficient_first** (worker/implement): capacity tier ->
          slack (smallest first) -> subscription tokens -> rating -> declared
          tier -> advisory -> quota -> provider_pref -> load -> fit -> route_id.
        * **strongest_first** (planner/reviewer): capacity tier -> subscription
          tokens -> rating -> declared tier -> slack -> advisory -> quota ->
          provider_pref -> load -> fit -> route_id.

        Marginal price is display only; capacity class orders already-qualified
        candidates, never admission.

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
        rating: RatedModel | None = getattr(a, "rating", None)
        if rating is None:
            rating = RatedModel(a.route_id, a.provider, None, None, None, None)
        rating_rank, neg_score, declared = rating_key(rating)
        # Health cache freshness for the receipt (read-only, never written).
        cache = getattr(self, "_health_cache", None)
        probe_class = "none"
        cache_checked_at: str | None = None
        cache_freshness: str | None = None
        rc_now = getattr(self, "_current_now", None) or datetime.now(timezone.utc)
        session = self._session_summary(a.route_id, rc_now)
        # Advisory ranks apply only to the category they were fetched for; an
        # admitted route with no entry has an unknown (None) rank and a named omission.
        rankings: dict[str, int] = getattr(self, "_rankings", None) or {}
        advisory_omission = getattr(self, "_rankings_omission", None)
        if not getattr(self, "_rankings_apply", True):
            rankings, advisory_omission = {}, "category_mismatch"
        elif advisory_omission is None and rankings and a.route_id not in rankings:
            advisory_omission = "no_entry"
        if cache is not None:
            lookup = cache.lookup(a.route_id, rc_now)
            if lookup.entry is not None:
                probe_class = lookup.entry.probe_class
                cache_checked_at = lookup.entry.checked_at.isoformat()
                cache_freshness = lookup.state
        quota = getattr(a, "quota_percent", None)
        _ordering_basis = (
            "strongest_first"
            if getattr(self._select_stats, "strongest_first", False)
            else "cheapest_sufficient_first"
        )
        components = {
            "ordering_basis": _ordering_basis,
            "rating_rank": rating_rank,
            "rating_basis": rating.basis,
            "rating": -neg_score if rating_rank in (0, 1) else None,
            "declared_tier": declared if rating_rank == 2 else None,
            "rating_metric": rating.metric,
            "rating_source": rating.source,
            "rating_as_of": rating.as_of,
            "rating_join": rating.join,
            "slack": getattr(a, "slack", 0),
            "quota_rank": quota_rank(quota if isinstance(quota, (int, float)) else None),
            "quota_percent": quota if isinstance(quota, (int, float)) else None,
            "expected_tokens": getattr(self, "_token_usage", {}).get(a.route_id),
            "provider_pref": pref,
            "load": load_value,
            "fit": getattr(a, "fit", 0),
            "route_id": a.route_id,
            # D6 capacity tier is the first selection key; marginal price is not.
            "capacity_order": _CAPACITY_ORDER.get(a.capacity, 3),
            "price_for_rank": price_value if price_known else 0.0,
            "price_known": price_known,
            "probe_class": probe_class,
            "evidence_level": getattr(a, "evidence_level", None),
            "cache_checked_at": cache_checked_at,
            "cache_freshness": cache_freshness,
            # Observational only in phase 1; _rank_key intentionally ignores these.
            "session_score": session.score if session is not None else None,
            "session_passes": session.passes if session is not None else None,
            "session_fails": session.fails if session is not None else None,
            "advisory_rank": rankings.get(a.route_id),
            "advisory_category": getattr(self, "_rankings_category", None),
            "advisory_omission": advisory_omission,
            "advisory_provenance": getattr(self, "_rankings_provenance", {}).get(a.route_id),
        }
        error = getattr(self, "_session_evidence_error", None)
        if error is not None:
            components["session_evidence_error"] = error
        return components

    def _rank_key(
        self, a: _Assessment
    ) -> tuple[
        int,
        int | float,
        int | float,
        int | float,
        int | float,
        float,
        int,
        int,
        int,
        int,
        int,
        int,
        str,
    ]:
        """Build the sort tuple from :meth:`_rank_components` (smaller wins).

        Two orderings are supported, chosen by ``role_ordering`` on the
        ``TaskRequirements`` that initiated this selection:

        * **cheapest_sufficient_first** (worker/implement nodes, the default):
          Slack comes before rating so the least capable route that still meets
          the floor wins within each capacity class.  Subscriptions sort by
          observed token usage after slack and before rating.

        * **strongest_first** (planner and reviewer roles): Rating and declared
          tier come before slack so the highest-quality admitted route wins.
          Subscriptions sort by token usage after rating, before slack.

        Price is excluded from the tuple in both orderings.
        """
        c = self._rank_components(a)
        subscription = a.capacity == CapacityClass.SUBSCRIPTION
        tokens = c["expected_tokens"]
        strongest_first: bool = getattr(self._select_stats, "strongest_first", False)
        if strongest_first:
            # Strongest-first: rating before slack.  Planner and reviewer roles.
            return (
                c["capacity_order"],
                int(tokens is None) if subscription else 0,
                float(tokens or 0) if subscription else 0.0,
                c["rating_rank"],
                -(c["rating"] or 0.0),
                c["declared_tier"] or 0,
                c["slack"],
                sys.maxsize if c["advisory_rank"] is None else c["advisory_rank"],
                0 if subscription else c["quota_rank"],
                c["provider_pref"],
                c["load"],
                -c["fit"],  # negate: stronger routes (higher fit) should rank lower
                c["route_id"],
            )
        # Cheapest-sufficient-first: slack before rating.  Worker/implement nodes.
        return (
            c["capacity_order"],
            c["slack"],
            int(tokens is None) if subscription else 0,
            float(tokens or 0) if subscription else 0.0,
            c["rating_rank"],
            -(c["rating"] or 0.0),
            c["declared_tier"] or 0,
            sys.maxsize if c["advisory_rank"] is None else c["advisory_rank"],
            0 if subscription else c["quota_rank"],
            c["provider_pref"],
            c["load"],
            -c["fit"],  # negate: stronger routes (higher fit) should rank lower
            c["route_id"],
        )

    def _rank_components_for_display(self, components: dict[str, Any]) -> dict[str, Any]:
        """Transform internal rank_components to consumer-facing format.

        Rank keys come first, in sort order: ``capacity_order``, then
        ``ordering_basis`` and ``slack`` (cheapest sufficient route first),
        then rating fields, then advisory, quota, provider, load, fit,
        route_id. Marginal price follows as information. ``price_known`` and
        ``rating_rank`` (encoded by ``rating_basis``) are omitted.
        """
        price_known = components.get("price_known", False)
        price_value = components.get("price_for_rank", 0.0)
        display = {
            "capacity_order": components["capacity_order"],
            "ordering_basis": components.get("ordering_basis", "cheapest_sufficient_first"),
            "slack": components["slack"],
            "rating_basis": components["rating_basis"],
            "rating": components["rating"],
            "declared_tier": components["declared_tier"],
            "rating_metric": components["rating_metric"],
            "rating_source": components["rating_source"],
            "rating_as_of": components["rating_as_of"],
            "rating_join": components.get("rating_join"),
            "advisory_rank": components["advisory_rank"],
            "advisory_category": components.get("advisory_category"),
            "advisory_omission": components.get("advisory_omission"),
            "advisory_provenance": components.get("advisory_provenance"),
            "quota_rank": components["quota_rank"],
            "quota_percent": components.get("quota_percent"),
            "expected_tokens": components.get("expected_tokens"),
            "provider_pref": components["provider_pref"],
            "load": components["load"],
            "fit": components["fit"],  # stays positive
            "route_id": components["route_id"],
            "price": price_value if price_known else None,
            "probe_class": components.get("probe_class", "none"),
            "evidence_level": components.get("evidence_level"),
            "cache_checked_at": components.get("cache_checked_at"),
            "cache_freshness": components.get("cache_freshness"),
            "session_score": components.get("session_score"),
            "session_passes": components.get("session_passes"),
            "session_fails": components.get("session_fails"),
        }
        if "fallback" in components:
            display["fallback"] = components["fallback"]
            display["fallback_reason"] = components["fallback_reason"]
        if "session_evidence_error" in components:
            display["session_evidence_error"] = components["session_evidence_error"]
        return display

    def _assess_all(
        self, requirements: TaskRequirements, now: datetime
    ) -> tuple[list[_Assessment], list[_Assessment]]:
        """Assess nonopaque routes and rank selectable routes below worker capacity.

        Refresh health and task-family token estimates before assessing routes.
        Missing or unreadable verified history leaves token usage unknown.
        """
        cache = getattr(self, "_health_cache", None)
        if cache is not None and cache.path.exists():
            cache.reload()
        self._current_now = now
        received_at = getattr(self, "_rankings_received_at", None)
        if received_at is not None:
            from verdict.omniroute_rankings import RankingsResult

            if not RankingsResult((), received_at=received_at).is_fresh(now):
                self._rankings = None
                self._rankings_omission = "stale"
        ranked_for = getattr(self, "_rankings_category", None)
        self._rankings_apply = ranked_for is None or ranked_for == requirements.task_category
        from verdict.candidate_pool import expected_token_usage
        from verdict.outcome_shadow import (
            load_verified_outcome_observations,
            resolve_outcome_shadow_roots,
        )

        family = requirements.task_family or ("code_edit" if requirements.coding else "general")
        self._token_usage = {}
        try:
            history = load_verified_outcome_observations(
                resolve_outcome_shadow_roots(None), task_family=family, as_of=now
            )
            self._token_usage = expected_token_usage(history.observations, task_family=family)
        except Exception as exc:
            _LOG.warning("Subscription usage history unavailable: %s", exc)
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
        # Store role ordering in thread-local so _rank_key can read it without a
        # parameter change.  This is safe because _assess_all, its callers, and
        # _rank_key all run on the same thread (BOD-341 uses one thread per select).
        self._select_stats.strongest_first = (
            getattr(requirements, "role_ordering", "cheapest_sufficient_first") == "strongest_first"
        )
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
        stats = dict(getattr(self._select_stats, "value", {}))
        accounting = getattr(self, "_last_accounting", None)
        if accounting:
            stats["selection_accounting"] = accounting
        return stats

    def select(
        self, requirements: TaskRequirements, *, now: datetime
    ) -> tuple[RouteVerdict | None, tuple[RouteVerdict, ...]]:
        """Return a selected route, if any, and verdicts for all assessed routes.

        Apply bounded refresh and confirmation probes before selection, honoring
        admission and cooldowns. Subscription fallback records its reason and
        queues free-route rescreen hints when probing is enabled.
        """
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
        if refresh_hook is not None and candidates:
            # The refresh job may just have written fresh positives into the
            # health cache (a cold VERDICT_HOME, as in CI, starts empty). It
            # writes through its own HealthCache object, so reload this one
            # from disk first, then re-read health so probe ordering sees
            # them. Never upgrade a candidate the refresh marked failed.
            cache = getattr(self, "_health_cache", None)
            if cache is not None and cache.path.exists():
                with contextlib.suppress(Exception):
                    cache.reload()
            upgraded: list[str] = []
            for candidate in candidates:
                if candidate.failed_stage is None:
                    refreshed = self._assess(candidate.route_id, requirements, now)
                    candidate.health = refreshed.health
                    candidate.health_category = refreshed.health_category
                    if refreshed.failed_stage is not None:
                        candidate.failed_stage = refreshed.failed_stage
                        candidate.reason = refreshed.reason
                        candidate.cooldown_until = refreshed.cooldown_until
                        candidate.cooldown_scope = refreshed.cooldown_scope
                    elif candidate.health == "healthy":
                        upgraded.append(candidate.route_id)
            self._last_refresh_upgrades = tuple(upgraded)

        rank_of = {a.route_id: i for i, a in enumerate(candidates)}
        probes_used = 0
        chosen: _Assessment | None = None
        chosen_rank: int | None = None
        blocked: dict[str, str] = {}  # provider -> cooldown_until (set in this select)
        probe_order = self._probe_order(candidates)
        # BOD-297: selection confirmation joins the shared coordinator lease;
        # never probe while another producer owns the job.
        # While another producer holds the refresh lease, skip probes and confirmations but still choose routes with fresh proof.
        health_cache = getattr(self, "_health_cache", None)
        lease_held = False
        if health_cache is not None:
            from verdict.orchestration.verified_refresh import _JobLock

            _lock_path = health_cache.path.parent / "verified-refresh.lock"
            if _JobLock(_lock_path).live():
                lease_held = True
        for a in probe_order:
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
            from verdict.admit_prove_confirm import passport_confirmation
            from verdict.orchestration.health_cache import PERMIT_PROOF_MAX_VALIDITY_SECONDS

            confirmation = passport_confirmation([a.route_id], now=now)
            rows = confirmation["passport"]
            qualified = _parse_iso(str(rows[0].get("qualified_at", ""))) if rows else None
            proof_stale = (
                a.route_id not in confirmation["admitted"]
                or qualified is None
                or (now - qualified).total_seconds() >= PERMIT_PROOF_MAX_VALIDITY_SECONDS
            )
            stale_reason: str | None = None
            if proof_stale and self._read_only:
                # Operator ruling: --no-confirm accepts exact-route proof that is
                # still valid even when older than the permit window.
                proof_stale = a.route_id not in read_only_valid_proof([a.route_id], now=now)
                if proof_stale:
                    stale_reason = _read_only_stale_reason(
                        a.route_id, rows[0] if rows else {}, now=now
                    )
            if a.health != "healthy" or confirming or proof_stale:
                if lease_held:
                    a.reason = "lease_held"
                    a.failed_stage = EligibilityStage.AVAILABLE
                    continue
                if stale_reason is not None:
                    # Same stage as probe_budget_exhausted below (no new rejection
                    # stage); only the reason names why proof is not valid.
                    a.reason = stale_reason
                    continue
                if probes_used >= self._max_probes:
                    a.reason = "probe_budget_exhausted"
                    continue
                probes_used += 1
                result = self._probe(a.route_id)
                self._record_health(a.route_id, result, now, provider=a.provider)
                if confirming:
                    self._record_confirmation(a.route_id, result, now)
                cache = getattr(self, "_health_cache", None)
                if cache is not None:
                    cache.reload()
                    latest = cache.entry(a.route_id)
                    if (
                        result.healthy
                        and latest is not None
                        and not latest.healthy
                        and latest.checked_at >= now
                    ):
                        a.failed_stage, a.reason = EligibilityStage.HEALTHY, "newer_negative"
                        continue
                if not result.healthy:
                    a.health = "unhealthy"
                    a.failed_stage = EligibilityStage.HEALTHY
                    a.reason = result.category
                    entry = self._state["cooldowns"].get(f"route:{a.route_id}")
                    if isinstance(entry, dict):
                        a.cooldown_until = str(entry.get("until"))
                    if (
                        result.category in _PROVIDER_SCOPE_CATEGORIES
                        and not _is_inband_refusal(result)
                        and not key_limit_on_non_free_is_route_scoped(
                            route_id=a.route_id,
                            http_status=getattr(result, "status_code", None),
                            detail=result.error_message or "",
                            free_set=self.openrouter_free_set,
                        )
                    ):
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
        if (
            chosen is not None
            and chosen.capacity == CapacityClass.SUBSCRIPTION
            and not requirements.frontier_worthy
        ):
            cache = getattr(self, "_health_cache", None)
            if cache is not None and self._max_probes > 0:
                with contextlib.suppress(Exception):
                    cache.request_rescreen(
                        [a.route_id for a in assessments if a.capacity == CapacityClass.FREE],
                        now=now,
                    )
            components[chosen.route_id]["fallback"] = "subscription"
            components[chosen.route_id]["fallback_reason"] = (
                "free attempts failed"
                if any(a.capacity == CapacityClass.FREE for a in candidates)
                else "no free route qualifies"
            )
        verdicts: list[RouteVerdict] = []
        selected: RouteVerdict | None = None
        accounting = selection_accounting(
            [
                {
                    "route_id": a.route_id,
                    "capacity_class": a.capacity,
                    "failed_stage": a.failed_stage.value if a.failed_stage else None,
                    "reached": a.reached.value if a.reached else None,
                    "reason": a.reason,
                }
                for a in assessments
            ]
        )
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
        # Carry the full classification through the existing candidate-event
        # path, including when no route is selected or candidate rows are capped.
        from dataclasses import replace

        accounting = selection_accounting([v.to_dict() for v in verdicts])
        verdicts = [
            replace(
                v, rank_components={**(v.rank_components or {}), "selection_accounting": accounting}
            )
            for v in verdicts
        ]
        if selected is not None:
            selected = next(v for v in verdicts if v.route_id == selected.route_id)
        self._last_accounting = accounting
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
        self._select_stats.value = {
            "probed": probes_used,
            "stale": stale,
            "healthy_after_probe": healthy_after,
            "eligible_after_probe": eligible_after,
        }
        return selected, final_verdicts

    def _probe_order(self, candidates: list[_Assessment]) -> list[_Assessment]:
        """Probe order mirrors the main rank key (role-dependent ordering).

        For cheapest-sufficient-first roles (workers): slack before rating,
        ``[:4]`` captures (capacity, slack, tokens_known, tokens_float).
        For strongest-first roles (planner/reviewer): rating before slack,
        ``[:4]`` captures (capacity, tokens_known, tokens_float, rating_rank).
        Within an equal ranking, fresh confirmations and provider round-robin
        retain the bounded scheduler.
        """
        order: list[_Assessment] = []
        by_provider: dict[str, list[_Assessment]] = {}
        for a in candidates:  # already rank-sorted; dicts keep first-seen order
            by_provider.setdefault(a.provider, []).append(a)
        # Fresh cache evidence changes confirmation scheduling, never
        # launch authority. These routes still pass the exact live probe.
        for fresh in (True, False):
            queues = [
                [a for a in queue if (a.health == "healthy") == fresh]
                for queue in by_provider.values()
            ]
            depth = max((len(q) for q in queues), default=0)
            for i in range(depth):
                order.extend(q[i] for q in queues if i < len(q))
        return sorted(
            order,
            key=lambda a: (
                *self._rank_key(a)[:4],
                *(rating_key(a.rating) if a.rating is not None else (3, 0.0, 0)),
            ),
        )

    def _record_health(
        self, route_id: str, result: HealthResult, now: datetime, *, provider: str = ""
    ) -> None:
        from verdict.admit_prove_confirm import record_dispatch_observation

        # Reclassify catalog-stale errors: OmniRoute returns bad_request with
        # a specific message for ids not in the live catalog.
        category = result.category
        if (
            not result.healthy
            and category in ("bad_request", "unservable", "unsupported")
            and is_catalog_stale_error(result.error_message)
        ):
            category = "catalog_stale"
        cache = getattr(self, "_health_cache", None)
        record_dispatch_observation(
            route_id,
            healthy=result.healthy,
            observed_at=now,
            failure_category=category,
            retry_after_seconds=result.retry_after_seconds,
            health_cache_path=cache.path if cache is not None else None,
            liveness_only=True,
        )
        with self._persist_lock:
            self._state["health"][route_id] = {
                "healthy": result.healthy,
                "category": category,
                "checked_at": _iso(now),
            }
            if not result.healthy:
                seconds = cooldown_seconds_for(category, result.retry_after_seconds)
                entry = {"until": _iso(now + timedelta(seconds=seconds)), "category": category}
                self._state["cooldowns"][f"route:{route_id}"] = dict(entry)
                route_scoped_key_limit = _is_inband_refusal(
                    result
                ) or key_limit_on_non_free_is_route_scoped(
                    route_id=route_id,
                    http_status=getattr(result, "status_code", None),
                    detail=result.error_message or "",
                    free_set=self.openrouter_free_set,
                )
                # A paid OpenRouter route outside the public 0/0 set is
                # route-scoped for BOTH cooldowns and not-free overrides, even
                # when its id carries a misleading ``:free`` suffix that maps
                # it into the shared ``openrouter-free`` pool (BOD-418).
                route_scoped_openrouter = route_scoped_key_limit or (
                    is_openrouter_catalog_route(route_id)
                    and self.openrouter_free_set is not None
                    and not openrouter_route_is_public_free(route_id, self.openrouter_free_set)
                )
                if (
                    provider
                    and category in _PROVIDER_SCOPE_CATEGORIES
                    and not route_scoped_key_limit
                ):
                    self._state["cooldowns"][f"pool:{pool_of(route_id)}"] = dict(entry)
                    self._state["cooldowns"][f"provider:{provider}"] = dict(entry)
                # Live-evidence not-free override: mark the route and pool so
                # capacity classification reflects the override (design §B, item 3).
                # A paid OpenRouter key-limit 403 on a route positively outside the
                # public 0/0 set is route-scoped: it must not write a pool override
                # that would exclude genuine free siblings (BOD-418).
                if is_not_free_signal(
                    category=category, status_code=getattr(result, "status_code", None)
                ):
                    pool = backend_pool(route_id)
                    until = _iso(
                        now
                        + timedelta(
                            seconds=cooldown_seconds_for(category, result.retry_after_seconds)
                        )
                    )
                    record_not_free_override(
                        self._state,
                        route_id,
                        pool=pool,
                        until_iso=until,
                        reason=category,
                        include_pool=not route_scoped_openrouter,
                    )
            self._persist()

    def record_failure(
        self, route_id: str, failure: FailureClassification, *, now: datetime
    ) -> None:
        with self._persist_lock:
            retry_after = failure.cooldown_seconds if failure.cooldown_seconds > 0 else None
            seconds = cooldown_seconds_for(failure.category, retry_after)
            entry = {"until": _iso(now + timedelta(seconds=seconds)), "category": failure.category}
            if failure.scope in {"route", "provider"}:
                # Only credential-pool exhaustion or account failures cool the whole
                # pool (SH-1). A bad model id, a 400 or a timeout is about this route
                # and must not block healthy sibling models on the same credential.
                if failure.scope == "provider" and failure.category in POOL_COOLDOWN_CATEGORIES:
                    self._state["cooldowns"][f"pool:{pool_of(route_id)}"] = dict(entry)
                self._state["cooldowns"][f"route:{route_id}"] = dict(entry)
            if failure.scope == "provider" and failure.category not in POOL_COOLDOWN_CATEGORIES:
                # Credential failures already have a canonical pool blocker above.
                # A second provider blocker would spread a FREE-pool failure onto
                # unrelated pools owned by the same provider (e.g. OpenRouter).
                raw_provider = str(self._rows.get(route_id, {}).get("owned_by", "")).lower()
                provider = resolve_provider(raw_provider) if raw_provider else raw_provider
                if not provider:
                    provider = route_id.split("/", 1)[0].lower()
                self._state["cooldowns"][f"provider:{provider}"] = dict(entry)
            self._persist()

    def record_session_failure(
        self,
        route_id: str,
        failure: FailureClassification,
        terminal: WorkerTerminal,
        *,
        now: datetime,
        provenance: str,
    ) -> Any:
        """Project real session failure into the same cache subsequent runs read."""
        from verdict.orchestration.session_feedback import record_worker_failure

        cache = getattr(self, "_health_cache", None)
        if cache is None:
            return None
        raw_provider = str(self._rows.get(route_id, {}).get("owned_by", "")).lower()
        provider = resolve_provider(raw_provider) if raw_provider else route_id.split("/", 1)[0]
        return record_worker_failure(
            cache,
            failure=failure,
            terminal=terminal,
            route_id=route_id,
            provider_id=provider,
            pool=pool_of(route_id),
            now=now,
            provenance=provenance,
        )

    def record_success(self, route_id: str, *, now: datetime) -> None:
        from verdict.admit_prove_confirm import record_dispatch_observation

        record_dispatch_observation(route_id, healthy=True, observed_at=now)
        with self._persist_lock:
            pool_key = f"pool:{pool_of(route_id)}"
            entry = self._state["cooldowns"].get(pool_key)
            if not (
                isinstance(entry, dict)
                and entry.get("category") in POOL_COOLDOWN_CATEGORIES
                and self._active_cooldown(pool_key, now) is not None
            ):
                self._state["cooldowns"].pop(pool_key, None)
            # Exact-route success supersedes a model-local throttle, not pool capacity.
            self._state["cooldowns"].pop(f"route:{route_id}", None)
            self._state["health"][route_id] = {
                "healthy": True,
                "category": "",
                "checked_at": _iso(now),
            }
            self._persist()

    def effort_model_rows(self) -> dict[str, dict[str, Any]]:
        """Allowlisted effort facts from inventory already fetched for selection.

        Production callers use this instead of reading private rows. Price,
        secrets and other inventory fields are not included.
        """
        from verdict.orchestration.effort import allowlisted_effort_facts

        return {
            route_id: allowlisted_effort_facts(row)
            for route_id, row in self._rows.items()
            if route_id
        }

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


def _declared_tier(row: Mapping[str, Any]) -> int | None:
    """Capability tier the inventory row declares; never derived from the id."""
    declared = row.get("capability_tier")
    if isinstance(declared, int) and not isinstance(declared, bool) and 0 <= declared <= 3:
        return declared
    return None


def _capability_tier(row: Mapping[str, Any], route_id: str) -> int | None:
    """Declared capability tier from inventory metadata, else the id classifier.

    Used for the sufficiency gate and the slack tie-break only, never as a
    rating. ``None`` means unknown: neither the inventory nor the classifier knows it.
    """
    declared = _declared_tier(row)
    if declared is not None:
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

    Marginal price is informational, not a ranking key. Receipts still
    distinguish "known and free" from "no pricing metadata"; the
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


_classify_capacity = classify_route_capacity


def capacity_class_of(
    conn: Mapping[str, Any] | None,
    row: Mapping[str, Any],
    *,
    route_id: str | None = None,
    openrouter_free_set: OpenRouterFreeSet | None = None,
) -> tuple[CapacityClass, str]:
    """Use the same D7 account-evidence policy as the eligibility ladder."""
    capacity, plan, _ = _classify_capacity(
        conn,
        row,
        route_id=route_id or str(row.get("id") or ""),
        openrouter_free_set=openrouter_free_set,
    )
    return capacity, plan
