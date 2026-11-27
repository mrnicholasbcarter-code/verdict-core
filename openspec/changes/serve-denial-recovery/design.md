# Design — recovering from a serve route denial

## Context

Workers on the OpenCode harness send their model calls through `verdict serve`, which applies its own per-route gates to the route the orchestrator assigned. When it refuses, it answers 503 with `assigned route '<id>' denied: <code> (<detail>)`. Part 1 (#1374) made the executor end the attempt on the first permanent code or the second identical other code and return a failed terminal that carries the text. The orchestrator's failure classifier does not read the text, and the orchestrator's ladder and serve do not share every rule, so the ladder can keep offering a route serve will keep refusing.

## Goals / Non-Goals

Goals:

- A serve denial is its own failure category, not a transient upstream fault.
- A route serve refuses for a permanent reason is not offered again in the same run, to any node.
- A denial writes nothing that outlives the run: no health-cache entry, no persisted cooldown.
- A real upstream 5xx behaves exactly as before.

Non-Goals:

- Keeping serve-refused identities out of selection in the first place (needs the operator's answer on whether `claude/*` is opaque by policy; tracked on #1354).
- A non-retryable HTTP status for lane denials (an ADR-053 amendment).
- Any change to executors that do not go through serve.

## Decisions

1. **New category `route_denied`.** The classifier recognises the denial text with the existing matcher in `opencode_denial.py`, for the terminal's own route only, and only on a 503. It returns `REROUTE`, scope `none`, cooldown 0. Scope `none` is what keeps the existing code from persisting a cooldown or writing session feedback; no second switch is added.
2. **The code decides the reach.** Permanent codes are the set already defined for part 1 (`PERMANENT_DENIAL_CODES`). `provider_unhealthy` is named explicitly. Every other code, including one this version has never seen, gets the narrowest treatment.
3. **Permanent: narrow the admitted set.** The ladder excludes the route with `AdmittedSet.exclude` at the `DOWNSTREAM` stage and the reason `serve_denied:<code>`. The set only narrows; the next run builds a fresh one. A later node's selection record then shows the route's first failed stage and that reason, with no new event type.
4. **Temporary: a cooldown held in memory.** `provider_unhealthy` cools every route with the same id prefix (the text before the first `/`) for 300 s; an unknown code cools the denied route for 60 s. Both live in a run-scoped map on the ladder that is consulted where persisted cooldowns are, and is never written to the health state file. The prefix, not the pool or the owner, is the reach, because `cc/` and `claude/` share an owner and a pool and must not cool each other.
5. **One record of what was done.** The runtime emits a `controller` event (`phase: "serve_denial"`) with the route, the code, and the action taken (`excluded`, `prefix_cooled`, `route_cooled`).
6. **Ledger class.** The receipt's attempt ledger maps failure categories onto a fixed set of classes (`provider_5xx`, `model_cooldown`, `quota_429` and so on); none of them means "refused by policy". `route_denied` is deliberately left out of that map, so its ledger class is `other`: the refusal says nothing about the model's quality or the provider's health and must not be counted as `provider_5xx` against the route. A dedicated class would be a change to that fixed set and is not part of this change.

## Risks / Trade-offs

- An exclusion is for the whole run. If serve's permanent refusal was itself a mistake, the route is lost until the next run.
- Prefix cooling can idle healthy routes of a provider whose other connection is fine. It is bounded at 300 s and by the run.
- The category depends on serve's wording. The matcher is the one part 1 already relies on, anchored on the exact route and the fixed `denied: <code>` form; if the wording changes, the failure falls back to `upstream_temporary`, which is today's behaviour.
- With every route of a node excluded or cooling, the node ends BLOCKED through the existing bounded-attempt and empty-pool paths; nothing waits without limit.

## Security / trust implications

The denial text comes from `verdict serve` on the local host through the worker's output, which is untrusted in general. It can only make the run more restrictive: it can exclude or cool the attempt's own route (and, for one named code, its prefix), never admit, prefer or authorise anything. The matcher accepts a code only in serve's fixed form for that exact route.

## Routing / context / memory implications

Routing: the admitted set narrows within the run; ranking is untouched. Free and cheap routes are still tried first, and a refused one now costs one attempt once instead of one attempt per cooldown period. Nothing is written to the health cache, the persisted cooldowns or memory.

## Migration / rollback

No stored state or schema changes. Reverting restores `upstream_temporary` for denials.

## ADR impact

None. ADR-053 (the lane runs every per-route gate) is unchanged; this is the orchestrator reacting to its result.
