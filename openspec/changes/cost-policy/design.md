# Design — operator cost policy (CP1)

## Policy schema

```yaml
cost_policy:
  version: 1
  order: [free, subscription, metered, unknown]   # shipped default = today's order
  unknown_mode: probe_only                        # assume_free | probe_only | exclude
  unknown_daily_call_cap: 20                      # per UNKNOWN route per UTC day, until its first cost fact; 0 = no cap
  metered:
    enabled: true
    spend_cap_usd: null                           # null = no Verdict-side cap
  subscription_reserve:
    enabled: false
    threshold_percent: 20
```

The policy is validated on load. Validation rejects:

- an `order` that is not a permutation of the four classes;
- an unknown mode;
- a negative cap.

A rejected policy fails closed: the command exits and names the bad key. It never falls back silently. The policy version is `v<version>-<sha256 of the normalised YAML>[:12]`.

This operator's file sets `order: [free, unknown, subscription, metered]` and `unknown_mode: assume_free`.

## One order

`verdict/cost_policy.py` owns the order. `CAPACITY_ORDER` becomes a function of the loaded policy, and every alias reads it:

- `eligibility._CAPACITY_ORDER`;
- `candidate_pool`;
- `verified_refresh.CAPACITY_ORDER`, which today has `UNKNOWN` before `METERED`;
- `model_refresh` and `identify_pools`.

The two `RouteSelection` assertions check the policy order: picking a route while a kept route earlier in the order exists raises.

## Unknown-cost modes

| Mode | Admission of an `UNKNOWN` route |
|---|---|
| `assume_free` | Same evidence as `FREE`: tool proof before dispatch, the same proof bar. Ranked where `order` puts `UNKNOWN`. Subject to `unknown_daily_call_cap`. |
| `probe_only` | Only after a consented, budgeted probe (ADR-012) succeeded with no observed charge. Discovery refresh probes `UNKNOWN` routes in this mode. |
| `exclude` | Never a candidate. |

The class stays `UNKNOWN` in decisions and receipts in every mode. A cost fact reclassifies the route and lifts the cap:

- a positive declared price;
- a billing fact from CP2;
- a 402 billing refusal;
- a nonzero observed charge.

A zero cost header or a zero call-log cost is not a fact (N45, ADR-049).

## Cap

The cap counts dispatched calls per route per UTC day in the attempt ledger, which Verdict already writes. A route at its cap is skipped with the reason `unknown_daily_cap` and stays visible in the receipt.

## Subscription reserve

The reserve reads only quota readings Verdict already takes (`/api/usage/{connection}` in `orchestration/run.py`); there is no new quota endpoint. A reading of `null` (OmniRoute 3.8.54 reports unknown quota as `percentRemaining: null`) or a read error is "unreadable". It does not trigger the reserve, and the receipt says `reserve_unreadable`.

## Flags

Caller flags narrow only (the shared-selection contract). `--include-unknown` and `--allow-metered`:

- do nothing when the policy already admits the class;
- when the policy forbids the class, exit with a message naming `cost_policy.unknown_mode` or `cost_policy.metered.enabled`.

`free_only` still narrows to `FREE`. Under `assume_free` it also admits `UNKNOWN`, because ADR-049 treats a route with no cost facts as free. The receipt names both classes.

## Receipts

Each selection records:

- `cost_policy_version`;
- `cost_class`;
- the deciding fact;
- for `UNKNOWN`, the mode that admitted it.

## Spec alignment

The main spec's requirement "Assumed-free capacity is not capability proof" (route-selection) says such routes are `FREE` with `assumed_free_no_paid_evidence`. The code says `UNKNOWN` with `no_free_evidence`. This change rewrites the requirement to match ADR-049: the class is `UNKNOWN`, and the mode decides its use.

## Acceptance (from #1244)

- With the shipped default policy, a read-only route on the live catalogue before and after shows no difference.
- With this operator's policy, selection follows `free, unknown, subscription, metered`.
