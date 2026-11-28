# Design — a provider's quota across its connections

## Context

A provider can have several connections (accounts). The gateway balances requests across the usable ones, so a provider can serve while one account is out of quota. The ladder's AVAILABLE stage reads each connection's remaining quota for the route (its own model window, else the account's) and has to turn those readings into one decision for the route.

## Goals / Non-Goals

Goals:

- A provider's routes are rejected for quota only when no usable connection can serve them.
- Unknown quota stays unknown at every step of the fold.
- The cooldown comes from the connections that are out, not from the whole provider.

Non-Goals:

- Reading more of the gateway's usage report as measured quota. Windows that carry only `remainingPercentage` stay unknown for a route.
- Any change to the other AVAILABLE rules (cooldowns, `provider_rate_limited`), to ranking, or to any other stage.

## Decisions

1. **One fold, three answers.** `provider_quota(connections, provider, route_id, floor=...)` returns `has_quota` (a usable connection measures at or above the floor), `exhausted` (every usable connection is measured and below it) or `unknown` (anything else: no usable connection, or an unmeasured connection with no measured one at or above the floor). Only `exhausted` rejects.
2. **Usable means active with a test status that is not failed.** The failed set is the one `verdict/admission.py` already uses for "viable". An inactive or failed connection contributes no reading and no reset.
3. **Unknown is neither.** An unmeasured connection is never counted as quota left, and never as exhausted. With one unmeasured and one exhausted connection the answer is `unknown`, the route is not rejected here, and the quota percent is reported as unknown; the gateway decides at request time. This keeps "unknown is never healthy" and "never rejected on a guess".
4. **The percent is the lowest measured value, or unknown.** For `has_quota` and `exhausted` it is the lowest measured reading, as before (ranking uses the worst account; that is not changed here). For `unknown` it is empty.
5. **The cooldown is the earliest reset among the exhausted connections.** Because the answer is `exhausted` only when every usable connection is exhausted, this is the earliest time any of them can serve again. A reset that is not in the future gives no cooldown time, as before.
6. **Where it runs.** The fold replaces the inline code in the AVAILABLE stage, after the admission, entitlement, health and cooldown checks and before the task gate. A route that fails any other stage fails it exactly as before; the change can only remove one rejection.

## Risks / Trade-offs

- A provider whose connections are mostly unmeasured (the Plus accounts today) is no longer rejected for quota even if some of them are in fact empty. Accepted: the previous rule rejected it on one measured account's reading, and the gateway answers a request on a connection with quota. A failed dispatch is bounded and recovers by reassignment; a wrongly rejected provider is never tried.
- The `testStatus` set is shared with admission by import, so the two cannot drift.

## Security / trust implications

The quota readings come from the gateway's usage report, which Verdict already reads and reduces to numbers and one timestamp per connection. The change reads no new field and records no new text. It can only stop one rejection; it authorises no route by itself, because every later stage and the gateway's own request-time checks still apply.

## Routing / context / memory implications

Routing: a provider with quota left on at least one usable account, or with an unmeasured account, now passes the AVAILABLE stage, so its routes compete in ranking on the same terms as any other. Ranking, capacity class and the task gate are untouched. Context and memory are not involved.

## Migration / rollback

No stored state or schema changes. Reverting restores the earlier fold, which rejects a provider on the measured connections alone.

## ADR impact

None. This follows the existing rules: unknown is never healthy, and the admitted set only narrows.
