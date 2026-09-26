"""Expected complete-strategy cost comparison (session economics (STAY/SWITCH)).

Compares **complete execution strategies**, not next-call price alone.
Cash, subscription opportunity, and quota pressure remain distinct dimensions.

Frozen consumer contract for BOD-119
------------------------------------
* ``ExpectedStrategyCost`` — strategy receipt with ``terms``, ``cash_usd``,
  ``subscription_opportunity``, ``quota_pressure``, and per-term statuses.
* ``CostPolicyMode`` — ``cheapest_qualified`` | ``expected_cost``.
* ``free_first`` — preference flag only; never an optimality claim when
  expected retries/escalation make another qualified path cheaper.
* ``compare_strategies`` / ``select_strategy`` — economics ranking helpers;
  eligibility/worthiness remain outside this module (BOD-107/109/104).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal

from verdict.cost_ledger import (
    COST_LEDGER_SCHEMA_VERSION,
    CostLedgerError,
    CostTerm,
    CostTermStatus,
    PriceEvidenceInput,
    QuotaEvidenceInput,
    quota_pressure_term,
    subscription_opportunity_term,
    tokens_to_usd_term,
)
from verdict.effective_capability import AssistanceCost

EXPECTED_COST_SCHEMA_VERSION = "1"

CostPolicyMode = Literal["cheapest_qualified", "expected_cost"]


def _format_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sum_usd(terms: Sequence[CostTerm], *, kinds: set[str] | None = None) -> Decimal | None:
    """Sum known USD terms. Returns None if any matching term is unknown."""

    total = Decimal("0")
    saw = False
    for term in terms:
        if term.unit != "usd":
            continue
        if kinds is not None and term.kind not in kinds:
            continue
        saw = True
        if term.status == "unknown" or term.amount is None:
            return None
        total += term.amount
    return total if saw else Decimal("0")


def _status_histogram(terms: Sequence[CostTerm]) -> dict[CostTermStatus, int]:
    counts: dict[CostTermStatus, int] = {"observed": 0, "estimated": 0, "unknown": 0, "assumed": 0}
    for term in terms:
        counts[term.status] += 1
    return counts


@dataclass(frozen=True)
class ExpectedStrategyCost:
    """Complete-strategy cost receipt for one qualified execution path."""

    strategy_id: str
    trajectory_id: str
    terms: tuple[CostTerm, ...]
    cash_usd: Decimal | None
    subscription_opportunity: Decimal | None
    quota_pressure: Decimal | None
    policy_mode: CostPolicyMode = "expected_cost"
    free_first_preferred: bool = False
    qualified: bool = True
    is_free: bool = False
    notes: str | None = None
    schema_version: str = EXPECTED_COST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.strategy_id or not str(self.strategy_id).strip():
            raise CostLedgerError("strategy_id must be non-empty")
        if not self.trajectory_id or not str(self.trajectory_id).strip():
            raise CostLedgerError("trajectory_id must be non-empty")
        if self.policy_mode not in {"cheapest_qualified", "expected_cost"}:
            raise CostLedgerError(f"invalid policy_mode: {self.policy_mode!r}")
        if not isinstance(self.terms, tuple):
            object.__setattr__(self, "terms", tuple(self.terms))

    @property
    def status_counts(self) -> dict[CostTermStatus, int]:
        return _status_histogram(self.terms)

    @property
    def resource_token_burden(self) -> Decimal:
        """Known token burden used only after expected cash ties."""
        return sum(
            (
                term.amount
                for term in self.terms
                if term.unit == "tokens" and term.amount is not None and term.status != "unknown"
            ),
            Decimal("0"),
        )

    @property
    def has_unknown_cash(self) -> bool:
        return any(
            term.unit == "usd" and (term.status == "unknown" or term.amount is None)
            for term in self.terms
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "ledger_schema_version": COST_LEDGER_SCHEMA_VERSION,
            "strategy_id": self.strategy_id,
            "trajectory_id": self.trajectory_id,
            "policy_mode": self.policy_mode,
            "free_first_preferred": self.free_first_preferred,
            "qualified": self.qualified,
            "is_free": self.is_free,
            "cash_usd": None if self.cash_usd is None else str(self.cash_usd),
            "subscription_opportunity": None
            if self.subscription_opportunity is None
            else str(self.subscription_opportunity),
            "quota_pressure": None if self.quota_pressure is None else str(self.quota_pressure),
            "status_counts": dict(self.status_counts),
            "terms": [term.to_dict() for term in self.terms],
            "notes": self.notes,
        }

    @classmethod
    def build(
        cls,
        *,
        strategy_id: str,
        trajectory_id: str,
        terms: Sequence[CostTerm],
        policy_mode: CostPolicyMode = "expected_cost",
        free_first_preferred: bool = False,
        qualified: bool = True,
        is_free: bool = False,
        notes: str | None = None,
    ) -> ExpectedStrategyCost:
        term_tuple = tuple(terms)
        cash = _sum_usd(term_tuple)
        sub = None
        pressure = None
        for term in term_tuple:
            if term.kind == "subscription_opportunity":
                if term.status == "unknown" or term.amount is None:
                    sub = None
                else:
                    sub = term.amount if sub is None else sub + term.amount
            if term.kind == "quota_pressure":
                if term.status == "unknown" or term.amount is None:
                    pressure = None
                else:
                    pressure = term.amount if pressure is None else max(pressure, term.amount)
        return cls(
            strategy_id=strategy_id,
            trajectory_id=trajectory_id,
            terms=term_tuple,
            cash_usd=cash,
            subscription_opportunity=sub,
            quota_pressure=pressure,
            policy_mode=policy_mode,
            free_first_preferred=free_first_preferred,
            qualified=qualified,
            is_free=is_free,
            notes=notes,
        )


@dataclass(frozen=True)
class StrategySelection:
    """Economics-only ranking result. Not a routing authority decision."""

    selected_strategy_id: str | None
    mode: CostPolicyMode
    free_first_preferred: bool
    reason: str
    ranked: tuple[str, ...]
    optimality_claimed: bool
    receipt: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected_strategy_id": self.selected_strategy_id,
            "mode": self.mode,
            "free_first_preferred": self.free_first_preferred,
            "reason": self.reason,
            "ranked": list(self.ranked),
            "optimality_claimed": self.optimality_claimed,
            "receipt": self.receipt,
        }


def build_strategy_from_assistance(
    *,
    strategy_id: str,
    trajectory_id: str,
    assistance: AssistanceCost,
    execution_tokens: int = 0,
    retry_tokens: int = 0,
    escalation_tokens: int = 0,
    route_switch_tokens: int = 0,
    price: PriceEvidenceInput | None = None,
    quota: QuotaEvidenceInput | None = None,
    subscription_units: Decimal | int | str | None = None,
    is_free: bool = False,
    qualified: bool = True,
    policy_mode: CostPolicyMode = "expected_cost",
    free_first_preferred: bool = False,
    now: datetime | None = None,
) -> ExpectedStrategyCost:
    """Build a strategy cost from AssistanceCost + optional execution overlays."""

    when = now or datetime.now(timezone.utc)
    terms: list[CostTerm] = []
    for kind, tokens in (
        ("planning", assistance.planning_tokens),
        ("hydration", assistance.context_tokens),
        ("tools", assistance.tool_tokens),
        ("verification", assistance.verification_tokens),
        ("execution", execution_tokens),
        ("retry", retry_tokens),
        ("escalation", escalation_tokens),
        ("route_switch", route_switch_tokens),
    ):
        if tokens <= 0:
            continue
        terms.append(
            CostTerm(
                kind=kind,
                amount=Decimal(tokens),
                unit="tokens",
                status="estimated",
                observed_at=when,
            )
        )
        terms.append(tokens_to_usd_term(tokens, kind=kind, price=price, now=when))

    terms.append(quota_pressure_term(quota))
    terms.append(subscription_opportunity_term(units=subscription_units))
    return ExpectedStrategyCost.build(
        strategy_id=strategy_id,
        trajectory_id=trajectory_id,
        terms=terms,
        policy_mode=policy_mode,
        free_first_preferred=free_first_preferred,
        qualified=qualified,
        is_free=is_free,
    )


def compare_strategies(
    strategies: Sequence[ExpectedStrategyCost],
    *,
    mode: CostPolicyMode = "expected_cost",
    free_first: bool = False,
) -> StrategySelection:
    """Rank qualified strategies under an explicit policy mode.

    ``free_first`` is a preference, not an optimality claim. Under
    ``expected_cost``, a free path loses when another qualified strategy has
    lower known expected cash (including retries/escalation).
    """

    qualified = [item for item in strategies if item.qualified]
    if not qualified:
        return StrategySelection(
            selected_strategy_id=None,
            mode=mode,
            free_first_preferred=free_first,
            reason="no_qualified_strategies",
            ranked=(),
            optimality_claimed=False,
            receipt={"strategies": [item.to_dict() for item in strategies]},
        )

    def sort_key(item: ExpectedStrategyCost) -> tuple[Any, ...]:
        cash = item.cash_usd
        # Unknown cash sorts last (conservative — never invent cheapness).
        cash_key = (1, Decimal("0")) if cash is None else (0, cash)
        sub = item.subscription_opportunity
        sub_key = (1, Decimal("0")) if sub is None else (0, sub)
        pressure = item.quota_pressure
        pressure_key = (1, Decimal("0")) if pressure is None else (0, pressure)
        resource_key = item.resource_token_burden
        if mode == "cheapest_qualified":
            # Tactical: minimize known cash, then the estimated token burden.
            return (cash_key, sub_key, pressure_key, resource_key, item.strategy_id)
        # Preserve the economic dimensions; resource burden breaks full economic ties.
        return (cash_key, sub_key, pressure_key, resource_key, item.strategy_id)

    ranked_items = sorted(qualified, key=sort_key)
    ranked_ids = tuple(item.strategy_id for item in ranked_items)

    if free_first:
        free_candidates = [item for item in ranked_items if item.is_free]
        if mode == "cheapest_qualified" and free_candidates:
            selected = free_candidates[0]
            return StrategySelection(
                selected_strategy_id=selected.strategy_id,
                mode=mode,
                free_first_preferred=True,
                reason="free_first_preference_cheapest_qualified",
                ranked=ranked_ids,
                optimality_claimed=False,
                receipt={
                    "selected": selected.to_dict(),
                    "strategies": [item.to_dict() for item in strategies],
                    "note": "free_first is preference, not expected-cost optimality",
                },
            )
        if mode == "expected_cost":
            # Preference only when free path is not dominated on known cash.
            best = ranked_items[0]
            if free_candidates:
                free = free_candidates[0]
                if (
                    free.cash_usd is not None
                    and best.cash_usd is not None
                    and free.cash_usd <= best.cash_usd
                ):
                    return StrategySelection(
                        selected_strategy_id=free.strategy_id,
                        mode=mode,
                        free_first_preferred=True,
                        reason="free_first_not_dominated_on_expected_cash",
                        ranked=ranked_ids,
                        optimality_claimed=False,
                        receipt={
                            "selected": free.to_dict(),
                            "strategies": [item.to_dict() for item in strategies],
                        },
                    )
                # Free path has higher expected cash (retries/escalation) → do not claim free-first optimal.
                return StrategySelection(
                    selected_strategy_id=best.strategy_id,
                    mode=mode,
                    free_first_preferred=True,
                    reason="expected_cost_beats_free_first_preference",
                    ranked=ranked_ids,
                    optimality_claimed=True,
                    receipt={
                        "selected": best.to_dict(),
                        "rejected_free": free.to_dict() if free_candidates else None,
                        "strategies": [item.to_dict() for item in strategies],
                        "note": "free_first preference deferred; expected-cost optimality retained",
                    },
                )

    selected = ranked_items[0]
    unknown_blocks = selected.cash_usd is None
    return StrategySelection(
        selected_strategy_id=None
        if unknown_blocks and mode == "expected_cost"
        else selected.strategy_id,
        mode=mode,
        free_first_preferred=free_first,
        reason=(
            "selected_expected_cost_unknown_cash_conservative"
            if unknown_blocks and mode == "expected_cost"
            else f"selected_{mode}"
        ),
        ranked=ranked_ids,
        optimality_claimed=not unknown_blocks and mode == "expected_cost",
        receipt={
            "selected": selected.to_dict(),
            "strategies": [item.to_dict() for item in strategies],
        },
    )


def select_strategy(
    strategies: Sequence[ExpectedStrategyCost],
    *,
    mode: CostPolicyMode = "expected_cost",
    free_first: bool = False,
) -> StrategySelection:
    """Alias for :func:`compare_strategies` (stable session STAY/SWITCH decisions name)."""

    return compare_strategies(strategies, mode=mode, free_first=free_first)


__all__ = [
    "EXPECTED_COST_SCHEMA_VERSION",
    "CostPolicyMode",
    "ExpectedStrategyCost",
    "StrategySelection",
    "build_strategy_from_assistance",
    "compare_strategies",
    "select_strategy",
]
