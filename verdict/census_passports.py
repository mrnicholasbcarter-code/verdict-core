"""Budgeted free-route census using existing health and passport stores."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from verdict.orchestration.health_cache import STATE_FRESH, HealthCache
from verdict.probes import ProbeObservation
from verdict.prove_at_rest_legacy import (
    STATUS_HEALTHY,
    ProofResult,
    ProveAtRestCycle,
    ProveAtRestStore,
    load_healthy_passports,
    passport_from_probe,
)


def passive_refresh(
    cache: HealthCache,
    max_passports: int,
    passport_store_path: Path,
    now: datetime,
    eligible_routes: set[str] | None = None,
) -> dict[str, int]:
    """Copy only unexpired successful free chat/tool evidence, never extend its age."""
    if max_passports < 1:
        raise ValueError("max_passports must be positive")
    store = ProveAtRestStore(path=passport_store_path)
    passports = load_healthy_passports(passport_store_path)
    eligible = []
    skipped_fresh = 0
    for route, entry in cache.routes().items():
        if eligible_routes is not None and route not in eligible_routes:
            continue
        if (
            entry.state_at(now) != STATE_FRESH
            or not entry.chat_ok
            or not entry.tool_ok
            or entry.capacity_evidence != "free"
        ):
            continue
        passport = passport_from_probe(
            provider=route.split("/", 1)[0],
            identity_id=route,
            observation=ProbeObservation(
                model_id=route,
                availability_state="ready",
                status="ok",
                observed_at=entry.checked_at,
                latency_ms=entry.latency_ms,
            ),
        )
        if passport.expires_at <= now:
            continue
        # A newer health proof renews the passport; an equal or older one does not.
        if route in passports and passports[route].expires_at >= passport.expires_at:
            skipped_fresh += 1
            continue
        eligible.append(
            ProofResult(
                identity_id=route,
                provider=passport.provider,
                status=STATUS_HEALTHY,
                proved_at=entry.checked_at,
                latency_ms=entry.latency_ms,
                passport=passport,
            )
        )
    selected = eligible[:max_passports]
    if selected:
        cycle = store.read() or ProveAtRestCycle(
            cycle_id="census", started_at=now, finished_at=now, admitted=(), results=()
        )
        by_id = {row.identity_id: row for row in cycle.results}
        by_id.update({row.identity_id: row for row in selected})
        store.write(replace(cycle, results=tuple(by_id.values()), finished_at=now))
    return {
        "updated": len(selected),
        "skipped_budget": len(eligible) - len(selected),
        "skipped_fresh": skipped_fresh,
    }


_log = logging.getLogger(__name__)
DEFAULT_MAX_PER_PROVIDER = 2
PASSIVE_EVIDENCE_MAX_AGE_SECONDS = 30 * 60
# Only evidence with an explicit success count (>=1 non-stream 2xx in the
# window, or a provider-stats / call-logs?excludeTests=1 success count) may
# renew a passport. OmniRoute 3.8.51 latency stats ("passive_success") are a
# 24-168h aggregate with no such count, so they never qualify: no producer of
# this category exists yet and those routes are left to the probe.
RECENT_SUCCESS_CATEGORY = "recent_success"
# Admit/health surface -> evidence source marker; stale surfaces renew nothing.
_SURFACE_SOURCE_MARKERS = {
    "latency_stats": "model-latency-stats",
    "connections": "resilience/connections",
    "provider_stats": "provider-stats",
    "call_logs": "call-logs",
    "providers": "/api/providers",
}


def _from_stale_surface(source: str, stale_served: Any) -> bool:
    return any(
        surface in source or _SURFACE_SOURCE_MARKERS.get(surface, "\0") in source
        for surface in stale_served or ()
    )


def refresh_from_passive_evidence(
    snapshot: Any,
    passport_store_path: Path,
    now: datetime,
    eligible_routes: set[str],
    *,
    max_age_seconds: float = PASSIVE_EVIDENCE_MAX_AGE_SECONDS,
) -> set[str]:
    """Renew basic liveness passports from recent counted success before probing.

    Only ``RECENT_SUCCESS_CATEGORY`` evidence (an explicit success count) that
    is healthy, unexpired and observed within ``max_age_seconds`` qualifies;
    latency-only evidence never does. Evidence from a surface listed in
    ``snapshot.stale_served`` renews nothing. The renewed passport is liveness
    only (``tool_support=False``) and keeps the observation time. A route
    holding an agentic/tool-capability pass is never touched: that pass keeps
    its own TTL and only an agentic probe renews it.
    """
    from verdict.admission import RuntimeEvidence

    store = ProveAtRestStore(path=passport_store_path)
    existing = load_healthy_passports(passport_store_path)
    stale_served = getattr(snapshot, "stale_served", ())
    results: dict[str, ProofResult] = {}
    for row in (snapshot.passive_health or RuntimeEvidence()).observations:
        route = row.key.removeprefix("route:")
        if not row.key.startswith("route:") or route not in eligible_routes:
            continue
        if row.category != RECENT_SUCCESS_CATEGORY or _from_stale_surface(row.source, stale_served):
            continue
        if route in existing and existing[route].tool_support:
            continue
        if row.state != "healthy" or row.until is None or row.observed_at is None:
            continue
        try:
            observed = datetime.fromisoformat(row.observed_at)
            until = datetime.fromisoformat(row.until)
        except (TypeError, ValueError):
            continue
        age = (now - observed).total_seconds()
        if until <= now or not 0 <= age <= max_age_seconds:
            continue
        passport = passport_from_probe(
            provider=route.split("/", 1)[0],
            identity_id=route,
            observation=ProbeObservation(
                model_id=route, availability_state="ready", status="ok", observed_at=observed
            ),
        )
        passport = replace(passport, tool_support=False)
        if passport.expires_at <= now:
            continue
        results[route] = ProofResult(
            identity_id=route,
            provider=passport.provider,
            status=STATUS_HEALTHY,
            proved_at=observed,
            latency_ms=None,
            passport=passport,
        )
    if results:
        cycle = store.read() or ProveAtRestCycle(
            cycle_id="census", started_at=now, finished_at=now, admitted=(), results=()
        )
        by_id = {row.identity_id: row for row in cycle.results}
        by_id.update(results)
        store.write(replace(cycle, results=tuple(by_id.values()), finished_at=now))
    return set(results)


def run_census(
    snapshot: Any,
    transport: Any,
    *,
    cache: HealthCache,
    passport_store_path: Path,
    now: datetime,
    max_candidates: int | None = None,
    max_tokens: int | None = None,
    max_per_provider: int | None = DEFAULT_MAX_PER_PROVIDER,
    cooling_providers: frozenset[str] = frozenset(),
    role_routes: frozenset[str] | None = None,
    tool_probe_interval_s: int | None = None,
    context_probe_max_per_tick: int = 1,
    context_prober: Any = None,
    context_store_path: Path | None = None,
) -> dict[str, Any]:
    """Refresh free, connected routes, preferring recent successful traffic.

    Pipeline additions, dark unless ``VERDICT_PIPELINE_PROBES`` is set: proven
    routes are re-probed (chat+tool) at least every ``tool_probe_interval_s``,
    ToS-avoid/blocklisted prefixes are never probed, and at most
    ``context_probe_max_per_tick`` one-step needle probes run on proven routes,
    all inside the per-provider budget. ``VERDICT_PIPELINE_STATE`` adds the
    per-model state summary and FAIL_FAKE provider blocklist to the report.

    ``role_routes`` are routes whose catalog capabilities meet the worker and
    reviewer requirements; they are kept fresh before other proven routes.

    Each full probe reserves 256 tokens for the fixed chat/tool prompts and
    their capped outputs (8 + 64). Budgets are independent: ``max_per_provider``
    bounds probes per provider family, and the optional ``max_candidates``
    (requests <= 2*N) and ``max_tokens`` caps each apply on their own.
    Routes with recent passive gateway success are renewed before any probe.
    """
    from verdict.admission import RuntimeEvidence
    from verdict.free_tier_admit import admit_free_tier_active
    from verdict.prove_at_rest import AdmittedRoute, probe_full

    if any(b is not None and b < 1 for b in (max_candidates, max_tokens, max_per_provider)):
        raise ValueError("census budgets must be positive")
    if context_probe_max_per_tick < 0 or (
        tool_probe_interval_s is not None and tool_probe_interval_s < 1
    ):
        raise ValueError("census pipeline budgets must be positive")
    from verdict.omniroute_pipeline.probes import probe_allowed, probes_enabled
    from verdict.omniroute_pipeline.state import (
        ModelState,
        ProviderBlocklist,
        state_enabled,
        transition,
    )

    pipeline_probes = probes_enabled()
    pipeline_state = state_enabled()
    receipt = admit_free_tier_active(snapshot)
    healthy = {
        row.key.removeprefix("route:")
        for row in (snapshot.passive_health or RuntimeEvidence()).observations
        if row.key.startswith("route:")
        and row.state == "healthy"
        and row.until is not None
        and datetime.fromisoformat(row.until) > now
    }
    from verdict.orchestration.credential_pools import canonical_route

    healthy_canonical = {canonical_route(r) for r in healthy} | healthy
    cooling = {canonical_route(f"{provider}/_").split("/", 1)[0] for provider in cooling_providers}
    from verdict.census_passports_service import CENSUS_INTERVAL_SECONDS
    from verdict.orchestration.health_cache import STATE_NEGATIVE

    routes = cache.routes()

    def proven(route: str) -> bool:
        entry = routes.get(route)
        return entry is not None and entry.healthy and entry.chat_ok and entry.tool_ok

    from verdict.orchestration.credential_pools import is_non_chat_route

    non_chat = [r for r in receipt.free_admitted if is_non_chat_route(r)]
    eligible = [
        r
        for r in receipt.free_admitted
        if r not in non_chat
        and canonical_route(r).split("/", 1)[0] not in cooling
        and cache.lookup(r, now).state != STATE_NEGATIVE
        and (not pipeline_probes or probe_allowed(r, free_status="free"))
    ]

    roles = role_routes or frozenset()

    def tier(route: str) -> int:
        # Keep routes that already passed a census probe alive first, then
        # gateway-observed healthy traffic, then routes never proven.
        if proven(route):
            return 0
        if route in healthy or canonical_route(route) in healthy_canonical:
            return 1
        return 2

    def rank_key(route: str) -> tuple[int, bool]:
        # Worker/reviewer-capable routes lead their tier (BOD-387).
        return tier(route), route not in roles

    # Within a tier, take one route per family before a second from any
    # family, so the budget keeps several families fresh at once.
    family_rank: dict[str, int] = {}
    seen: dict[tuple[int, bool, str], int] = {}
    for route in sorted(eligible, key=lambda r: (rank_key(r), r)):
        slot = (*rank_key(route), route.split("/", 1)[0])
        family_rank[route] = seen.get(slot, 0)
        seen[slot] = family_rank[route] + 1
    candidates = sorted(eligible, key=lambda r: (rank_key(r), family_rank[r], r))
    refresh_cap = max_candidates if max_candidates is not None else max(1, len(candidates))
    passive = refresh_from_passive_evidence(snapshot, passport_store_path, now, set(candidates))
    refreshed = passive_refresh(cache, refresh_cap, passport_store_path, now, set(candidates))
    passports = load_healthy_passports(passport_store_path)
    # Renew any passport that would lapse before the tick after next can
    # prove it again, so a slow or late tick never leaves a gap.
    renew_window = 2 * CENSUS_INTERVAL_SECONDS
    if pipeline_probes and tool_probe_interval_s is not None:
        renew_window = max(renew_window, tool_probe_interval_s)
    renew_before = now + timedelta(seconds=renew_window)
    pending = [
        model
        for model in candidates
        if model not in passive
        and (model not in passports or passports[model].expires_at <= renew_before)
    ]
    families = {canonical_route(model).split("/", 1)[0] for model in candidates}
    probe_budget = len(families) * max_per_provider if max_per_provider is not None else None
    stale_served = list(getattr(snapshot, "stale_served", ()))
    _log.info(
        "census probe budget: %s (%d providers x max_per_provider=%s); stale_served=%s",
        probe_budget,
        len(families),
        max_per_provider,
        stale_served,
    )
    if stale_served:
        _log.warning("census tick uses stale admit surfaces: %s", ", ".join(stale_served))
    if max_per_provider is not None:
        per_family: dict[str, int] = {}
        budgeted = []
        for model in pending:
            family = canonical_route(model).split("/", 1)[0]
            if per_family.get(family, 0) < max_per_provider:
                per_family[family] = per_family.get(family, 0) + 1
                budgeted.append(model)
        pending = budgeted
    caps = [len(pending)]
    if max_candidates is not None:
        caps.append(max_candidates - refreshed["updated"])
    if max_tokens is not None:
        caps.append(max_tokens // 256)
    limit = max(0, min(caps))
    # One probe in three explores routes the census has not proven, so proven
    # routes can never starve discovery. The explored route rotates per tick.
    keep = [model for model in pending if tier(model) == 0]
    explore = [model for model in pending if tier(model) != 0]
    reserved: list[str] = []
    if explore and limit >= 3:
        start = int(now.timestamp() // CENSUS_INTERVAL_SECONDS) % len(explore)
        reserved = (explore[start:] + explore[:start])[: limit // 3]
    chosen = keep[: limit - len(reserved)] + reserved
    chosen += [model for model in explore if model not in chosen][: limit - len(chosen)]
    probed = 0
    states: dict[str, ModelState] = {}
    blocklist = ProviderBlocklist()
    family_used: dict[str, int] = {}
    for model in chosen:
        outcome = probe_full(
            AdmittedRoute(model, model.split("/", 1)[0], "free", capacity_evidence="free"),
            transport,
            timeout_seconds=15,
        )
        probed += 1
        family = canonical_route(model).split("/", 1)[0]
        family_used[family] = family_used.get(family, 0) + 1
        if outcome.result is not None:
            cache.record(model, outcome.result, now)
        if pipeline_state:
            states[model] = _pipeline_state(model, outcome, blocklist, transition, ModelState)
    context_probed = 0
    if pipeline_probes and context_prober is not None and context_probe_max_per_tick > 0:
        from verdict.omniroute_pipeline.census_hook import run_context_probes
        from verdict.omniroute_pipeline.context_store import default_context_store_path

        context_probed = run_context_probes(
            candidates,
            proven=proven,
            family_used=family_used,
            max_per_provider=max_per_provider,
            max_per_tick=context_probe_max_per_tick,
            prober=context_prober,
            store_path=context_store_path or default_context_store_path(),
            now=now,
            states=states,
            blocklist=blocklist,
        )
    cache.save()
    after = passive_refresh(cache, refresh_cap, passport_store_path, now, set(candidates))
    after["updated"] += refreshed["updated"] + len(passive)
    return {
        **after,
        "passive_refreshed": len(passive),
        "probe_budget": probe_budget,
        "stale_served": stale_served,
        "non_chat_skipped": len(non_chat),
        "candidates": len(candidates),
        "probed": probed,
        "reserved_tokens": probed * 256,
        **({"context_probed": context_probed} if pipeline_probes else {}),
        **(
            {
                "pipeline_states": {m: st.value for m, st in sorted(states.items())},
                "blocklisted_providers": dict(sorted(blocklist.entries.items())),
            }
            if pipeline_state
            else {}
        ),
    }


def _pipeline_state(
    model: str, outcome: Any, blocklist: Any, transition: Any, model_state: Any
) -> Any:
    """Map one census probe onto the pipeline state machine (FAIL_FAKE blocklists)."""
    from verdict.omniroute_pipeline.probes import ProbeOutcome

    status = getattr(outcome, "http_status", None)
    category = getattr(outcome, "category", None) or getattr(outcome, "nowrite", None)
    result = getattr(outcome, "result", None)
    if status == 402 and "fake" in str(getattr(outcome, "detail", "") or category or ""):
        probe = ProbeOutcome.FAIL_FAKE
    elif status in (401, 402, 404, 410, 429):
        probe = None
    elif result is not None and getattr(result, "tool_ok", False):
        return model_state.TOOL_OK
    else:
        probe = ProbeOutcome.FAIL if result is not None else None
    return transition(
        model_state.CATALOGED, http_status=status, probe=probe, route=model, blocklist=blocklist
    )


def load_census_cooldowns(
    base_url: str, api_key: str | None = None, *, transport: Any = None
) -> frozenset[str]:
    """Avoid spending probes on providers with an active gateway cooldown."""
    import httpx

    from verdict.free_tier_admit import normalize_omniroute_origin
    from verdict.omniroute import _records

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    with httpx.Client(transport=transport, timeout=10, follow_redirects=False) as client:
        response = client.get(
            f"{normalize_omniroute_origin(base_url)}/api/resilience/model-cooldowns",
            headers=headers,
        )
        response.raise_for_status()
        payload = response.json()
    # Same payload-shape tolerance as the passive-health cooldown merge:
    # unknown keys or non-mapping rows mean "no cooldown hints", never a crash.
    return frozenset(
        row["provider"]
        for row in _records(payload, "items", "lockouts", "cooldowns", "data")
        if isinstance(row.get("provider"), str)
        and isinstance(row.get("remainingMs"), (int, float))
        and not isinstance(row.get("remainingMs"), bool)
        and row["remainingMs"] > 0
    )


def _worker_capable_routes(base_url: str, api_key: str | None) -> frozenset[str]:
    """Catalog routes with the capabilities worker and reviewer roles require.

    Reads the gated census catalog the admit snapshot just cached; no extra
    gateway call.
    """
    from verdict.availability import normalize_catalog
    from verdict.catalog_cache import get_cached_catalog
    from verdict.free_tier_admit import (
        default_census_catalog_cache_path,
        normalize_omniroute_origin,
    )
    from verdict.subagent_models import SUBAGENT_ROLE_REQUIREMENTS

    catalog = get_cached_catalog(
        normalize_omniroute_origin(base_url),
        api_key,
        cache_path=default_census_catalog_cache_path(),
    )
    required = (
        SUBAGENT_ROLE_REQUIREMENTS["worker"].required
        | SUBAGENT_ROLE_REQUIREMENTS["reviewer"].required
    )
    return frozenset(
        model.id for model in normalize_catalog(catalog) if required <= set(model.capabilities)
    )


def cmd_census(
    *,
    max_candidates: int | None = None,
    max_tokens: int | None = None,
    max_per_provider: int | None = DEFAULT_MAX_PER_PROVIDER,
    passport_store_path: Path | None = None,
    health_cache_path: Path | None = None,
    output_json: bool = False,
    install: bool = False,
    dry_run: bool = False,
    tool_probe_interval_s: int | None = None,
    context_probe_max_per_tick: int = 1,
) -> None:
    import json

    from verdict.free_tier_admit import load_omniroute_admit_snapshot, omniroute_endpoint_from_env
    from verdict.orchestration.health_cache import default_cache_path
    from verdict.prove_at_rest import default_state_path, live_transport

    if install:
        from verdict.census_passports_service import manage_census_service

        report = manage_census_service(dry_run=dry_run)
    else:
        if dry_run:
            raise ValueError("--dry-run requires --install")
        endpoint = omniroute_endpoint_from_env()
        if endpoint is None:
            raise ValueError("OmniRoute endpoint is not configured")
        base_url = endpoint[0]
        from verdict.omniroute_pipeline.census_hook import census_probe_key

        # One resolver path: the census probe key file only (never an env key).
        key = census_probe_key()
        if key is None:
            raise ValueError("census probe key file ~/.config/verdict/omniroute-api-key is missing")
        snapshot = load_omniroute_admit_snapshot(base_url, api_key=key, catalog_scope="gated")
        role_routes = _worker_capable_routes(base_url, key)
        # A cooldown-endpoint outage must not kill the timer's census run; the
        # census then just spends its probe budget without cooldown hints.
        import httpx

        # ValueError covers an invalid-JSON 200 body; none of these may kill it.
        try:
            cooling = load_census_cooldowns(base_url, key)
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            cooling = frozenset()
        from verdict.omniroute_pipeline.census_hook import guard_transport, needle_prober
        from verdict.omniroute_pipeline.probes import probes_enabled

        report = run_census(
            snapshot,
            guard_transport(live_transport(base_url, api_key=key)),
            cache=HealthCache(health_cache_path or default_cache_path()),
            passport_store_path=passport_store_path or default_state_path(),
            now=datetime.now(timezone.utc),
            max_candidates=max_candidates,
            max_tokens=max_tokens,
            max_per_provider=max_per_provider,
            cooling_providers=cooling,
            role_routes=role_routes,
            tool_probe_interval_s=tool_probe_interval_s,
            context_probe_max_per_tick=context_probe_max_per_tick,
            context_prober=needle_prober(base_url, key) if probes_enabled() else None,
        )
        from verdict.omniroute_pipeline.census_hook import free_rule_tick

        # Dark unless VERDICT_PIPELINE_FREE_RULE is set: public catalogs only.
        free_rule = free_rule_tick(snapshot, now=datetime.now(timezone.utc))
        if free_rule is not None:
            report["free_rule"] = free_rule
        from verdict.census_passports_service import write_census_last_result

        write_census_last_result(report)
    print(json.dumps(report, indent=2, sort_keys=True))
