# Design

## Context

Discovery raised before a terminal run event, causing unchanged auth retries.

## Goals / Non-Goals

Correct the causal failure path and retain bounded recovery. Do not replace the
controller, add credentials, change ranking membership, or claim live completion.

## Decisions

Use typed GatewayDiscoveryError; only 401/403 settle startup BLOCKED, while transient errors still raise for supervisor recovery.

## Risks / Trade-offs

Early blocks need accurate causes. Shared negative records can affect other runs,
so scope, expiry, real-source guards and locked writes remain mandatory.

## Security / trust implications

Raw credentials, transcripts and transport exception bodies do not enter new
evidence. Unknown and injected evidence cannot authorize a route.

## Routing / context / memory implications

Only failure evidence narrows availability. No positive health, capability,
context memory or advisory ranking authority is added.

## Migration / rollback

Changes are additive. Roll back this story commit to restore prior behavior.
Existing schema-1 cache readers ignore additive metadata; no migration is needed.

## ADR impact

ADR-036 orchestration and ADR 0001 fail-closed invariants remain intact. This
implements their failure-evidence boundary without an architectural replacement.
