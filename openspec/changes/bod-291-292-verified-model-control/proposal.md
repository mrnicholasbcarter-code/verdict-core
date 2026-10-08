# Proposal: Verified model projection and bounded health refresh

## Why

BOD-291 needs an unscoped list of evidenced-working identities, not a catalog presented as health. BOD-292 needs one bounded refresh path that keeps health current when a consumer needs it, without probing thousands of routes or spending metered quota without consent.

## What Changes

- Add the pure, shared `verdict.verified-models/v1` projection. Separate VERIFIED, STALE, FAILED, UNAVAILABLE, UNVERIFIED, INVENTORY_ONLY, and EXCLUDED. Inventory and local admission/ladder hints never prove verification or launch authority.
- Make TUI `/eligibility` open this view without a default scope. Add status/provider/search/page arguments and CLI `verdict eligibility --verified`; preserve existing `verdict eligibility --json` keys and behavior.
- Add a bounded refresh coordinator. **Operator decision, 2026-10-08:** opening the TUI/CLI verified view, Prime picker preview/apply for the involved ids, or selection before dispatch triggers automatic refresh of needed non-fresh FREE/SUBSCRIPTION routes. **Superseding amendment:** the consumer waits with live progress and renders/uses statuses only after completion; it does not show a provisional list. Fresh needed entries bypass refresh and waiting. Autocomplete never probes or waits.
- Bound each trigger by route, request, wall-time, and concurrency limits. Single-flight consumers join the active job. Use provider round-robin priority, token buckets, scoped cooldowns, cancel, and one cache writer. `VERDICT_AUTO_REFRESH=0` disables automatic refresh.
- Keep a manual plan/execute action for metered/unknown routes or wider coverage. Show count, cap, request estimate, quota/spend warning, and digest-bound y/N consent (default No). Fix `/probe` model-list parsing and in-TUI consent.
- Reuse the two-step prove-at-rest Prober, canonical failure scopes, and health-cache TTL constants. Cache health remains display/scheduling evidence, never launch authority; runtime admission still confirms the exact route before launch.

## Capabilities

### New Capabilities

- `verified-model-projection`: trustworthy status precedence, stable JSON, unscoped shared view, filters, and bounded paging.
- `bounded-model-refresh`: automatic prepaid refresh with bounded consumer waiting, single-flight/cancel, explicit manual consent, failure persistence, and `/probe` consent/list handling.

### Modified Capabilities

None. The repository's existing review/integration specs are unaffected. Legacy eligibility JSON stays backward compatible; the new verified mode is opt-in at the CLI.

## Impact

- New projection and rendering modules under `verdict/orchestration/`; new snapshot/action adapter and refresh coordinator; action registration, orchestration CLI, and TUI wiring.
- Additive health-cache evidence/cooldown metadata and bounded full-probe API; read-only legacy adapters; negative-only runtime cache adapter. No new external dependency, provider, inventory expansion, or live test calls.
- BOD-293 receives a reusable wait/refresh API; this change does not implement its picker. BOD-295 autocomplete reads the local snapshot only.
- Implementation is split into three disjoint file owners in `design.md` and `tasks.md`. This change contains design artifacts only.
