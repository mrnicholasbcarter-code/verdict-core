# Design

## Context

Verdict routes prompts to the cheapest proven-capable model. Routes keep their
proof fresh through two separate mechanisms: `verdict acceptance run` runs the
hourly tool check, and `verdict identify --screen` runs the six-hourly agentic
probe. Both must agree that a route is capable before it stays in the eligible
set.

The tool check can flag a route as failing, but until this change it had no way
to tell the agentic screen "re-probe this one". And even if a hint were already
in the health cache, `screen_models` would still skip the route with
`skipped_fresh` because its agentic proof was younger than 24 hours.

Separately, `prove-at-rest once --reprobe` builds the chat-phase budget from
`max_requests` before computing how much to hold back for the named reprobe.
When the coordinator fills the entire budget, the named reprobe finds zero
remaining requests and silently exits, so the operator's explicit request never
runs.

## Goals / Non-Goals

**Goals:**
- Close the feedback loop between the hourly tool check and the six-hourly
  agentic screen: a route that fails both tool-check attempts gets a rescreen
  hint so the screen probes it even with fresh proof.
- Guarantee that a named forced reprobe (`--reprobe`) always gets its
  `AGENTIC_TURNS` turns, regardless of how many requests the chat phase used.
- Keep the fix minimal: no new files, no new services, no new timer, one new
  key in the `measure()` return dict, one new guard in `decide()`, one new
  field in `Prober`.

**Non-Goals:**
- Change the hourly or six-hourly schedule.
- Change what constitutes a "fresh" agentic proof.
- Add a new API endpoint or CLI flag.
- Let acceptance write health or proof entries — it is a measurement; it must
  not write capability state.

## Decisions

1. **Rescreen hints are scheduling hints, never capability writes.** The same
   invariant that governs `request_rescreen` / `complete_rescreen` in the
   existing health cache API applies: a hint neither admits a route, makes it
   healthy, nor writes proof. `eligibility_check` wraps the call in `try/except`
   so a cache failure cannot fail the acceptance run.

2. **Budget split in `run_once`, not in `Prober.__init__`.**
   `_reprobe_reserved` is computed inside `run_once()` after `routes_loader()`
   produces the live snapshot, so the reservation reflects which of the named
   routes are actually admitted and FREE on this run. Putting it in `__init__`
   would use a potentially stale value.

3. **Early return when `chat_max <= 0`.** `RefreshConfig` requires
   `max_requests >= 1`; passing zero raises an error. When the entire budget is
   reserved for the reprobe, `_run_chat_phase` returns early with
   `stopped_reason = "request_cap"` before instantiating the coordinator.

4. **`_request_cap` is not modified.** The existing `_request_cap` field is read
   only in `_buffer_auth_failure` (agentic phase, auth outage gate). The
   reprobe reservation is a separate concern and must not pollute the auth gate
   logic.

5. **`failed_all_listed` is sorted for determinism.** Routes are sorted before
   being stored in the return dict and before being passed to `request_rescreen`,
   so the audit log and the cache are both deterministic.

## Risks / Trade-offs

- **[Rescreen hint not consumed]** If the six-hourly screen job is not running
  (e.g., timer not installed), the hint stays in the cache indefinitely.
  Acceptable: the hint is a scheduling hint, not a health write; a stale hint
  does not affect routing.
- **[Over-reservation of chat budget]** When many routes are named with
  `--reprobe`, the chat phase may get a very small budget. Acceptable: the
  operator explicitly named those routes, so the bias toward reprobe is
  intentional.
- **[No test for the installed-main live path]** The end-to-end trace (tool
  check → rescreen hint → screen consumes hint) can only be verified on merged
  main with live services. Covered by the post-merge task.

## Security / trust implications

No new network calls, no new external data sources, no new trust boundary.
`request_rescreen` takes only route IDs that were already in the admission
ladder's listed set — IDs that Verdict itself produced, not untrusted input.

## Routing / context / memory implications

`_reprobe_reserved` is an internal field on `Prober`; it is not persisted,
emitted in stats, or used in routing decisions. The rescreen hint is a
scheduling hint stored in the health cache; it does not alter admission or
ranking.

## Migration / rollback

No migration needed. The new `failed_all_listed` key in the `measure()` return
dict is additive. The `_reprobe_reserved` field defaults to zero, so a prober
built without `reprobe_routes` behaves identically to the previous version.
Rollback: revert the four file changes; no data migration required.

## ADR impact

No new ADR needed. This change tightens the operational loop between two
existing scheduled jobs. It does not change any admission invariant, routing
invariant, or architectural decision. The scheduling-hint guarantee ("hints
never write capability state") is already established in the health cache API
and is not changed here.
