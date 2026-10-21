"""Gate active admit with fresh prove-at-rest passports + budgeted confirm.

Serve cheap-path admit must not select on OmniRoute ``isActive`` alone. After
``admit_free_tier_active``, candidates are intersected with healthy/fresh
passports from the prove-at-rest store. A sufficient fresh shortlist needs no
new probes; otherwise a bounded confirm pass refreshes candidates. Everything
else is a named drop before select. Empty intersection fails closed.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from verdict.admission import RuntimeEvidence
from verdict.free_tier_admit import FreeTierAdmitReceipt, NamedDrop
from verdict.model_passports import ModelPassport
from verdict.probes import ProbeBudget, ProbeObservation, ProbePolicy, ProbeRunner, ProbeTransport
from verdict.prove_at_rest import (
    ProveAtRestStore,
    default_state_path,
    load_healthy_passports,
    passport_from_probe,
)

REASON_NO_PASSPORT = "no_passport"
REASON_PASSPORT_STALE = "passport_stale"
REASON_CONFIRM_FAILED = "confirm_failed"
REASON_CONFIRM_BUDGET = "confirm_budget_exhausted"
REASON_CONFIRM_UNAVAILABLE = "confirm_unavailable"

DEFAULT_CONFIRM_MAX_CANDIDATES = 3
DEFAULT_CONFIRM_TIMEOUT_SECONDS = 5.0
# ProbeBudget counts prompt plus completion usage, not just max_tokens=1 output.
DEFAULT_CONFIRM_TOKEN_BUDGET_PER_REQUEST = 128
_CONFIRM_PROVIDER = "admit_confirm"

FAIL_CLOSED_PROVE_CONFIRM = "fail_closed — empty active ∩ fresh-passport ∩ confirmed intersection"


@dataclass(frozen=True)
class PassportEvidence:
    """Receipt evidence for one passport gate decision."""

    identity_id: str
    fresh: bool
    reason: str | None = None
    expires_at: str | None = None
    qualified_at: str | None = None
    auth_state: str | None = None
    availability_state: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"identity_id": self.identity_id, "fresh": self.fresh}
        if self.reason is not None:
            payload["reason"] = self.reason
        if self.expires_at is not None:
            payload["expires_at"] = self.expires_at
        if self.qualified_at is not None:
            payload["qualified_at"] = self.qualified_at
        if self.auth_state is not None:
            payload["auth_state"] = self.auth_state
        if self.availability_state is not None:
            payload["availability_state"] = self.availability_state
        return payload


@dataclass(frozen=True)
class ConfirmEvidence:
    """Receipt evidence for one budgeted confirm probe."""

    identity_id: str
    confirmed: bool
    status: str
    latency_ms: float | None = None
    error: str | None = None
    http_status: int | None = None
    error_class: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "identity_id": self.identity_id,
            "confirmed": self.confirmed,
            "status": self.status,
        }
        if self.latency_ms is not None:
            payload["latency_ms"] = self.latency_ms
        if self.error is not None:
            payload["error"] = self.error
        if self.http_status is not None:
            payload["http_status"] = self.http_status
        if self.error_class is not None:
            payload["error_class"] = self.error_class
        return payload


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _format_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def passport_is_fresh(passport: ModelPassport, *, now: datetime | None = None) -> bool:
    """Return True when a prove-at-rest passport is still usable for admit."""
    current = now or _now()
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    from verdict.orchestration.health_cache import PERMIT_PROOF_MAX_VALIDITY_SECONDS

    if (current - passport.qualified_at).total_seconds() >= PERMIT_PROOF_MAX_VALIDITY_SECONDS:
        return False
    if passport.expires_at <= current:
        return False
    if passport.auth_state != "authorized":
        return False
    return passport.availability_state == "eligible"


def _passport_can_refresh(passport: ModelPassport | None) -> bool:
    """True when a candidate may be refreshed by one budgeted live confirm.

    A missing passport (None) is eligible: the candidate was already admitted
    by ``admit_free_tier_active`` and the bounded confirm probe is the only
    path to establish an initial passport for a cold/empty store.

    For an existing passport, expiry is operational; authorization and a
    non-denied availability state justify one bounded probe.  Unauthorized or
    denied passports stay fail-closed.
    """
    if passport is None:
        return True
    if passport.auth_state != "authorized":
        return False
    return passport.availability_state in {"eligible", "degraded", "quarantined"}


def _passport_evidence(
    identity_id: str, passport: ModelPassport | None, *, now: datetime
) -> PassportEvidence:
    if passport is None:
        return PassportEvidence(identity_id=identity_id, fresh=False, reason=REASON_NO_PASSPORT)
    fresh = passport_is_fresh(passport, now=now)
    reason = None if fresh else REASON_PASSPORT_STALE
    return PassportEvidence(
        identity_id=identity_id,
        fresh=fresh,
        reason=reason,
        expires_at=_format_datetime(passport.expires_at),
        qualified_at=_format_datetime(passport.qualified_at),
        auth_state=passport.auth_state,
        availability_state=passport.availability_state,
    )


def passport_confirmation(
    route_ids: Sequence[str],
    *,
    now: datetime,
    passports: Mapping[str, ModelPassport] | None = None,
    passport_store_path: Path | None = None,
) -> dict[str, Any]:
    """Confirmation of stored passports in the shape ``relay.admission_proof`` consumes.

    Read-only: no probes and no store writes. A route is admitted only when its
    own passport is fresh, authorized and eligible; ``qualified_at`` and
    ``expires_at`` are the passport's, never ``now``. ``receipt_id`` is the
    SHA-256 of the canonical JSON of the passports used.
    """
    loaded = load_healthy_passports(passport_store_path) if passports is None else dict(passports)
    from verdict.orchestration.health_cache import HealthCache, default_cache_path

    cache = HealthCache(default_cache_path())
    admitted: list[str] = []
    rows: list[dict[str, Any]] = []
    for route_id in route_ids:
        passport = loaded.get(route_id)
        negative = cache.entry(route_id)
        if (
            passport is not None
            and negative is not None
            and not negative.healthy
            and negative.checked_at >= passport.qualified_at
        ):
            passport = None
        evidence = _passport_evidence(route_id, passport, now=now)
        rows.append(evidence.to_dict())
        if evidence.fresh:
            admitted.append(route_id)
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":"))
    return {
        "admitted": admitted,
        "receipt_id": "passports:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "passport": rows,
    }


def _record_refreshed_passport(
    passport_rows: list[PassportEvidence],
    identity_id: str,
    previous: ModelPassport | None,
    observation: ProbeObservation,
    *,
    now: datetime,
) -> None:
    """Replace stale/missing passport evidence when the budgeted confirm just succeeded."""
    if previous is not None and passport_is_fresh(previous, now=now):
        return
    provider = (previous.provider if previous is not None else None) or identity_id.split("/", 1)[0]
    refreshed = passport_from_probe(
        provider=provider, identity_id=identity_id, observation=observation
    )
    evidence = _passport_evidence(identity_id, refreshed, now=now)
    for index, row in enumerate(passport_rows):
        if row.identity_id == identity_id:
            passport_rows[index] = evidence
            return
    passport_rows.append(evidence)


def _persist_confirm_observations(
    observations: Mapping[str, ProbeObservation],
    *,
    passport_store_path: Path | None,
    passports_were_explicit: bool,
    now: datetime,
) -> None:
    """Write probed confirms back to the prove-at-rest cycle.

    An explicit passport map with no store path is an in-memory fixture and
    must not touch the operator's default store. Production passes the store
    path even when it also passes a preloaded passport map. A real observation
    can initialize a cold store.
    """
    if not observations or (passports_were_explicit and passport_store_path is None):
        return
    store = ProveAtRestStore(passport_store_path or default_state_path())
    for identity_id, observation in observations.items():
        store.record_confirm(identity_id, observation, now=now)


def record_dispatch_observation(
    route_id: str,
    *,
    healthy: bool,
    observed_at: datetime,
    passport_store_path: Path | None = None,
    health_cache_path: Path | None = None,
    failure_category: str = "upstream",
    retry_after_seconds: float | None = None,
) -> None:
    """Refresh permit proof from a real confirm, screen or completed dispatch.

    This is liveness/authorization evidence only, never tool or task-quality
    credit. Callers supply the call's start time so concurrent negatives win.
    """
    from verdict.orchestration.health_cache import HealthCache, ProbeResult, default_cache_path

    cache = HealthCache(health_cache_path or default_cache_path())
    if not healthy:

        def record_negative(locked: HealthCache) -> None:
            previous = locked.entry(route_id)
            if previous is not None and (
                previous.checked_at > observed_at
                or (previous.checked_at == observed_at and not previous.healthy)
            ):
                return
            locked.record(
                route_id,
                ProbeResult(
                    category=failure_category,
                    chat_ok=False,
                    tool_ok=False,
                    retry_after_seconds=retry_after_seconds,
                    failure_scope="route",
                ),
                observed_at,
            )

        cache.merge_and_save(record_negative)
    latest = cache.entry(route_id)
    if (
        healthy
        and latest is not None
        and not latest.healthy
        and (latest.checked_at >= observed_at or latest.until > observed_at)
    ):
        return
    ProveAtRestStore(passport_store_path or default_state_path()).record_confirm(
        route_id,
        ProbeObservation(
            model_id=route_id,
            availability_state="ready" if healthy else "denied",
            status="ok" if healthy else "failed",
            observed_at=observed_at,
            error=None if healthy else "dispatch_failed",
        ),
        now=observed_at,
    )


def _confirm_ok(observation: ProbeObservation) -> bool:
    return observation.availability_state == "ready" and observation.error is None


def _confirm_from_observation(observation: ProbeObservation) -> ConfirmEvidence:
    confirmed = _confirm_ok(observation)
    status = "confirmed" if confirmed else (observation.error or observation.status or "failed")
    if observation.error == "budget_exhausted":
        status = REASON_CONFIRM_BUDGET
    return ConfirmEvidence(
        identity_id=observation.model_id,
        confirmed=confirmed,
        status=status,
        latency_ms=observation.latency_ms,
        error=observation.error,
        http_status=observation.http_status,
        error_class=observation.error_class,
    )


def gate_admit_prove_confirm(
    receipt: FreeTierAdmitReceipt,
    *,
    passports: Mapping[str, ModelPassport] | None = None,
    passive_health: RuntimeEvidence | None = None,
    health_cache_path: Path | None = None,
    passport_store_path: Path | None = None,
    confirm_transport: ProbeTransport | None = None,
    confirm_budget: ProbeBudget | None = None,
    now: datetime | None = None,
    live: bool = False,
    consented: bool = False,
    max_confirm_candidates: int = DEFAULT_CONFIRM_MAX_CANDIDATES,
    confirm_timeout_seconds: float = DEFAULT_CONFIRM_TIMEOUT_SECONDS,
) -> FreeTierAdmitReceipt:
    """Intersect active admit with fresh passports + budgeted confirm.

    Named drops are appended for ``no_passport``, ``passport_stale``,
    ``confirm_failed``, ``confirm_budget_exhausted``, and ``confirm_unavailable``.
    Any fresh stored passport reuses its proof without transport calls; a
    passively-healthy route without one still gets a single bounded confirm.
    Otherwise, missing confirm transport drops the shortlist fail-closed.
    """
    if (
        isinstance(max_confirm_candidates, bool)
        or not isinstance(max_confirm_candidates, int)
        or max_confirm_candidates < 1
    ):
        raise ValueError("max_confirm_candidates must be a positive integer")
    current = now or _now()
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    loaded = load_healthy_passports(passport_store_path) if passports is None else dict(passports)

    if health_cache_path is not None and passports is None:
        from verdict.orchestration.credential_pools import canonical_route
        from verdict.orchestration.health_cache import STATE_FRESH, HealthCache
        from verdict.prove_at_rest import passport_from_probe

        cache = HealthCache(health_cache_path)
        cached_ids = [
            identity_id
            for identity_id in receipt.admitted
            if (
                entry := (
                    cache.routes().get(identity_id)
                    or cache.routes().get(canonical_route(identity_id))
                )
            )
            is not None
            and entry.state_at(current) == STATE_FRESH
            and entry.chat_ok
            and entry.tool_ok
            and (
                passive_health is None
                or any(
                    (
                        row.key == "route:" + identity_id
                        or row.key == "route:" + canonical_route(identity_id)
                    )
                    and row.state == "healthy"
                    and row.until is not None
                    and datetime.fromisoformat(row.until) > current
                    for row in passive_health.observations
                )
            )
        ]
        if cached_ids:
            rows = []
            for identity_id in cached_ids:
                entry = cache.routes().get(identity_id) or cache.routes().get(
                    canonical_route(identity_id)
                )
                assert entry is not None
                probe_passport = passport_from_probe(
                    provider=identity_id.split("/", 1)[0],
                    identity_id=identity_id,
                    observation=ProbeObservation(
                        model_id=identity_id,
                        availability_state="ready",
                        status="ok",
                        observed_at=entry.checked_at,
                        latency_ms=entry.latency_ms,
                    ),
                )
                if passport_is_fresh(probe_passport, now=current):
                    rows.append(_passport_evidence(identity_id, probe_passport, now=current))
            if rows:
                return _finalize(
                    receipt,
                    admitted=[row.identity_id for row in rows],
                    exclusions=list(receipt.exclusions),
                    passport_evidence=rows,
                    confirm_evidence=(),
                )

    from verdict.orchestration.credential_pools import canonical_route

    passive_ids = {
        obs.key.removeprefix("route:")
        for obs in (passive_health or RuntimeEvidence()).observations
        if obs.key.startswith("route:")
        and obs.state == "healthy"
        and obs.until is not None
        and datetime.fromisoformat(obs.until) > current
    }
    passive_canonical = {canonical_route(r) for r in passive_ids} | passive_ids

    exclusions = list(receipt.exclusions)
    passport_rows: list[PassportEvidence] = []
    fresh_ids: list[str] = []
    passive_fresh_ids: list[str] = []

    for identity_id in receipt.admitted:
        passport = loaded.get(identity_id) or loaded.get(canonical_route(identity_id))
        evidence = _passport_evidence(identity_id, passport, now=current)
        passport_rows.append(evidence)
        if evidence.fresh:
            fresh_ids.append(identity_id)
            continue
        if passport is not None and not evidence.fresh:
            if live and consented and _passport_can_refresh(passport):
                continue
            reason = evidence.reason or REASON_PASSPORT_STALE
            exclusions.append(
                NamedDrop(
                    identity_id, reason, "prove-at-rest passport expired or not authorized/eligible"
                )
            )
            continue
        if identity_id in passive_canonical or canonical_route(identity_id) in passive_canonical:
            passive_fresh_ids.append(identity_id)
            continue
        if live and consented and _passport_can_refresh(passport):
            # Operational TTL elapsed. Keep the candidate for the bounded
            # confirm refresh; a failed or unattempted refresh still drops it.
            continue
        reason = evidence.reason or REASON_NO_PASSPORT
        detail = (
            "no healthy prove-at-rest passport"
            if reason == REASON_NO_PASSPORT
            else "prove-at-rest passport expired or not authorized/eligible"
        )
        exclusions.append(NamedDrop(identity_id, reason, detail))

    confirm_rows: list[ConfirmEvidence] = []
    confirmed_ids: list[str] = []

    if fresh_ids:
        ordered_fresh = fresh_ids
        confirmed = ordered_fresh[:max_confirm_candidates]
        excluded_ids = {drop.model_id for drop in exclusions}
        for identity_id in ordered_fresh[max_confirm_candidates:]:
            drop = NamedDrop(
                identity_id, REASON_CONFIRM_BUDGET, "outside budgeted confirm shortlist"
            )
            exclusions.append(drop)
            excluded_ids.add(drop.model_id)
        fresh_set = set(fresh_ids)
        for evidence in passport_rows:
            if evidence.identity_id not in fresh_set and evidence.identity_id not in excluded_ids:
                drop = NamedDrop(
                    evidence.identity_id,
                    evidence.reason or REASON_PASSPORT_STALE,
                    "passport not refreshed; fresh shortlist is sufficient",
                )
                exclusions.append(drop)
                excluded_ids.add(drop.model_id)
        return _finalize(
            receipt,
            admitted=confirmed,
            exclusions=exclusions,
            passport_evidence=passport_rows,
            confirm_evidence=(),
        )

    # A stored passport is operational evidence with a short TTL. When a live
    # confirm is consented, refresh only the budgeted shortlist instead of
    # dropping the whole pool because the background prover is stale. Stable
    # capability metadata is not re-probed here. Without live consent, an
    # expired passport still fails closed as passport_stale.
    if live and consented:
        refreshable = [
            identity_id
            for identity_id in receipt.admitted
            if _passport_can_refresh(loaded.get(identity_id))
        ]
        eligible_ids = set(fresh_ids) | set(passive_fresh_ids) | set(refreshable)
    else:
        eligible_ids = set(fresh_ids) | set(passive_fresh_ids)
    ordered_ids = [item for item in receipt.admitted if item in eligible_ids]
    ordered = ordered_ids
    shortlist = ordered[:max_confirm_candidates]
    deferred = ordered[max_confirm_candidates:]
    covered = set(shortlist) | set(deferred)
    for index, evidence in enumerate(passport_rows):
        if evidence.fresh or evidence.reason is not None or evidence.identity_id in covered:
            continue
        exclusions.append(
            NamedDrop(
                evidence.identity_id,
                REASON_PASSPORT_STALE,
                "operational passport expired and confirm budget was not spent on it",
            )
        )
        passport_rows[index] = PassportEvidence(
            identity_id=evidence.identity_id,
            fresh=False,
            reason=REASON_PASSPORT_STALE,
            expires_at=evidence.expires_at,
            qualified_at=evidence.qualified_at,
            auth_state=evidence.auth_state,
            availability_state=evidence.availability_state,
        )

    for identity_id in deferred:
        passport = loaded.get(identity_id) or loaded.get(canonical_route(identity_id))
        if passport is not None and not passport_is_fresh(passport, now=current):
            exclusions.append(
                NamedDrop(identity_id, REASON_PASSPORT_STALE, "passport was not refreshed")
            )
        exclusions.append(
            NamedDrop(identity_id, REASON_CONFIRM_BUDGET, "outside budgeted confirm shortlist")
        )
        confirm_rows.append(
            ConfirmEvidence(
                identity_id=identity_id,
                confirmed=False,
                status=REASON_CONFIRM_BUDGET,
                error="outside budgeted confirm shortlist",
            )
        )

    if not shortlist:
        return _finalize(
            receipt,
            admitted=(),
            exclusions=exclusions,
            passport_evidence=passport_rows,
            confirm_evidence=confirm_rows,
        )

    if confirm_transport is None:
        for identity_id in shortlist:
            exclusions.append(
                NamedDrop(
                    identity_id,
                    REASON_CONFIRM_UNAVAILABLE,
                    "budgeted confirm transport not configured",
                )
            )
            confirm_rows.append(
                ConfirmEvidence(
                    identity_id=identity_id,
                    confirmed=False,
                    status=REASON_CONFIRM_UNAVAILABLE,
                    error="confirm transport not configured",
                )
            )
        return _finalize(
            receipt,
            admitted=(),
            exclusions=exclusions,
            passport_evidence=passport_rows,
            confirm_evidence=confirm_rows,
        )

    budget = confirm_budget or ProbeBudget(
        provider=_CONFIRM_PROVIDER,
        max_requests=len(shortlist),
        max_tokens=DEFAULT_CONFIRM_TOKEN_BUDGET_PER_REQUEST * max_confirm_candidates,
        max_response_bytes=65_536,
        max_duration_seconds=max(
            float(confirm_timeout_seconds) * max_confirm_candidates, float(confirm_timeout_seconds)
        ),
    )
    observations: list[ProbeObservation] = []
    deadline = time.monotonic() + budget.max_duration_seconds
    tokens_left = budget.max_tokens
    bytes_left = budget.max_response_bytes
    requests_left = budget.max_requests
    successes = 0
    for identity_id in shortlist:
        remaining = deadline - time.monotonic()
        if (
            successes >= max_confirm_candidates
            or requests_left <= 0
            or tokens_left <= 0
            or bytes_left <= 0
            or remaining <= 0
        ):
            observations.append(
                ProbeObservation(
                    model_id=identity_id,
                    availability_state="unknown",
                    status="skipped",
                    observed_at=current,
                    error="budget_exhausted",
                )
            )
            continue
        runner = ProbeRunner(
            ProbePolicy(
                max_models_per_run=1,
                timeout_seconds=min(float(confirm_timeout_seconds), remaining),
                max_duration_seconds=remaining,
                cooldown_seconds=0.0,
                max_cooldown_seconds=0.0,
                quarantine_seconds=0.0,
            )
        )
        result = runner.run_with_diagnostics(
            [identity_id],
            confirm_transport,
            now=current,
            live=live,
            consented=consented,
            provider=_CONFIRM_PROVIDER,
            budget=ProbeBudget(
                provider=_CONFIRM_PROVIDER,
                max_requests=1,
                max_tokens=tokens_left,
                max_response_bytes=bytes_left,
                max_duration_seconds=remaining,
            ),
        )
        observations.extend(result.observations)
        requests_left -= result.diagnostics.transport_calls
        tokens_left -= result.diagnostics.observed_tokens
        bytes_left -= result.diagnostics.response_bytes
        successes += sum(_confirm_ok(item) for item in result.observations)
    by_id = {item.model_id: item for item in observations}
    for identity_id in shortlist:
        observation = by_id.get(identity_id)
        if observation is None:
            exclusions.append(
                NamedDrop(identity_id, REASON_CONFIRM_FAILED, "confirm probe missing")
            )
            confirm_rows.append(
                ConfirmEvidence(
                    identity_id=identity_id,
                    confirmed=False,
                    status=REASON_CONFIRM_FAILED,
                    error="probe_missing",
                )
            )
            continue
        if health_cache_path is not None and _confirm_ok(observation):
            from verdict.orchestration.health_cache import HealthCache

            latest = HealthCache(health_cache_path).entry(identity_id)
            if (
                latest is not None
                and not latest.healthy
                and (latest.checked_at >= observation.observed_at or latest.until > current)
            ):
                observation = replace(
                    observation, availability_state="denied", error="newer_negative"
                )
                by_id[identity_id] = observation
        confirm = _confirm_from_observation(observation)
        confirm_rows.append(confirm)
        if confirm.confirmed:
            confirmed_ids.append(identity_id)
            _record_refreshed_passport(
                passport_rows, identity_id, loaded.get(identity_id), observation, now=current
            )
            continue
        reason = (
            REASON_CONFIRM_BUDGET
            if confirm.status == REASON_CONFIRM_BUDGET
            else REASON_CONFIRM_FAILED
        )
        exclusions.append(NamedDrop(identity_id, reason, confirm.error or confirm.status))
        passport = loaded.get(identity_id) or loaded.get(canonical_route(identity_id))
        if (
            reason == REASON_CONFIRM_BUDGET
            and passport is not None
            and not passport_is_fresh(passport, now=current)
        ):
            exclusions.append(
                NamedDrop(identity_id, REASON_PASSPORT_STALE, "passport was not refreshed")
            )

    _persist_confirm_observations(
        {
            identity: observation
            for identity, observation in by_id.items()
            if observation.status != "skipped"
        },
        passport_store_path=passport_store_path,
        passports_were_explicit=passports is not None,
        now=current,
    )
    return _finalize(
        receipt,
        admitted=tuple(sorted(confirmed_ids)),
        exclusions=exclusions,
        passport_evidence=passport_rows,
        confirm_evidence=confirm_rows,
    )


def _finalize(
    receipt: FreeTierAdmitReceipt,
    *,
    admitted: Sequence[str],
    exclusions: list[NamedDrop],
    passport_evidence: Sequence[PassportEvidence],
    confirm_evidence: Sequence[ConfirmEvidence],
) -> FreeTierAdmitReceipt:
    admitted_set = set(admitted)
    admitted_sorted = tuple(item for item in receipt.admitted if item in admitted_set)
    chosen = admitted_sorted[0] if admitted_sorted else None
    return replace(
        receipt,
        admitted=admitted_sorted,
        exclusions=tuple(exclusions),
        chosen=chosen,
        empty_intersection=chosen is None,
        passport=tuple(passport_evidence),
        confirm=tuple(confirm_evidence),
    )


__all__ = [
    "DEFAULT_CONFIRM_MAX_CANDIDATES",
    "DEFAULT_CONFIRM_TIMEOUT_SECONDS",
    "FAIL_CLOSED_PROVE_CONFIRM",
    "REASON_CONFIRM_BUDGET",
    "REASON_CONFIRM_FAILED",
    "REASON_CONFIRM_UNAVAILABLE",
    "REASON_NO_PASSPORT",
    "REASON_PASSPORT_STALE",
    "ConfirmEvidence",
    "PassportEvidence",
    "gate_admit_prove_confirm",
    "passport_confirmation",
    "passport_is_fresh",
]
