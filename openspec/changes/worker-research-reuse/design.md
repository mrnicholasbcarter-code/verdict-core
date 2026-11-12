# Design

## Context

Worker retries currently rebuild advisory context. Existing context pack identity remains content-derived.

## Goals / Non-Goals

- Goals: stable research identity, explicit deltas, budgeted projection.
- Non-Goals: runtime integration, admission, routing or a new memory store.

## Decisions

Research identity binds task, observed repository revision and source revisions. Deltas never alter it. Projection recompiles stored units through the existing compiler.

## Risks / Trade-offs

A stale source could be reused if its revision is omitted. Reuse therefore requires explicit revisions and rejects mismatches.

## Security / trust implications

Deltas are redacted, sanitized and quoted. Settled evidence is an observed snapshot, not authenticated truth or route authority.

## Routing / context / memory implications

Context remains advisory. No route, admission or memory authority is added.

## Migration / rollback

Fields are additive and default empty. Rollback removes the new APIs without changing existing context pack digests.

## ADR impact

ADR-022 and ADR-036 remain unchanged: retrieval stays advisory and recovery/review boundaries remain independent.
