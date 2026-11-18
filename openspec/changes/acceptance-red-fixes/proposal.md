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

## What Changes

- `openai_health_probe` reports an in-band credit refusal as unhealthy `payment_required`, and
  the ladder scopes it to the one route: no pool or provider cooldown, no pool not-free override.
- `verdict prove-at-rest once --reprobe ROUTE` (repeatable) forces admitted FREE chat routes into
  the agentic phase. Named routes do not count against `--agentic-max-routes`; `--max-requests`
  still applies. A route-scoped billing negative clears the route's agentic proof.
- Selection reports `lease_held` with exit status 75 when no route was chosen and candidates were
  skipped for the lease. Acceptance waits for the lease (at most 10 minutes), retries once and
  records both attempts. `verdict-identify-screen.timer` runs at 20 past.
- The docs-refresh setup action starts `verdict-docs-refresh.service` once when the read-only
  preflight is not ready, and the doctor hint is `verdict memory docs --fix --repo-root <root>`.
- After an in-band refusal the relay may try the next admitted candidate without a caller
  idempotency key.

## Safety

- No admission widening: every change either narrows (probe, proof invalidation) or only changes
  how a failure is named and retried. A reprobe is limited to admitted FREE routes and to the
  request cap.
- The relay exception covers only an HTTP 200 refusal with zero completion tokens and no tool
  call. Every other failure keeps the idempotency rule.

## Not in this change

- Capacity classification is unchanged (ADR-042). Which evidence makes a route free or metered is
  an open operator decision and needs an ADR change first.
- A real HTTP 402 from the ladder health probe still cools the pool and the provider (BOD-334).
  Three existing tests pin that, one of them because it keeps a provider with many refusing
  routes from using up the probe budget. Only the HTTP 200 in-band refusal is route-scoped here.
