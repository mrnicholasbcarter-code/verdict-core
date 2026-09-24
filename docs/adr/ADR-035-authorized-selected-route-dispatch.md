# ADR-035: Authorized selected-route dispatch (post-swarm)

- **Status:** Accepted — successor to ADR-023
- **Lifecycle:** PARTIALLY_TRUE (BOD-169 evidence bar: source/test present; withholding CURRENT until ecosystem re-audit)
- **Date:** 2026-09-24
- **Deciders:** Verdict Core maintainers
- **Supersedes:** [ADR-023](ADR-023-governed-swarm-supervision.md)
- **Related:** BOD-17, BOD-67, BOD-104, BOD-127, [ADR-0001](0001-verdict-control-plane-invariants.md), [ADR-032](ADR-032-core-model-metadata-store.md)

## Context

ADR-023 described Ruflo/governed-swarm supervision. BOD-17 deleted that
architecture from Core. ADR-023's banner correctly declares SUPERSEDED, but
the [ecosystem V2 ADR lifecycle index (BOD-169)](https://github.com/mrnicholasbcarter-code/verdict-ecosystem/blob/main/docs/ADR_LIFECYCLE.md)
classified it `MISSING_SUCCESSOR` because no numbered successor ADR named the
replacement decision.

## Decision

Canonical Core dispatch after swarm deletion is:

1. **Strategy authority** — BOD-104 execution-path / `verdict/serve_path.py`
   owns strategy selection on the API serve path.
2. **Binding** — BOD-67 / `verdict/dispatcher.py` binds an authorized
   `selected_route` only (hydrate → authorize → assign). The historical
   `SwarmDispatcher` name is authorize-only dispatch, not Ruflo swarm
   supervision.
3. **Legacy selectors** — BOD-127 demotes chooser / live-routing /
   failover invent paths; they may feed candidates but are not routing
   authority when execution-path authority is required.

Hard eligibility still precedes advisory ranking ([ADR-0001](0001-verdict-control-plane-invariants.md)).
OmniRoute remains inventory/execute/health only ([ADR-032](ADR-032-core-model-metadata-store.md)).

## Consequences

- ADR-023 is SUPERSEDED by this record (closes BOD-169 `MISSING_SUCCESSOR`).
- Interviewers and the ecosystem re-audit should cite ADR-035 + BOD-104/67/127,
  not ADR-023, for current dispatch architecture.
- Demoted selector modules may remain as feeds/escapes until a later cleanup
  story removes them; they are not a second routing policy.
