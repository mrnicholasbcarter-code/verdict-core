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
   in the classifier.  The screen was the tool designed to discover them, so
   blocking at the tier filter was circular.

3. `extra_ratings` required `live is not None` (route in OmniRoute's auto-combo
   pool).  Subscription frontier routes appear in `/v1/models` but not in the
   auto-combo endpoint.

## Goals / Non-Goals

**Goals:**
- Subscription frontier routes are included in the extra-ratings pool when
  their route-prefix provider has a usable connection.
- Unknown-tier routes pass the `--max-capability-tier` filter.
- Provider names in extra-ratings use the connection namespace consistently.
- Opaque routes (`auto/*`, `combo/*`) remain excluded.

**Non-Goals:**
- Changing admission semantics (unknown is never healthy, opaque routes are
  never candidates).
- Adding frontier routes to the ranked/traffic-proven pool; they still appear
  last (unrated, basis=unknown).
- Modifying ranking, review, or any other verdict subsystem.

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

**Tier-filter semantics.** The screen's purpose is to produce tool proofs for
unproven routes.  Blocking an unknown-tier route at the filter means it can
never be discovered.  The correct semantics: `tier is not None and tier > N`.
Routes whose tier is a known value above the ceiling are still excluded.

## Risks / Trade-offs

Subscription frontier routes are expensive (they use real API quota when
probed).  The existing `max_routes`, `max_per_provider` and `--capacity`
filters remain the primary governors; this change only makes those routes
visible to the screen.

## Security / trust implications

No change to admission or eligibility.  The opaque guard prevents `auto/*` and
`combo/*` from reaching dispatch.

## Routing / context / memory implications

Extra-ratings routes remain `basis=unknown` and appear after traffic-proven
routes.  They do not affect the ranked pool or advisory signals.

## Migration / rollback

No migration required.  The change is backward-compatible: screens that
previously skipped unknown-tier routes now probe them (subject to existing
caps).

## ADR impact

No ADR changes required.  The opaque-route invariant (ADR-0001) is preserved by
the opaque guard.
