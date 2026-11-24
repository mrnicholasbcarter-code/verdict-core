# identify-screen-subscription-frontier design

## Context

Three independent bugs prevented `verdict identify --screen` from reaching
subscription frontier routes like `cc/claude-opus-5-5`:

1. `extra_ratings` used `route_identities[rid][0]` (the `/v1/models` `owned_by`
   field, e.g. `"anthropic"`) as the provider.  The `--provider` filter and
   `usable_providers` use the connection namespace (e.g. `"cc"`).  Frontier
   routes were always dropped by provider filters.

2. The tier filter called `(tier is None or tier > max_capability_tier)`.
   New models return `None` from `_capability_tier` because they are not yet
   in the classifier, so a new frontier model could never be screened.  Letting
   every no-tier route through is too broad, though: the screen spends real
   quota on subscription routes, and a no-tier route with no rating evidence
   may be a weak model that the ceiling exists to exclude.  A no-tier route
   passes only when fresh ratings place its model in the ADR-051 planner band.

3. `extra_ratings` required `live is not None` (route in OmniRoute's auto-combo
   pool).  Subscription frontier routes appear in `/v1/models` but not in the
   auto-combo endpoint.

## Goals / Non-Goals

**Goals:**
- Subscription frontier routes are included in the extra-ratings pool when
  their route-prefix provider has a usable connection.
- A no-tier route passes the `--max-capability-tier` filter only when its
  model is in the ADR-051 planner band (frontier or fallback) on fresh ratings;
  otherwise it keeps `skipped_unknown_tier`.
- Provider names in extra-ratings use the connection namespace consistently.
- Opaque routes (`auto/*`, `combo/*`) remain excluded.

**Non-Goals:**
- Changing admission semantics (unknown is never healthy, opaque routes are
  never candidates).
- Adding frontier routes to the ranked/traffic-proven pool; they still appear
  last (unrated, basis=unknown).
- Letting every no-tier route through the tier filter.
- Modifying `planner_band.py`, ranking, review, or any other verdict subsystem.

## Decisions

**Route-prefix as provider for subscription routes.** `rid.split("/", 1)[0]`
produces the same namespace as `live_candidates.provider` and `usable_providers`
(both come from connection data).  This is the correct namespace for
`--provider` filters and capacity checks.

**`usable_providers` guard for no-live path.** Only include a subscription
route when its prefix is in `usable_providers` — meaning at least one
connection for that provider is configured and usable.  This avoids adding
routes for providers with no active connection.

**Opaque guard before live check.** `is_opaque(rid) or is_non_chat_route(rid)`
runs before the `live` lookup so it applies equally to routes that happen to
appear in the inventory as opaque entries.

**Tier-filter semantics.** Three cases.  A known tier above the ceiling is
`skipped_capability_tier`, as before.  A route with no tier is
`skipped_unknown_tier` by default, the conservative reading of "unknown is
never healthy".  It passes the filter only when its stripped model key (the
route id without a trailing `:suffix`) is in `fallback_model_keys` from
`planner_band.compute_planner_bands`, used read-only; the fallback set
includes the frontier set.  The band is built from fresh `aa_intelligence`
ratings, meaning the newest batch inside the 12-hour window that module already
uses.  It is computed once per screen and only when `--max-capability-tier` is
set.  With no metadata, or only stale ratings, it is empty, so the screen falls
back to skipping every no-tier route.  Band membership is rating evidence that
the model is strong enough to be worth a probe.  It does not admit the route:
the probe still has to pass, and admission is unchanged.

## Risks / Trade-offs

Subscription frontier routes are expensive (they use real API quota when
probed).  The existing `max_routes`, `max_per_provider` and `--capacity`
filters remain the primary governors; this change only makes those routes
visible to the screen.  The band gate keeps a no-tier route from being probed
without rating evidence.

## Security / trust implications

No change to admission or eligibility.  The opaque guard prevents `auto/*` and
`combo/*` from reaching dispatch.

## Routing / context / memory implications

Extra-ratings routes remain `basis=unknown` and appear after traffic-proven
routes.  They do not affect the ranked pool or advisory signals.

## Migration / rollback

No migration required.  Behaviour for known-tier routes, and for no-tier
routes outside the planner band, is unchanged.  No-tier routes inside the band
are now probed (subject to existing caps).

## ADR impact

No ADR changes required.  The opaque-route invariant (ADR-0001) is preserved by
the opaque guard.
