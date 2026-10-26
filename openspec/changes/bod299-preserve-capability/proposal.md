# Proposal: BOD-299 preserve capability across availability failure

## Why

A negative HealthCache.record clears a prior agentic pass even when the route did not answer. Liveness must remain negative, but that observation does not disprove the earlier tool capability.

## What Changes

- Preserve existing probe capability and its original timestamp only for no-answer timeout, upstream, rate-limit, authentication, payment and permission failures.
- Keep current negative health, backoff, failure timestamp and dispatch authority unchanged.
- Retain revocation for answered tool/agentic failures and fail closed for unknown, malformed and model-mismatch results.

## Capabilities

### Modified Capabilities

- `shared-qualification`: Independent capability retention and liveness failure.

## Impact

Only HealthCache.record, focused existing test files and this change. No live source/configuration mutation, new store, schema, TTL, cap, permit policy or gateway calls. Historical batch-writer attribution is not proven and is not asserted.

## Rollout and acceptance remaining

Parent independent exact-head review and rerun are required before push. Local implementation is not whole-story completion. Merge and installed-main setup/doctor, real prepared task, retained operational proof and subsequent acceptance per BOD-439 remain OPEN and are not ticked as implementation tasks.
