"""Budgeted free-route census using existing health and passport stores."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
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
        if route in passports and passports[route].expires_at > now:
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


def run_census(
    snapshot: Any,
    transport: Any,
    *,
    cache: HealthCache,
    passport_store_path: Path,
    now: datetime,
    max_candidates: int = 6,
    max_tokens: int = 1536,
    cooling_providers: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Refresh free, connected routes, preferring recent successful traffic.

    Each full probe reserves 256 tokens for the fixed chat/tool prompts and
    their capped outputs (8 + 64). The candidate cap bounds requests to 2*N.
    """
    from verdict.admission import RuntimeEvidence
    from verdict.free_tier_admit import admit_free_tier_active
    from verdict.prove_at_rest import AdmittedRoute, probe_full

    if max_candidates < 1 or max_tokens < 1:
        raise ValueError("census budgets must be positive")
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
    from verdict.orchestration.health_cache import STATE_NEGATIVE

    candidates = sorted(
        (
            r
            for r in receipt.free_admitted
            if canonical_route(r).split("/", 1)[0] not in cooling
            and cache.lookup(r, now).state != STATE_NEGATIVE
        ),
        key=lambda r: (r not in healthy and canonical_route(r) not in healthy_canonical, r),
    )
    refreshed = passive_refresh(cache, max_candidates, passport_store_path, now, set(candidates))
    passports = load_healthy_passports(passport_store_path)
    pending = [
        model
        for model in candidates
        if model not in passports or passports[model].expires_at <= now
    ]
    limit = min(max_candidates - refreshed["updated"], max_tokens // 256)
    probed = 0
    for model in pending[:limit]:
        outcome = probe_full(
            AdmittedRoute(model, model.split("/", 1)[0], "free", capacity_evidence="free"),
            transport,
            timeout_seconds=15,
        )
        probed += 1
        if outcome.result is not None:
            cache.record(model, outcome.result, now)
    cache.save()
    after = passive_refresh(cache, max_candidates, passport_store_path, now, set(candidates))
    after["updated"] += refreshed["updated"]
    return {
        **after,
        "candidates": len(candidates),
        "probed": probed,
        "reserved_tokens": probed * 256,
    }


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


def cmd_census(
    *,
    max_candidates: int = 6,
    max_tokens: int = 1536,
    passport_store_path: Path | None = None,
    health_cache_path: Path | None = None,
    output_json: bool = False,
    install: bool = False,
    dry_run: bool = False,
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
        base_url, key = endpoint
        key_path = Path.home() / ".config/verdict/omniroute-api-key"
        if key_path.is_file():
            key = key_path.read_text().strip()
        snapshot = load_omniroute_admit_snapshot(base_url, api_key=key)
        # A cooldown-endpoint outage must not kill the timer's census run; the
        # census then just spends its probe budget without cooldown hints.
        import httpx

        # ValueError covers an invalid-JSON 200 body; none of these may kill it.
        try:
            cooling = load_census_cooldowns(base_url, key)
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            cooling = frozenset()
        report = run_census(
            snapshot,
            live_transport(base_url, api_key=key),
            cache=HealthCache(health_cache_path or default_cache_path()),
            passport_store_path=passport_store_path or default_state_path(),
            now=datetime.now(timezone.utc),
            max_candidates=max_candidates,
            max_tokens=max_tokens,
            cooling_providers=cooling,
        )
        from verdict.census_passports_service import write_census_last_result

        write_census_last_result(report)
    print(json.dumps(report, indent=2, sort_keys=True))
