"""Thin compatibility alias for continuity proof command ``pytest tests/test_budget.py``.

Canonical coverage lives in ``tests/test_cost_ledger.py`` and
``tests/test_expected_cost.py``. This module re-exports those proof cases so
older Continuity checklists keep working.
"""

from __future__ import annotations

from tests.test_cost_ledger import (  # noqa: F401
    test_assistance_cost_attribution_same_trajectory,
    test_cached_tokens_never_invent_savings,
    test_integer_decimal_arithmetic_and_unknown_price,
    test_partial_and_missing_billing_reconcile,
    test_protected_cheaper_inference_budget,
    test_quota_vs_cash_are_distinct_pools,
    test_reservation_race_is_atomic,
    test_stale_prices_are_assumed_conservative,
    test_subscription_pressure_distinct_from_cash,
)
from tests.test_expected_cost import (  # noqa: F401
    test_cheapest_qualified_vs_expected_cost_modes,
    test_free_first_preference_vs_expected_cost_optimality,
    test_strategy_receipt_shows_observed_estimated_unknown_assumed,
    test_subscription_and_quota_dimensions_on_strategy,
)
