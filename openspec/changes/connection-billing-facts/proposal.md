# Capacity from connection billing facts (CP2)

**Ticket:** #1245 (story CP2). **Decision:** ADR-049, cost policy (PR #1249).
**Depends on:** closeout batch B closed on main (`one-capacity-classifier`, `capacity-from-facts-not-names` and the rest of the selection batch). Ships before or with CP1 (#1244).

## Why

`verdict/capacity.py::classify_route_capacity` makes every OAuth connection
`SUBSCRIPTION` (deciding fact `oauth_subscription`). OmniRoute's own curated
billing catalog (`open-sse/config/connectionBillingCatalog.ts`, shipped with the
package) says `authType` "is NOT a safe proxy in either direction": metered OAuth
connections exist, and plan-included API-key connections exist. A wrong class
spends the wrong capacity: subscription quota on what should be metered, or the
reverse.

Three more gaps sit next to it:

- `verdict/orchestration/provider_catalog.py::connection_signals_free` treats the
  substring `free` in `plan_label`, `providerSpecificData.tier` or
  `providerSpecificData.plan` as a free fact. That is a label match, which the
  "never from its name" invariant forbids.
- The gateway's free-tier facts carry a `freeType` per model. Verdict reads only
  membership, so a `one-time-initial` credit counts as free supply the same way a
  recurring daily allowance does.
- Overage behaviour is not recorded, so a subscription route that bills past its
  window looks cost-free after the window is spent.

## What changes

- `capacity_of` decides `SUBSCRIPTION` and `METERED` from connection billing
  facts, not `authType`:
  - `subscription` gives `SUBSCRIPTION`, with deciding fact `billing_catalog_subscription`.
  - `metered` gives `METERED`, with deciding fact `billing_catalog_metered`.
  - `keyless` gives `FREE`, with deciding fact `billing_catalog_keyless`.
  - `unknown`, or no entry, is not a fact. The other fact rules run, and without one the route is `UNKNOWN`.
- The facts come from a pinned snapshot of OmniRoute's `CONNECTION_BILLING_CATALOG`
  (`verdict/data/omniroute_connection_billing.json`). It records the gateway
  version and the SHA-256 of the source file. On top of it sits an optional
  operator overlay of declared facts in Verdict's config. An auth-typed entry wins
  over a provider-wide one, as in OmniRoute.
- Each route records its overage (`hard-stop`, `meters-to-paid` or `unknown`). A
  subscription route whose overage is not `hard-stop` is not cost-free once its
  window is spent.
- Free facts record `freeType` and `hardStopGuaranteed` from
  `/api/free-tier/summary` `perModel`. Free-supply accounting counts
  `one-time-initial` credits separately from recurring free supply. A
  `discontinued` entry is not a free fact.
- The `plan_label`, `tier` and `plan` substring rules are removed from
  `connection_signals_free`.
- `verdict doctor` reports drift when the installed gateway version or the
  catalog file's hash differs from the snapshot.
- A read-only `verdict discovery connections` lists every connection with its
  class, deciding fact and overage. It makes no model calls.
- There is still exactly one classifier, `verdict/capacity.py::capacity_of`.

## Impact

- Code: `verdict/capacity.py`, `verdict/orchestration/provider_catalog.py`, the
  free-tier reader in `verdict/omniroute_evidence.py`, `verdict/doctor*`, the
  discovery commands, and the new `verdict/data/omniroute_connection_billing.json`.
- Selection: OAuth connections absent from the catalog stop being `SUBSCRIPTION`.
  Under ADR-049's `assume_free` mode they become `UNKNOWN`, ranked where the
  operator's order puts `UNKNOWN`. The live diff shows which connections move.
- Tests: `tests/test_one_capacity_classifier.py`, `tests/test_serve_capacity_order.py`,
  `tests/test_catalog_truth.py`, plus new tests for the snapshot, the overlay, the
  overage and drift.
