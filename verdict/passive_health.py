"""Fold OmniRoute real-traffic evidence into the health cache as chat-only liveness.

Observed traffic proves a route answered real requests; it never proves tool use
or agentic work, so recorded entries keep ``tool_ok`` false. No model calls and
no gateway writes happen here: the evidence is already fetched.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from datetime import datetime
from typing import Any, Protocol

from verdict.omniroute_evidence import OmniRouteEvidence
from verdict.orchestration.credential_pools import pool_of
from verdict.orchestration.health_cache import (
    EVIDENCE_SOURCE_TRAFFIC,
    EVIDENCE_SOURCE_WEBHOOK,
    HealthCache,
    format_datetime,
)
from verdict.relay import _RELAY_FEEDBACK_CATEGORIES, failure_class

SOURCE = "omniroute:health-matrix"
# Without these sources, traffic, provider usability or cooldowns are unknown.
_REQUIRED_SOURCES = ("health_matrix", "providers", "resilience", "cooldowns")


class TrafficEvent(Protocol):
    @property
    def event(self) -> str: ...
    @property
    def timestamp(self) -> datetime: ...
    @property
    def provider(self) -> str | None: ...
    @property
    def model(self) -> str | None: ...
    @property
    def latency_ms(self) -> float | None: ...


def _ok_status(status: int | None) -> bool:
    return status is None or 200 <= status < 400


def _event_routes(evidence: OmniRouteEvidence, provider: str, model: str) -> set[str]:
    """The exact catalog id and every catalog alias of the same (provider, root)."""
    exact = {model} if model in evidence.inventory_route_ids else set()
    roots = {
        model,
        *(identity[1] for r, identity in evidence.route_identities.items() if r == model),
    }
    return exact | {
        route
        for route, identity in evidence.route_identities.items()
        if identity[0] == provider and identity[1] in roots
    }


def refresh_health_from_traffic(
    evidence: OmniRouteEvidence,
    cache: HealthCache,
    *,
    now: datetime,
    webhook_events: Iterable[TrafficEvent] = (),
) -> dict[str, Any]:
    """Record observed successes as liveness and 402/5xx traffic as route negatives.

    A failed required source writes nothing. Auth and request errors stay out of
    the cache, the same rule the relay applies to its own failures.
    """
    report: dict[str, Any] = {
        "source": SOURCE,
        "refreshed_at": format_datetime(now),
        "recorded": 0,
        "recorded_failures": 0,
        "skipped": {},
        "newest_observed_at": None,
        "oldest_observed_at": None,
        "webhook_events_used": 0,
        "webhook_events_unattributed": 0,
        "tool_proof_granted": 0,
        "model_calls": 0,
        "gateway_writes": 0,
        "source_errors": list(evidence.source_errors),
    }
    if set(evidence.source_errors) & set(_REQUIRED_SOURCES):
        return report

    skipped: Counter[str] = Counter()
    # Newest positive observation per route: (observed_at, source, latency_ms).
    positives: dict[str, tuple[datetime, str, float | None]] = {}

    def admissible(route: str, provider: str) -> bool:
        if provider not in evidence.usable_providers:
            skipped["skipped_provider_unusable"] += 1
            return False
        if route in evidence.cooled_models:
            skipped["skipped_cooled"] += 1
            return False
        return True

    for route, traffic in evidence.proven_by_traffic.items():
        newest = evidence.traffic_by_route.get(route, traffic)
        if not _ok_status(traffic.last_status) or not _ok_status(newest.last_status):
            skipped["skipped_last_status_error"] += 1
            continue
        if traffic.last_request_at is None:
            skipped["skipped_unknown_age"] += 1
            continue
        if admissible(route, traffic.provider):
            positives[route] = (traffic.last_request_at, EVIDENCE_SOURCE_TRAFFIC, None)

    for event in webhook_events:
        if event.event != "request.completed":
            continue
        provider, model = event.provider, event.model
        routes = _event_routes(evidence, provider, model) if provider and model else set()
        if not provider or not routes:
            report["webhook_events_unattributed"] += 1
            continue
        for route in sorted(routes):
            if not admissible(route, provider):
                continue
            current = positives.get(route)
            if current is None or event.timestamp > current[0]:
                positives[route] = (event.timestamp, EVIDENCE_SOURCE_WEBHOOK, event.latency_ms)

    failures: dict[str, tuple[datetime, int, str]] = {}
    for route, traffic in evidence.traffic_by_route.items():
        if traffic.last_status is None:
            continue
        category = _RELAY_FEEDBACK_CATEGORIES.get(failure_class(traffic.last_status))
        if category is None:
            continue
        if traffic.last_request_at is None:
            skipped["skipped_unknown_age"] += 1
            continue
        failures[route] = (traffic.last_request_at, traffic.last_status, category)

    observed: list[datetime] = []

    def mutate(locked: HealthCache) -> None:
        for route, (observed_at, source, latency_ms) in sorted(positives.items()):
            outcome = locked.record_passive_traffic(
                route,
                observed_at=observed_at,
                now=now,
                source=source,
                latency_ms=latency_ms,
                pool=pool_of(route),
                capacity_evidence=evidence.route_capacity_evidence.get(route),
            )
            if outcome == "recorded":
                report["recorded"] += 1
                report["webhook_events_used"] += source == EVIDENCE_SOURCE_WEBHOOK
                observed.append(observed_at)
            else:
                skipped[outcome] += 1
        for route, (observed_at, status, category) in sorted(failures.items()):
            outcome = locked.record_passive_failure(
                route,
                observed_at=observed_at,
                now=now,
                http_status=status,
                category=category,
                source=EVIDENCE_SOURCE_TRAFFIC,
            )
            if outcome == "recorded":
                report["recorded_failures"] += 1
                observed.append(observed_at)
            else:
                skipped[outcome] += 1

    cache.merge_and_save(mutate)
    report["skipped"] = dict(sorted(skipped.items()))
    if observed:
        report["newest_observed_at"] = format_datetime(max(observed))
        report["oldest_observed_at"] = format_datetime(min(observed))
    return report
