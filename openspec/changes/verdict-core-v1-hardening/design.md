# Design

## Context

This is an umbrella contract for the reviewed V1 plan. Existing OpenSpec changes remain authoritative for their component details; this change records only cross-component behavior that must hold together. The plan and its A01–A30 matrix are the implementation source of truth.

## Goals / Non-Goals

**Goals:** Coordinate the 13 existing stories; retain their dependency order and explicit acceptance coverage; keep unknown evidence visible and authority boundaries intact.

**Non-Goals:** Replace existing component specs, introduce a second router/coordinator/ledger, claim any acceptance row already passes, or authorize deployment, service cutover, or DoD closure.

## Decisions

1. Keep one umbrella capability, `v1-model-control`, for observable cross-cutting guarantees. Existing active and canonical specs continue to own detailed component behavior; this delta adds only requirements not represented there as a coherent end-to-end contract.
2. Preserve one shared qualification coordinator and current evidence/receipt stores. All producers share scoped reservations and bounded budgets; unknown topology uses an exclusive Core lane. Unknown values remain explicit and never become positive evidence.
3. Apply admission and hard task/access constraints before ranking. Rank eligible choices by task performance and fit, with no price preference. Price is a hard constraint only when an explicit spend limit requires it.
4. Bind final sends and handoffs to scoped, unexpired permits. Preserve protocol terminal states and reported identity, close stream transports once, and join attempts to independent checked outcomes.
5. Feed selection only with verified task-matched outcomes in the checked 30-day window, weighted with a seven-day half-life. Demote only below 0.4 with at least three unique checked attempts; zero observations retain the 0.5 prior and unknown status.
6. Install one six-hour metadata refresh timer with visible source provenance through existing setup and doctor paths. Preserve the supported opt-in mirror and partial-source last-good facts under original deadlines.
7. Use the 13 story groups and their A01–A30 mappings in `tasks.md` as the delivery map. Implementation checkboxes remain open until the owning story performs its required work and validation.

## Risks / Trade-offs

- [Umbrella scope can drift into duplicate component design] → keep component requirements in their current changes and update this delta only for cross-component behavior.
- [Some upstream identity, retry, or pool-topology facts may be unavailable] → preserve explicit unknown/unsupported outcomes and bound claims to Core-coordinated behavior.
- [Integration proof depends on live operator state] → defer live acceptance and parent evidence to the dedicated final stories; fixtures and strict spec validation do not count as live proof.

## Migration Plan

Deliver stories in the waves in `tasks.md`, requiring verified merged-main prerequisites between dependent waves. Do not archive this change, mark implementation tasks complete, publish DoD evidence, or switch installed services until their own proof gates pass.
