"""Gate free∩active admit with fresh prove-at-rest passports + budgeted confirm.

Serve cheap-path admit must not select on OmniRoute ``isActive`` alone. After
``admit_free_tier_active``, candidates are intersected with healthy/fresh
passports from the prove-at-rest store, then a small bounded confirm probe
runs. Only confirmed identities may be selected; everything else is a named
drop before select. Empty intersection fails closed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from verdict.free_tier_admit import FreeTierAdmitReceipt, NamedDrop, _choose_sort
from verdict.model_passports import ModelPassport
from verdict.probes import ProbeBudget, ProbeObservation, ProbePolicy, ProbeRunner, ProbeTransport
from verdict.prove_at_rest import load_healthy_passports

REASON_NO_PASSPORT = "no_passport"
REASON_PASSPORT_STALE = "passport_stale"
REASON_CONFIRM_FAILED = "confirm_failed"
REASON_CONFIRM_BUDGET = "confirm_budget_exhausted"
REASON_CONFIRM_UNAVAILABLE = "confirm_unavailable"

DEFAULT_CONFIRM_MAX_CANDIDATES = 3
DEFAULT_CONFIRM_TIMEOUT_SECONDS = 5.0
_CONFIRM_PROVIDER = "admit_confirm"

FAIL_CLOSED_PROVE_CONFIRM = (
    "fail_closed — empty free∩active ∩ fresh-passport ∩ confirmed intersection"
)


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
    if passport.expires_at <= current:
        return False
    if passport.auth_state != "authorized":
        return False
    return passport.availability_state == "eligible"


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
    passport_store_path: Path | None = None,
    confirm_transport: ProbeTransport | None = None,
    confirm_budget: ProbeBudget | None = None,
    now: datetime | None = None,
    live: bool = False,
    consented: bool = False,
    max_confirm_candidates: int = DEFAULT_CONFIRM_MAX_CANDIDATES,
    confirm_timeout_seconds: float = DEFAULT_CONFIRM_TIMEOUT_SECONDS,
) -> FreeTierAdmitReceipt:
    """Intersect free∩active admit with fresh passports + budgeted confirm.

    Named drops are appended for ``no_passport``, ``passport_stale``,
    ``confirm_failed``, ``confirm_budget_exhausted``, and ``confirm_unavailable``.
    When the confirm transport is missing, every shortlisted candidate is dropped
    fail-closed (no silent admit).
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

    exclusions = list(receipt.exclusions)
    passport_rows: list[PassportEvidence] = []
    fresh_ids: list[str] = []

    for identity_id in receipt.admitted:
        passport = loaded.get(identity_id)
        evidence = _passport_evidence(identity_id, passport, now=current)
        passport_rows.append(evidence)
        if evidence.fresh:
            fresh_ids.append(identity_id)
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

    if not fresh_ids:
        return _finalize(
            receipt,
            admitted=(),
            exclusions=exclusions,
            passport_evidence=passport_rows,
            confirm_evidence=confirm_rows,
        )

    healthy = frozenset(receipt.active_providers)
    ordered = sorted(fresh_ids, key=lambda item: _choose_sort(item, healthy))
    shortlist = ordered[:max_confirm_candidates]
    deferred = ordered[max_confirm_candidates:]
    for identity_id in deferred:
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
        max_tokens=len(shortlist),
        max_response_bytes=65_536,
        max_duration_seconds=max(
            float(confirm_timeout_seconds) * len(shortlist), float(confirm_timeout_seconds)
        ),
    )
    runner = ProbeRunner(
        ProbePolicy(
            max_models_per_run=len(shortlist),
            timeout_seconds=float(confirm_timeout_seconds),
            max_duration_seconds=budget.max_duration_seconds,
            cooldown_seconds=0.0,
            max_cooldown_seconds=0.0,
            quarantine_seconds=0.0,
        )
    )
    observations = runner.run(
        shortlist,
        confirm_transport,
        now=current,
        live=live,
        consented=consented,
        provider=_CONFIRM_PROVIDER,
        budget=budget,
    )
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
        confirm = _confirm_from_observation(observation)
        confirm_rows.append(confirm)
        if confirm.confirmed:
            confirmed_ids.append(identity_id)
            continue
        reason = (
            REASON_CONFIRM_BUDGET
            if confirm.status == REASON_CONFIRM_BUDGET
            else REASON_CONFIRM_FAILED
        )
        exclusions.append(NamedDrop(identity_id, reason, confirm.error or confirm.status))

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
    admitted_sorted = tuple(sorted(admitted))
    healthy = frozenset(receipt.active_providers)
    chosen = None
    if admitted_sorted:
        chosen = sorted(admitted_sorted, key=lambda item: _choose_sort(item, healthy))[0]
    return FreeTierAdmitReceipt(
        admitted=admitted_sorted,
        exclusions=tuple(exclusions),
        chosen=chosen,
        empty_intersection=chosen is None,
        active_providers=receipt.active_providers,
        free_tier_providers=receipt.free_tier_providers,
        pack_digest=receipt.pack_digest,
        omissions=receipt.omissions,
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
    "passport_is_fresh",
]
