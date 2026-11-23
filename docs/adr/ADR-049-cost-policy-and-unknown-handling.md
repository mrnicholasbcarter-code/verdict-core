# ADR-049: Cost policy, unknown-cost handling and per-operator cost ordering

**Status:** Accepted (operator, 2026-11-21: "the rule should say paid only from facts"; open points settled the same day)
**Date:** 2026-11-21
**Deciders:** Operator
**Supersedes:** the 2026-11-19 rule "unknown is never free" (docstring of
`verdict/capacity.py::classify_route_capacity`, `docs/CONFIGURATION.md` "Capacity classification")
**Relates to:** [ADR-043](ADR-043-role-dependent-selection-order.md) (cheapest sufficient first),
[ADR-0001](0001-verdict-control-plane-invariants.md) (control-plane invariants),
decision log D15 and D32, issues #1243 (this ADR), #1244 (CP1), #1245 (CP2), PR #1242 (ADR-048, not on main)

## Context

Verdict exists to send each step to the cheapest model that can do it (D15: free tokens are a
force multiplier). Today the classifier in `verdict/capacity.py` puts a route with no cost facts
in `UNKNOWN` with the evidence `no_free_evidence`, and `CAPACITY_ORDER` ranks `UNKNOWN` last,
after `METERED`. Most OmniRoute routes have no declared price, so most of the free supply the
operator has (keyless nodes, free tiers, community routes) is ranked below pay-per-token routes
or excluded by a free-only spend policy.

The operator's intent is the opposite: "prove that they're not free, not proven free"
(2026-11-20), and "the rule should say paid only from facts" (2026-11-21).

Two facts limit what Verdict can observe about cost:

- **N45** (`docs/decisions/NEGATIVE_RESULTS.md`, OmniRoute 3.8.51, 2026-10-15): the OmniRoute
  call-log cost field read 0 for the Anthropic provider while real tokens were billed. A zero cost
  field proves nothing.
- **The cost header is an estimate.** OmniRoute's `x-omniroute-response-cost` is computed from
  OmniRoute's own price table (its UI labels it "Estimated cost — real billing may vary"). It
  reads 0 for every route OmniRoute has no price for, which is most `UNKNOWN` routes. A nonzero
  value is evidence of a charge; a zero is not evidence of anything.

## Decision

### Goal

For each step, pick the model most likely to succeed at the lowest cost to the operator.

### The rule

**A route is PAID only from declared or observed facts, never from its name.** A route with no
cost facts is treated as free. A name never decides either way: a `:free` suffix does not make a
route free, and a provider name or a model name does not make it paid.

The cost class may still read `UNKNOWN` in decisions and receipts, so the evidence gap stays
visible. `UNKNOWN` is a statement about evidence, not about price.

### Cost classes and their order

The classes stay `FREE`, `SUBSCRIPTION`, `METERED` and `UNKNOWN`. Their order is a per-operator
policy, read from configuration, not fixed in code. This operator's order is
`FREE`, `UNKNOWN`, `SUBSCRIPTION`, `METERED`: free first, then routes with no cost facts, then
subscription quota, then pay-per-token.

### Unknown-cost mode

A policy mode decides how `UNKNOWN` routes are used:

| Mode | Meaning |
|---|---|
| `assume_free` | `UNKNOWN` routes are used as free. They are admitted on the same evidence a `FREE` route needs (tool proof before dispatch) and ranked where the operator's order puts them. |
| `probe_only` | `UNKNOWN` routes are used only after a consented, budgeted probe ([ADR-012](ADR-012-consented-budgeted-probes.md)) succeeds and shows no charge. |
| `exclude` | `UNKNOWN` routes are never candidates. |

This operator's mode is `assume_free`.

### What proves a route is paid

Any one of these makes a route `METERED` (or `SUBSCRIPTION` when the fact is a subscription
connection):

1. A positive declared price in the catalog row of an API-key connection.
   *(operator, 2026-11-23: list price counts for API-key connections only; an
   account/OAuth connection with no billing catalog entry stays UNKNOWN until
   an observed fact appears — a list price alone is not sufficient evidence
   that the operator will be billed for OAuth usage.  Implemented in CP2
   #1268.)*
2. A billing fact of `metered` from OmniRoute's curated `connectionBillingCatalog`.
3. A billing refusal: `billing_402_metered` evidence in the health cache.
4. A nonzero observed charge: a nonzero cost in the call log, or a nonzero
   `x-omniroute-response-cost` header.

A zero cost field, a zero cost header, an absent price, or a route's absence from a published free
list is not proof either way.

### Accepted risk

A pay-as-you-go account that bills silently (no error, no declared price, no priced entry in
OmniRoute's table) is not detected by any of the facts above. Under `assume_free` such a route is
used as free until a fact appears. This operator accepts that risk.

## Settled points (operator, 2026-11-21)

1. **Default mode for other operators: `probe_only`.** A new operator's accounts are unknown to
   Verdict, and `probe_only` keeps a silently billing account from being used before a probe has
   run. This operator sets `assume_free` explicitly.
2. **Pay-per-token routes: on by default.** `METERED` routes are candidates, ranked by the
   operator's cost order (last in the default order). An operator can turn them off in policy.
   This operator's standing rule of no pay-per-token spend is unchanged.
3. **A cap on `UNKNOWN` routes before their first fact: yes.** Each `UNKNOWN` route gets a
   per-route daily call cap, set in configuration, so a silently billing route cannot run up an
   unbounded bill. The cap lifts when a fact classifies the route. CP1 sets the default value.

## Consequences

- The free supply the operator already has becomes usable without a manual allow-list.
- A silently billing account can spend money under `assume_free` until a fact appears. The cap in
  settled point 3 bounds that spend; the receipts keep `UNKNOWN` visible so the gap can be audited.
- Cost order becomes configuration, so tests that pin `CAPACITY_ORDER` change in CP1.
- The AGENTS.md invariant row changes when the code makes it true. Until CP1 lands, main still
  ranks `UNKNOWN` last, so the row says what main enforces: a name never decides a route's cost
  class, in either direction. CP1 changes the row to "A route is PAID only from declared or
  observed facts, never from its name" in the same pull request that changes the behaviour.

### Rejected alternatives

- **Keep "unknown is never free".** It contradicts the operator's intent and wastes the free
  supply (D15).
- **Relabel `UNKNOWN` routes as `FREE`.** It would destroy the evidence gap that receipts need, and
  it would make the free-from-facts test meaningless.
- **Treat a zero cost header as proof of free** (the approach in PR #1242, ADR-048). The header is
  an estimate that reads 0 for every unpriced route, so zero proves nothing (N45). ADR-048's other
  rule (a nonzero header proves a charge) is kept here as fact 4. Recommended disposition of
  #1242: close it and fold its useful parts into CP1.
- **A predictor model or a classifier call per step.** Out of scope for the build order (#1233).

## Code and tests the implementation changes

CP2 (#1245, classifier reads OmniRoute billing facts) and CP1 (#1244, cost policy) change these.
This ADR changes none of their behaviour.

| Where | What changes |
|---|---|
| `verdict/capacity.py::classify_route_capacity` | Docstring rule "unknown is never free" (marked superseded by this ADR). The `openrouter_not_in_public_free_set` branch decides `METERED` from absence from the free set; under this ADR `METERED` needs a positive catalog price (CP2). |
| `verdict/capacity.py::CAPACITY_ORDER` | Fixed order becomes the operator's configured order (CP1). Aliases: `verdict/orchestration/eligibility.py::_CAPACITY_ORDER`, `verdict/candidate_pool.py`. |
| `verdict/orchestration/verified_refresh.py::CAPACITY_ORDER` | A second, different order (`UNKNOWN` before `METERED`); CP1 makes both read one policy. |
| `verdict/actions/model_refresh.py`, `verdict/identify_pools.py` | Read `CAPACITY_ORDER` for grouping and approval lists. |
| `verdict/orchestration/eligibility.py`, `verdict/orchestration/runtime.py`, `verdict/candidate_pool.py` | Places that treat `UNKNOWN` specially at admission, dispatch and pool building; CP1 routes them through the unknown-cost mode. |
| `docs/CONFIGURATION.md` "Capacity classification" | "unknown is never free" becomes the mode description (CP1). |
| `tests/test_serve_capacity_order.py` | In CP1, `::test_route_name_never_makes_a_route_free` keeps its name and its docstring drops "unknown is never free" (this ADR's PR leaves it unchanged). `::test_route_name_never_makes_a_route_paid` (added in this ADR's PR) stays as is. Tests asserting the fixed order (the identity of `CAPACITY_ORDER` across modules, `capacity_order == 2` for the last tier) follow the configured order. |
| `tests/test_catalog_truth.py`, `tests/test_one_capacity_classifier.py`, `tests/test_orchestration_capacity_bounds.py`, `tests/test_authoritative_eligibility.py` | Comments and assertions that say unknown is never free, or that rank by the fixed order. |
| `AGENTS.md` invariant row | Becomes "A route is PAID only from declared or observed facts, never from its name" in CP1. |
