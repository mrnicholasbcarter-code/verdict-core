"""Cost ledger — reservations, reconciliation, and trajectory economics (session economics (STAY/SWITCH)).

Economics only: this module accounts for complete-path spend (planning,
hydration, tools, execution, verification, retry/escalation, cache, switching,
subscription/quota opportunity, and metered cash).  It does **not** own
STAY/SWITCH session decisions (BOD-119), execution-path optimization (BOD-104),
or task-class/worthiness routing (BOD-107/109).

Frozen consumer contract for BOD-119
------------------------------------
Session Economics should consume these symbols without forking arithmetic:

* ``CostTerm`` — atomic term with ``kind``, ``amount`` (``Decimal | None``),
  ``unit``, ``status`` ∈ {``observed``, ``estimated``, ``unknown``, ``assumed``},
  plus optional ``evidence_id`` / ``observed_at`` / ``fresh_until``.
* ``Reservation`` / ``CostLedger.reserve`` / ``CostLedger.reconcile`` —
  atomic reservations; reconcile when actual usage is missing, delayed,
  partial, or cached.
* ``CostLedger.attribute_assistance`` — maps :class:`~verdict.effective_capability.AssistanceCost`
  token buckets onto the same trajectory.
* Optional BOD-92 shapes: ``QuotaEvidenceInput``, ``PriceEvidenceInput``,
  ``CacheEvidenceInput`` — unknown defaults; never invent cache savings.

Cash, subscription opportunity, and quota pressure are distinct pools.
``claims_ledger`` is a different artifact and must not be overloaded here.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, TypedDict

from verdict.effective_capability import AssistanceCost

COST_LEDGER_SCHEMA_VERSION = "1"

CostTermStatus = Literal["observed", "estimated", "unknown", "assumed"]
CostUnit = Literal["usd", "tokens", "quota_units", "subscription_units", "dimensionless"]
SpendPool = Literal["cash", "subscription", "quota", "cheaper_inference"]
ReservationStatus = Literal["reserved", "reconciled", "released", "partial"]

CostTermKind = Literal[
    "planning",
    "hydration",
    "tools",
    "execution",
    "verification",
    "retry",
    "escalation",
    "cache_read",
    "cache_write",
    "route_switch",
    "subscription_opportunity",
    "quota_pressure",
    "cash",
    "overhead",
    "cheaper_inference",
]


class CostLedgerError(ValueError):
    """Raised when ledger arithmetic or reservation rules cannot proceed safely."""


class QuotaEvidenceInput(TypedDict, total=False):
    """Optional normalized quota/cooldown evidence (runtime certification (passport) consumer shape)."""

    pool_id: str
    remaining_pct: float | None
    cooldown_until: str | None
    observed_at: str | None
    fresh_until: str | None
    evidence_id: str | None


class PriceEvidenceInput(TypedDict, total=False):
    """Optional price evidence. Missing/stale fields stay explicit."""

    input_usd_per_mtok: str | None
    output_usd_per_mtok: str | None
    observed_at: str | None
    fresh_until: str | None
    stale: bool
    evidence_id: str | None


class CacheEvidenceInput(TypedDict, total=False):
    """Optional cache evidence. Savings apply only when observed — never invented."""

    cached_input_tokens: int | None
    cache_hit: bool | None
    savings_usd: str | None
    observed_at: str | None
    fresh_until: str | None
    evidence_id: str | None


def _utc(value: datetime | None, field_name: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise CostLedgerError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _format_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _as_decimal(value: Decimal | int | str | float | None, field_name: str) -> Decimal | None:
    if value is None:
        return None
    try:
        if isinstance(value, Decimal):
            amount = value
        elif isinstance(value, bool):
            raise InvalidOperation
        else:
            amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise CostLedgerError(f"{field_name} must be a finite decimal") from exc
    if not amount.is_finite():
        raise CostLedgerError(f"{field_name} must be a finite decimal")
    return amount


def _require_non_negative(amount: Decimal, field_name: str) -> Decimal:
    if amount < 0:
        raise CostLedgerError(f"{field_name} must be non-negative")
    return amount


@dataclass(frozen=True)
class CostTerm:
    """Atomic cost term with explicit observation status.

    ``amount`` is ``None`` when ``status`` is ``unknown``. Unknown terms stay
    unknown — consumers must not invent fill-in values.
    """

    kind: str
    amount: Decimal | None
    unit: CostUnit
    status: CostTermStatus
    evidence_id: str | None = None
    observed_at: datetime | None = None
    fresh_until: datetime | None = None
    notes: str | None = None

    def __post_init__(self) -> None:
        if not self.kind or not str(self.kind).strip():
            raise CostLedgerError("kind must be a non-empty string")
        if self.status not in {"observed", "estimated", "unknown", "assumed"}:
            raise CostLedgerError(f"invalid status: {self.status!r}")
        if self.unit not in {"usd", "tokens", "quota_units", "subscription_units", "dimensionless"}:
            raise CostLedgerError(f"invalid unit: {self.unit!r}")
        object.__setattr__(self, "observed_at", _utc(self.observed_at, "observed_at"))
        object.__setattr__(self, "fresh_until", _utc(self.fresh_until, "fresh_until"))
        if self.status == "unknown":
            if self.amount is not None:
                raise CostLedgerError("unknown terms must not carry an amount")
            return
        amount = _as_decimal(self.amount, "amount")
        if amount is None:
            raise CostLedgerError(f"{self.status} terms require an amount")
        object.__setattr__(self, "amount", _require_non_negative(amount, "amount"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "amount": None if self.amount is None else str(self.amount),
            "unit": self.unit,
            "status": self.status,
            "evidence_id": self.evidence_id,
            "observed_at": _format_datetime(self.observed_at),
            "fresh_until": _format_datetime(self.fresh_until),
            "notes": self.notes,
        }


@dataclass(frozen=True)
class Reservation:
    """Reserved spend against a named pool on one trajectory."""

    reservation_id: str
    trajectory_id: str
    amount: Decimal
    unit: CostUnit
    pool: SpendPool
    status: ReservationStatus
    reserved_at: datetime
    terms: tuple[CostTerm, ...] = ()
    actual_amount: Decimal | None = None
    reconcile_notes: str | None = None
    pool_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount", _require_non_negative(self.amount, "amount"))
        object.__setattr__(self, "reserved_at", _utc(self.reserved_at, "reserved_at"))
        if self.actual_amount is not None:
            object.__setattr__(
                self,
                "actual_amount",
                _require_non_negative(
                    _as_decimal(self.actual_amount, "actual_amount") or Decimal("0"),
                    "actual_amount",
                ),
            )
        if not isinstance(self.terms, tuple):
            object.__setattr__(self, "terms", tuple(self.terms))

    def to_dict(self) -> dict[str, Any]:
        return {
            "reservation_id": self.reservation_id,
            "trajectory_id": self.trajectory_id,
            "amount": str(self.amount),
            "unit": self.unit,
            "pool": self.pool,
            "pool_id": self.pool_id,
            "status": self.status,
            "reserved_at": _format_datetime(self.reserved_at),
            "actual_amount": None if self.actual_amount is None else str(self.actual_amount),
            "reconcile_notes": self.reconcile_notes,
            "terms": [term.to_dict() for term in self.terms],
        }


@dataclass
class CostLedger:
    """Trajectory-scoped cost ledger with atomic reservations.

    Cash, subscription, quota, and protected CheaperInference budgets are
    distinct. Concurrent ``reserve`` calls are serialized by an ``RLock``.
    """

    trajectory_id: str
    cash_budget_usd: Decimal | None = None
    subscription_budgets: Mapping[str, Decimal] = field(default_factory=dict)
    quota_budgets: Mapping[str, Decimal] = field(default_factory=dict)
    cheaper_inference_budget_usd: Decimal = field(default_factory=lambda: Decimal("0"))
    cheaper_inference_protected_floor_usd: Decimal = field(default_factory=lambda: Decimal("0"))
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)
    _reservations: dict[str, Reservation] = field(default_factory=dict, init=False, repr=False)
    _cash_reserved: Decimal = field(default_factory=lambda: Decimal("0"), init=False, repr=False)
    _cash_spent: Decimal = field(default_factory=lambda: Decimal("0"), init=False, repr=False)
    _subscription_reserved: dict[str, Decimal] = field(default_factory=dict, init=False, repr=False)
    _quota_reserved: dict[str, Decimal] = field(default_factory=dict, init=False, repr=False)
    _ci_reserved: Decimal = field(default_factory=lambda: Decimal("0"), init=False, repr=False)
    _ci_spent: Decimal = field(default_factory=lambda: Decimal("0"), init=False, repr=False)
    _assistance_terms: list[CostTerm] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.trajectory_id or not str(self.trajectory_id).strip():
            raise CostLedgerError("trajectory_id must be non-empty")
        if self.cash_budget_usd is not None:
            self.cash_budget_usd = _require_non_negative(
                _as_decimal(self.cash_budget_usd, "cash_budget_usd") or Decimal("0"),
                "cash_budget_usd",
            )
        self.cheaper_inference_budget_usd = _require_non_negative(
            _as_decimal(self.cheaper_inference_budget_usd, "cheaper_inference_budget_usd")
            or Decimal("0"),
            "cheaper_inference_budget_usd",
        )
        self.cheaper_inference_protected_floor_usd = _require_non_negative(
            _as_decimal(
                self.cheaper_inference_protected_floor_usd, "cheaper_inference_protected_floor_usd"
            )
            or Decimal("0"),
            "cheaper_inference_protected_floor_usd",
        )
        if self.cheaper_inference_protected_floor_usd > self.cheaper_inference_budget_usd:
            raise CostLedgerError("protected floor cannot exceed CheaperInference budget")
        self.subscription_budgets = {
            key: _require_non_negative(
                _as_decimal(value, f"subscription_budgets[{key}]") or Decimal("0"),
                f"subscription_budgets[{key}]",
            )
            for key, value in dict(self.subscription_budgets).items()
        }
        self.quota_budgets = {
            key: _require_non_negative(
                _as_decimal(value, f"quota_budgets[{key}]") or Decimal("0"), f"quota_budgets[{key}]"
            )
            for key, value in dict(self.quota_budgets).items()
        }

    def remaining_cash_usd(self) -> Decimal | None:
        with self._lock:
            if self.cash_budget_usd is None:
                return None
            return self.cash_budget_usd - self._cash_reserved - self._cash_spent

    def remaining_cheaper_inference_usd(self) -> Decimal:
        with self._lock:
            return self.cheaper_inference_budget_usd - self._ci_reserved - self._ci_spent

    def protected_cheaper_inference_remaining_usd(self) -> Decimal:
        """Headroom reserved for evaluation/calibration (protected floor)."""

        with self._lock:
            usable = self.cheaper_inference_budget_usd - self.cheaper_inference_protected_floor_usd
            consumed = self._ci_reserved + self._ci_spent
            if consumed < usable:
                return self.cheaper_inference_protected_floor_usd
            return max(Decimal("0"), self.cheaper_inference_budget_usd - consumed)

    def reserve(
        self,
        amount: Decimal | int | str,
        *,
        pool: SpendPool = "cash",
        unit: CostUnit = "usd",
        pool_id: str | None = None,
        terms: Sequence[CostTerm] = (),
        allow_protected_cheaper_inference: bool = False,
        now: datetime | None = None,
    ) -> Reservation:
        """Atomically reserve spend. Raises when the pool cannot cover ``amount``."""

        reserved_amount = _require_non_negative(
            _as_decimal(amount, "amount") or Decimal("0"), "amount"
        )
        when = _utc(now or datetime.now(timezone.utc), "now")
        assert when is not None
        term_tuple = tuple(terms)

        with self._lock:
            if pool == "cash":
                if unit != "usd":
                    raise CostLedgerError("cash pool requires usd unit")
                if self.cash_budget_usd is not None:
                    remaining = self.cash_budget_usd - self._cash_reserved - self._cash_spent
                    if reserved_amount > remaining:
                        raise CostLedgerError("insufficient cash budget")
                self._cash_reserved += reserved_amount
            elif pool == "subscription":
                if not pool_id:
                    raise CostLedgerError("subscription reserve requires pool_id")
                budget = self.subscription_budgets.get(pool_id)
                if budget is None:
                    raise CostLedgerError(f"unknown subscription pool: {pool_id!r}")
                reserved = self._subscription_reserved.get(pool_id, Decimal("0"))
                if reserved_amount > budget - reserved:
                    raise CostLedgerError("insufficient subscription budget")
                self._subscription_reserved[pool_id] = reserved + reserved_amount
            elif pool == "quota":
                if not pool_id:
                    raise CostLedgerError("quota reserve requires pool_id")
                budget = self.quota_budgets.get(pool_id)
                if budget is None:
                    raise CostLedgerError(f"unknown quota pool: {pool_id!r}")
                reserved = self._quota_reserved.get(pool_id, Decimal("0"))
                if reserved_amount > budget - reserved:
                    raise CostLedgerError("insufficient quota budget")
                self._quota_reserved[pool_id] = reserved + reserved_amount
            elif pool == "cheaper_inference":
                if unit != "usd":
                    raise CostLedgerError("cheaper_inference pool requires usd unit")
                usable = self.cheaper_inference_budget_usd
                if not allow_protected_cheaper_inference:
                    usable = (
                        self.cheaper_inference_budget_usd
                        - self.cheaper_inference_protected_floor_usd
                    )
                remaining = usable - self._ci_reserved - self._ci_spent
                if reserved_amount > remaining:
                    raise CostLedgerError("insufficient CheaperInference budget")
                self._ci_reserved += reserved_amount
            else:
                raise CostLedgerError(f"unknown pool: {pool!r}")

            reservation = Reservation(
                reservation_id=str(uuid.uuid4()),
                trajectory_id=self.trajectory_id,
                amount=reserved_amount,
                unit=unit,
                pool=pool,
                status="reserved",
                reserved_at=when,
                terms=term_tuple,
                pool_id=pool_id,
            )
            self._reservations[reservation.reservation_id] = reservation
            return reservation

    def reconcile(
        self,
        reservation_id: str,
        *,
        actual_amount: Decimal | int | str | None = None,
        missing: bool = False,
        delayed: bool = False,
        partial: bool = False,
        cached_tokens: int | None = None,
        cache_evidence: CacheEvidenceInput | None = None,
        notes: str | None = None,
    ) -> Reservation:
        """Reconcile a reservation when billing is final, partial, delayed, or missing."""

        with self._lock:
            current = self._reservations.get(reservation_id)
            if current is None:
                raise CostLedgerError(f"unknown reservation: {reservation_id!r}")
            if current.status in {"reconciled", "released"}:
                raise CostLedgerError(f"reservation already {current.status}")

            reconcile_notes = notes
            terms = list(current.terms)

            if missing:
                # Keep reservation charged conservatively until real usage arrives.
                updated = replace(
                    current,
                    status="reserved",
                    reconcile_notes=reconcile_notes or "actual_missing_conservative_hold",
                )
                self._reservations[reservation_id] = updated
                return updated

            if delayed and actual_amount is None:
                updated = replace(
                    current,
                    status="reserved",
                    reconcile_notes=reconcile_notes or "actual_delayed_hold",
                )
                self._reservations[reservation_id] = updated
                return updated

            if actual_amount is None and not partial:
                raise CostLedgerError("actual_amount required unless missing/delayed/partial")

            actual = (
                Decimal("0")
                if actual_amount is None
                else _require_non_negative(
                    _as_decimal(actual_amount, "actual_amount") or Decimal("0"), "actual_amount"
                )
            )

            if cache_evidence is not None:
                terms.extend(_cache_terms(cache_evidence, cached_tokens=cached_tokens))
            elif cached_tokens is not None:
                # Token counts alone do not imply cash savings.
                terms.append(
                    CostTerm(
                        kind="cache_read",
                        amount=Decimal(cached_tokens),
                        unit="tokens",
                        status="observed",
                        notes="cached_tokens_without_priced_savings",
                    )
                )

            status: ReservationStatus = (
                "partial" if partial and actual < current.amount else "reconciled"
            )
            self._apply_settle(current, actual)
            updated = replace(
                current,
                status=status,
                actual_amount=actual,
                terms=tuple(terms),
                reconcile_notes=reconcile_notes
                or ("partial_billing" if status == "partial" else "reconciled"),
            )
            self._reservations[reservation_id] = updated
            return updated

    def release(self, reservation_id: str) -> Reservation:
        with self._lock:
            current = self._reservations.get(reservation_id)
            if current is None:
                raise CostLedgerError(f"unknown reservation: {reservation_id!r}")
            if current.status in {"reconciled", "released", "partial"}:
                raise CostLedgerError(f"cannot release reservation in status {current.status}")
            self._release_reserved(current, current.amount)
            updated = replace(current, status="released", reconcile_notes="released")
            self._reservations[reservation_id] = updated
            return updated

    def attribute_assistance(
        self,
        assistance: AssistanceCost,
        *,
        price: PriceEvidenceInput | None = None,
        now: datetime | None = None,
    ) -> tuple[CostTerm, ...]:
        """Attribute AssistanceCost token buckets onto this trajectory."""

        when = _utc(now or datetime.now(timezone.utc), "now")
        buckets = (
            ("planning", assistance.planning_tokens),
            ("hydration", assistance.context_tokens),
            ("tools", assistance.tool_tokens),
            ("verification", assistance.verification_tokens),
        )
        terms: list[CostTerm] = []
        for kind, tokens in buckets:
            if tokens <= 0:
                continue
            terms.append(
                CostTerm(
                    kind=kind,
                    amount=Decimal(tokens),
                    unit="tokens",
                    status="estimated",
                    observed_at=when,
                    notes="assistance_cost_attribution",
                )
            )
            usd_term = tokens_to_usd_term(tokens, kind=kind, price=price, now=when)
            terms.append(usd_term)

        with self._lock:
            self._assistance_terms.extend(terms)
        return tuple(terms)

    def assistance_terms(self) -> tuple[CostTerm, ...]:
        with self._lock:
            return tuple(self._assistance_terms)

    def reservations(self) -> tuple[Reservation, ...]:
        with self._lock:
            return tuple(self._reservations.values())

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "schema_version": COST_LEDGER_SCHEMA_VERSION,
                "trajectory_id": self.trajectory_id,
                "cash_budget_usd": None
                if self.cash_budget_usd is None
                else str(self.cash_budget_usd),
                "cash_reserved_usd": str(self._cash_reserved),
                "cash_spent_usd": str(self._cash_spent),
                "subscription_budgets": {k: str(v) for k, v in self.subscription_budgets.items()},
                "quota_budgets": {k: str(v) for k, v in self.quota_budgets.items()},
                "cheaper_inference_budget_usd": str(self.cheaper_inference_budget_usd),
                "cheaper_inference_protected_floor_usd": str(
                    self.cheaper_inference_protected_floor_usd
                ),
                "reservations": [item.to_dict() for item in self._reservations.values()],
                "assistance_terms": [term.to_dict() for term in self._assistance_terms],
            }

    def _apply_settle(self, reservation: Reservation, actual: Decimal) -> None:
        reserved = reservation.amount
        if reservation.pool == "cash":
            self._cash_reserved -= reserved
            self._cash_spent += actual
            if self._cash_reserved < 0:
                self._cash_reserved = Decimal("0")
        elif reservation.pool == "subscription":
            assert reservation.pool_id is not None
            current = self._subscription_reserved.get(reservation.pool_id, Decimal("0"))
            self._subscription_reserved[reservation.pool_id] = max(
                Decimal("0"), current - reserved + actual
            )
        elif reservation.pool == "quota":
            assert reservation.pool_id is not None
            current = self._quota_reserved.get(reservation.pool_id, Decimal("0"))
            self._quota_reserved[reservation.pool_id] = max(
                Decimal("0"), current - reserved + actual
            )
        elif reservation.pool == "cheaper_inference":
            self._ci_reserved -= reserved
            self._ci_spent += actual
            if self._ci_reserved < 0:
                self._ci_reserved = Decimal("0")

    def _release_reserved(self, reservation: Reservation, amount: Decimal) -> None:
        if reservation.pool == "cash":
            self._cash_reserved = max(Decimal("0"), self._cash_reserved - amount)
        elif reservation.pool == "subscription":
            assert reservation.pool_id is not None
            current = self._subscription_reserved.get(reservation.pool_id, Decimal("0"))
            self._subscription_reserved[reservation.pool_id] = max(Decimal("0"), current - amount)
        elif reservation.pool == "quota":
            assert reservation.pool_id is not None
            current = self._quota_reserved.get(reservation.pool_id, Decimal("0"))
            self._quota_reserved[reservation.pool_id] = max(Decimal("0"), current - amount)
        elif reservation.pool == "cheaper_inference":
            self._ci_reserved = max(Decimal("0"), self._ci_reserved - amount)


def tokens_to_usd_term(
    tokens: int,
    *,
    kind: str,
    price: PriceEvidenceInput | None,
    now: datetime | None = None,
    output: bool = False,
) -> CostTerm:
    """Convert tokens to a USD term. Unknown/stale prices stay explicit/conservative."""

    when = _utc(now or datetime.now(timezone.utc), "now")
    if price is None:
        return CostTerm(
            kind=kind,
            amount=None,
            unit="usd",
            status="unknown",
            observed_at=when,
            notes="price_unknown",
        )

    rate_key = "output_usd_per_mtok" if output else "input_usd_per_mtok"
    raw_rate = price.get(rate_key)
    if raw_rate is None:
        return CostTerm(
            kind=kind,
            amount=None,
            unit="usd",
            status="unknown",
            evidence_id=price.get("evidence_id"),
            observed_at=when,
            notes=f"{rate_key}_missing",
        )
    if not isinstance(raw_rate, (str, int, float, Decimal)):
        raise CostLedgerError(f"{rate_key} must be a decimal-compatible value")

    rate = _as_decimal(raw_rate, rate_key)
    assert rate is not None
    usd = (Decimal(tokens) * rate) / Decimal("1000000")
    stale = bool(price.get("stale", False))
    fresh_until = None
    fresh_raw = price.get("fresh_until")
    if isinstance(fresh_raw, str) and fresh_raw:
        fresh_until = datetime.fromisoformat(fresh_raw.replace("Z", "+00:00"))
        if when is not None and fresh_until <= when:
            stale = True

    status: CostTermStatus = "assumed" if stale else "estimated"
    evidence_id = price.get("evidence_id")
    return CostTerm(
        kind=kind,
        amount=usd,
        unit="usd",
        status=status,
        evidence_id=evidence_id if isinstance(evidence_id, str) else None,
        observed_at=when,
        fresh_until=_utc(fresh_until, "fresh_until"),
        notes="stale_price_conservative" if stale else "priced_from_evidence",
    )


def quota_pressure_term(evidence: QuotaEvidenceInput | None) -> CostTerm:
    """Map optional quota evidence to a dimensionless pressure term."""

    if evidence is None or evidence.get("remaining_pct") is None:
        return CostTerm(
            kind="quota_pressure",
            amount=None,
            unit="dimensionless",
            status="unknown",
            notes="quota_evidence_unavailable",
        )
    remaining = float(evidence["remaining_pct"])  # type: ignore[arg-type]
    pressure = max(0.0, min(1.0, 1.0 - (remaining / 100.0)))
    amount = Decimal(str(pressure)).quantize(Decimal("0.000001"))
    return CostTerm(
        kind="quota_pressure",
        amount=amount,
        unit="dimensionless",
        status="observed",
        evidence_id=evidence.get("evidence_id"),
        notes=f"pool={evidence.get('pool_id', 'unknown')}",
    )


def subscription_opportunity_term(
    *,
    units: Decimal | int | str | None,
    status: CostTermStatus = "estimated",
    evidence_id: str | None = None,
) -> CostTerm:
    """Opportunity cost against a subscription pool (not metered cash)."""

    if units is None:
        return CostTerm(
            kind="subscription_opportunity",
            amount=None,
            unit="subscription_units",
            status="unknown",
            evidence_id=evidence_id,
        )
    amount = _require_non_negative(_as_decimal(units, "units") or Decimal("0"), "units")
    return CostTerm(
        kind="subscription_opportunity",
        amount=amount,
        unit="subscription_units",
        status=status,
        evidence_id=evidence_id,
    )


def _cache_terms(
    evidence: CacheEvidenceInput, *, cached_tokens: int | None = None
) -> list[CostTerm]:
    terms: list[CostTerm] = []
    evidence_id = evidence.get("evidence_id")
    evidence_id_str = evidence_id if isinstance(evidence_id, str) else None
    tokens = evidence.get("cached_input_tokens")
    if tokens is None:
        tokens = cached_tokens
    if tokens is not None:
        terms.append(
            CostTerm(
                kind="cache_read",
                amount=Decimal(int(tokens)),
                unit="tokens",
                status="observed",
                evidence_id=evidence_id_str,
            )
        )
    savings = evidence.get("savings_usd")
    if savings is None:
        # Never invent cash savings from a cache hit flag alone.
        if evidence.get("cache_hit") is True:
            terms.append(
                CostTerm(
                    kind="cache_read",
                    amount=None,
                    unit="usd",
                    status="unknown",
                    evidence_id=evidence_id_str,
                    notes="cache_hit_without_observed_savings",
                )
            )
        return terms
    if not isinstance(savings, (str, int, float, Decimal)):
        raise CostLedgerError("savings_usd must be a decimal-compatible value")
    amount = _require_non_negative(
        _as_decimal(savings, "savings_usd") or Decimal("0"), "savings_usd"
    )
    terms.append(
        CostTerm(
            kind="cache_read",
            amount=amount,
            unit="usd",
            status="observed",
            evidence_id=evidence_id_str,
            notes="observed_cache_savings",
        )
    )
    return terms


__all__ = [
    "COST_LEDGER_SCHEMA_VERSION",
    "CacheEvidenceInput",
    "CostLedger",
    "CostLedgerError",
    "CostTerm",
    "CostTermKind",
    "CostTermStatus",
    "CostUnit",
    "PriceEvidenceInput",
    "QuotaEvidenceInput",
    "Reservation",
    "ReservationStatus",
    "SpendPool",
    "quota_pressure_term",
    "subscription_opportunity_term",
    "tokens_to_usd_term",
]
