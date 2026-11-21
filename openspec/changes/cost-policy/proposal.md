# Operator cost policy (CP1)

**Ticket:** #1244 (story CP1). **Decision:** ADR-049 (accepted 2026-11-21, `docs/adr/ADR-049-cost-policy-and-unknown-handling.md`).
**Depends on:** closeout batch B closed on main. CP2 (#1245, `connection-billing-facts`) ships before or with this change.

## Why

`verdict/capacity.py::CAPACITY_ORDER` hard-codes one cost order (`FREE`, `SUBSCRIPTION`, `METERED`, `UNKNOWN`). It also ranks routes with no cost facts last, which contradicts ADR-049: a route is PAID only from declared or observed facts. `verdict/orchestration/verified_refresh.py` keeps a second, different order. No operator can change the order, protect subscription quota, cap metered spend, or choose how routes with no cost facts are used.

## What changes

- A `cost_policy` section in Verdict config sets:
  - `order`: the class order;
  - `unknown_mode`: `assume_free`, `probe_only` or `exclude`;
  - `unknown_daily_call_cap`: a per-route daily call cap for `UNKNOWN` routes before their first cost fact;
  - `metered.enabled` and `metered.spend_cap_usd`;
  - `subscription_reserve.enabled` and `subscription_reserve.threshold_percent`.
- The shipped defaults follow ADR-049's settled points: `probe_only`, metered on, the cap on, the reserve off. The default order is today's order, so selection with the default policy is unchanged. This operator's config sets `order: [free, unknown, subscription, metered]` and `unknown_mode: assume_free`.
- `CAPACITY_ORDER` and the `verified_refresh` order are replaced by one read of the policy. Every caller ranks by the policy's order.
- Under `assume_free`, an `UNKNOWN` route is admitted on the same evidence a `FREE` route needs (tool proof before dispatch). Under `probe_only` it needs a consented, budgeted probe that succeeded with no charge. Under `exclude` it is never a candidate.
- The subscription reserve stops worker nodes from using a subscription route whose remaining window is below the threshold. Planner and reviewer roles may still use it. An unreadable quota never triggers the reserve, and the receipt says so.
- Caller flags can only narrow. `--include-unknown` and `--allow-metered` cannot widen past the policy. When the policy already allows the class they do nothing; when it forbids it, the command says which policy key to change.
- Each receipt records the policy version (a hash of the normalised policy) and the cost class of each selection.
- The AGENTS.md cost row becomes "A route is PAID only from declared or observed facts, never from its name", in this change, the one that makes it true.

## Impact

- Code:
  - `verdict/capacity.py`;
  - the new `verdict/cost_policy.py`;
  - `verdict/orchestration/eligibility.py`, `verdict/candidate_pool.py`, `verdict/orchestration/verified_refresh.py`, `verdict/actions/model_refresh.py` and `verdict/identify_pools.py`;
  - `verdict/autodev_routing.py::RouteSelection` and `verdict/live_routing.py::RouteSelection`;
  - `verdict/task_profile.py`, the receipt writers, and `docs/CONFIGURATION.md`.
- Tests: the order and identity tests in `tests/test_serve_capacity_order.py`, plus `tests/test_catalog_truth.py`, `tests/test_one_capacity_classifier.py` and `tests/test_orchestration_capacity_bounds.py`. New tests cover the policy schema, the modes, the cap, the reserve and narrowing-only flags.
