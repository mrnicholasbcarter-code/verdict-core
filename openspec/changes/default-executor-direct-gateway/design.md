# Design: direct-gateway is the default worker executor

## Context

The harness registry already holds two worker adapters: `prime-headless`
(spawns the Prime Agent binary, visibility from Prime's `models.json`) and
`direct-gateway` (HTTP to the gateway, visibility from the live catalog).
Only the default flag decided which one a user got.

## Goals / Non-Goals

- Goal: a gateway-only host runs, sets up, passes doctor and live acceptance.
- Goal: Prime users keep identical behaviour by naming Prime.
- Non-goal: an OpenCode executor (BOD-285, after 1.0).

## Decisions

- Move `default=True` to `DirectGatewayAdapter`; `--executor` defaults to `DEFAULT_REGISTRY.default_name()` so one place decides.
- `build_selector(visibility_executor=None)` resolves to the registry default. Callers that run Prime pass it explicitly; `--sync-visibility` (which writes Prime's registry) passes `prime-headless`.
- With an executor map, the ladder is shared by every node, so it uses Prime visibility when any backend is Prime, else the `--executor` visibility. This keeps the old strict rule for mixed Prime runs.
- Prime presence is `harness_prime.discover().installed` (binary on PATH), the existing probe. Acceptance also needs the Verdict harness enabled, since an installed but unconfigured Prime is not something the operator asked Verdict to check.
- Doctor uses the existing `not-installed` state (already used by shared memory), outside `BLOCKING_ROW_STATES`.

## Risks / Trade-offs

- Users relying on the implicit Prime default switch harness silently; the proposal and docs name the flag to restore it.
- direct-gateway workers answer without an agent tool loop; quality differences are a harness choice, not an admission change.

## Security / trust implications

None new: admission, review and receipts are unchanged. Fewer reads of `~/.prime` on the default path.

## Routing / context / memory implications

Visibility on the default path is the live catalog, filtered to concrete routes, so new routes are visible without a sync. Admission still decides membership.

## Migration / rollback

Pass `--executor prime-headless` to keep Prime. Rollback is reverting this change.

## ADR impact

None; ADR 0001 invariants hold.
