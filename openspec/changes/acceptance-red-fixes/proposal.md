# acceptance-red-fixes: defects that kept live acceptance red

## Why

Live acceptance on the installed build stayed red for reasons that were defects in Verdict, not
in the gateway:

- The ladder's one-token health probe accepted any non-empty text, so an HTTP 200 whose text was
  the provider's credit refusal counted as healthy.
- No command could probe a route again while its 24 hour agentic proof was fresh, and a refused
  route kept that proof.
- `verdict route` and `verdict choose` reported `no_eligible_route` when the only thing wrong was
  that another producer held `verified-refresh.lock`. The six-hourly identify screen started on
  the hour, the same minute as the hourly acceptance run.
- After every install the documentation index was stale until the six-hourly timer ran, and the
  doctor hint named a command that does not refresh it.
- The relay did not try a second admitted route after an in-band refusal when the caller sent no
  idempotency key, which is what the acceptance serve check does.
- A `:free` suffix was treated as proof that a route is free (operator rule, 2026-11-18: it is
  not).

## What Changes

- `openai_health_probe` reports an in-band credit refusal as unhealthy `payment_required`, and
  the ladder scopes it to the one route: no pool or provider cooldown, no pool not-free override.
- `verdict prove-at-rest once --reprobe ROUTE` (repeatable) forces admitted FREE chat routes into
  the agentic phase, inside `--agentic-max-routes` and `--max-requests`. A route-scoped billing
  negative clears the route's agentic proof.
- Selection reports `lease_held` with exit status 75 when no route was chosen and candidates were
  skipped for the lease. Acceptance waits for the lease (at most 10 minutes), retries once and
  records both attempts. `verdict-identify-screen.timer` runs at 20 past.
- The docs-refresh setup action starts `verdict-docs-refresh.service` once when the read-only
  preflight is not ready, and the doctor hint is `verdict memory docs --fix --repo-root <root>`.
- After an in-band refusal the relay may try the next admitted candidate without a caller
  idempotency key.
- `classify_route_capacity` no longer returns FREE from the `:free` suffix. The evidence value
  `free_suffix` is removed; `openrouter_public_zero_alias` and `free_suffix_unverified` are new.

## Safety

- No admission widening: every change either narrows (probe, proof invalidation, capacity) or
  only changes how a failure is named and retried. A reprobe is limited to admitted FREE routes
  and to the existing route and request caps.
- The relay exception covers only an HTTP 200 refusal with zero completion tokens and no tool
  call. Every other failure keeps the idempotency rule.
- Capacity: kc, kilocode, cl and cline `:free` routes outside the public OpenRouter 0/0 set are
  now SUBSCRIPTION (their OAuth connection), and credit-provider `:free` routes without evidence
  are UNKNOWN. This reduces the FREE supply to routes with evidence.

## Not in this change

- Observed zero cost (OmniRoute webhook cost, response cost header) is not yet recorded per
  route, so it is not a FREE evidence source here.
- Other name-based `:free` uses are unchanged: `free_route_harvest.free_status`,
  `free_tier_admit._looks_free_by_name`, `workers.py` cheap-model selection,
  `omniroute_catalog` ordering and `omniroute_catalog_stats` counts.
