# capacity-from-facts-not-names

## Why

Operator, 2026-11-19 14:38Z: "no names should never determine if something is a free route or not".

Local rehearsal cert-fix10 (merged main 02ca50d3) proved 5 planner pools in its preflight. One was
`pol/...:free`, classified FREE by the `:free` suffix rule in `verdict/capacity.py`, which ran before
the credit-provider rule from `credit-routes-not-free` (#1133). When used, it refused with an in-band
payment_required, so chaos had 4 usable pools against a measured need of 5 and ended BLOCKED at
independent review.

The capacity classifier skipped the fact-based rules main already has:
`verdict/omniroute_pipeline/free_rule.py` (rule 0.2: FREE only on explicit 0/0 price or the
provider's own flag; a `:free` suffix fails closed) and the gateway's free-tier membership
(`/api/free-tier/summary`, already parsed into `OmniRouteAdmitSnapshot.free_tier`).

## What Changes

- `classify_route_capacity` decides from facts only: the OpenRouter public 0/0 set (BOD-418), the
  connection's declared free signals, oauth subscription, the gateway free-tier membership, and
  rule 0.2 on the declared price. The `:free` suffix rule, `DEFAULT_CREDIT_PROVIDERS`,
  `VERDICT_CREDIT_PROVIDERS` and the `assumed_free_no_paid_evidence` default are removed.
- No signal means `UNKNOWN` (`no_free_evidence`). Unknown is never free.
- OpenRouter routes without a loaded public free set are `UNKNOWN`
  (`openrouter_free_set_unavailable`), not METERED by provider name.
- `verdict serve` capacity passes the gateway free-tier membership to the classifier.
- Supersedes the `credit-routes-not-free` requirement that a credit provider's `:free` suffix stays
  FREE.
- Certification preflight: `PREFLIGHT_MAX_ROUTES` 13 -> 30. Request caps do not matter for free
  capacity (operator decision 2026-11-05, ai-memory
  `decisions/verdict-caps-subscription-free-2026-11-05.md`); the cap still bounds probes.

## Impact

Consumers of the capacity class: `verdict/intelligence.py` (`_route_capacity`, serve and relay
ordering), `verdict/orchestration/eligibility.py` (ladder ENTITLED/capacity filter and
`capacity_class_of`), `verdict/omniroute_evidence.py` (route capacities used by
`identify --screen --capacity free` and the certification preflight),
`verdict/orchestration/verified_models.py`, `verdict/prove_at_rest.py`. Routes that were FREE only
by name or by absence of paid evidence become UNKNOWN: they drop out of `--capacity free` filters
and sort after FREE/SUBSCRIPTION/METERED in capacity order. Admission ranking and review semantics
are unchanged.
