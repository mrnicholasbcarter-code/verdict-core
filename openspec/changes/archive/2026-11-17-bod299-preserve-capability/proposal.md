# Proposal: BOD-299 preserve capability across availability failure

## Why

A negative HealthCache.record clears a prior agentic pass even when the route did not answer. Liveness must remain negative, but that observation does not disprove the earlier tool capability.

## What Changes

- Preserve existing probe capability and its original timestamp only for no-answer timeout, upstream, rate-limit, authentication, payment and permission failures.
- Keep current negative health, backoff, failure timestamp and dispatch authority unchanged.
- Retain revocation for answered tool/agentic failures and fail closed for unknown, malformed and model-mismatch results.

## Capabilities

### Modified Capabilities

- `shared-qualification`: Independent capability retention and liveness failure. This extends the same in-flight capability used by `v1-ledger-history-runtimes` and `v1-permits-streams-public-models`; it is not a duplicate new capability. There is no archived main spec yet, so the delta adds requirements rather than modifying an archived requirement. Its existing in-flight Purpose is not replaced.

## Impact

Only HealthCache.record, focused existing test files and this change. No live source/configuration mutation, new store, schema, TTL, cap, permit policy or gateway calls. Historical batch-writer attribution is not proven and is not asserted.

## Evidence boundary

KNOWN: focused regressions and independent exact-head review show the scoped cache behavior on local fixtures. INFERRED: this fixes the reproduced capability-loss path, not an attributed historical batch-writer cause. NOT AVAILABLE: merged-main host acceptance and later operational proof. A deterministic local replay is fixture acceptance only, not host or real-model proof.

## Acceptance criteria

- Earlier exact-route proof survives no-answer availability failures with its original timestamp; negative liveness, last failure and backoff remain authoritative.
- Save/reload and merge preserve evidence without changing a sibling; backoff expiry alone is unprobed, not healthy. Single-call recovery does not renew proof expiry.
- Missing/expired, tied/future and answered/unknown/malformed/model-mismatch evidence remains unqualified; existing session and permit denial stay unchanged.
- Parent rollout acceptance below remains OPEN; local verification cannot satisfy it.

## Rollout and acceptance remaining

Parent independent exact-head review and rerun are required before push. Local implementation is not whole-story completion. Merge and installed-main setup/doctor, real prepared task, retained operational proof and subsequent acceptance per BOD-439 remain OPEN and are not ticked as implementation tasks.
