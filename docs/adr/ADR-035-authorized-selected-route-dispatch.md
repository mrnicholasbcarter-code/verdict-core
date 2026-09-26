# ADR-035: Authorized selected-route dispatch (post-swarm)

- **Status:** Accepted — successor to ADR-023
- **Lifecycle:** PARTIALLY_TRUE (the cross-repo ADR lifecycle evidence audit evidence bar: source/test present; withholding CURRENT until ecosystem re-audit)
- **Date:** 2026-09-24
- **Deciders:** Verdict Core maintainers
- **Supersedes:** [ADR-023](ADR-023-governed-swarm-supervision.md)
- **Related:** the obsolete Ruflo/swarm/hivemind architecture removal (ADR-023, superseded by ADR-035), the hydrate-before-dispatch authorized execution path design, the execution-path authority optimizer (ADR-035), the authorized selected-route dispatch design (ADR-035), [ADR-0001](0001-verdict-control-plane-invariants.md), [ADR-032](ADR-032-core-model-metadata-store.md)

## Context

ADR-023 described Ruflo/governed-swarm supervision. the obsolete Ruflo/swarm/hivemind architecture removal (ADR-023, superseded by ADR-035) deleted that
architecture from Core. ADR-023's banner correctly declares SUPERSEDED, but
the [ecosystem V2 ADR lifecycle index ](https://github.com/mrnicholasbcarter-code/verdict-ecosystem/blob/main/docs/ADR_LIFECYCLE.md)
classified it `MISSING_SUCCESSOR` because no numbered successor ADR named the
replacement decision.

## Decision

Canonical Core dispatch after swarm deletion is:

1. **Strategy authority** — the execution-path authority optimizer (ADR-035) execution-path / `verdict/serve_path.py`
   owns strategy selection on the API serve path.
2. **Binding** — the hydrate-before-dispatch authorized execution path design / `verdict/dispatcher.py` binds an authorized
   `selected_route` only (hydrate → authorize → assign). The historical
   `SwarmDispatcher` name is authorize-only dispatch, not Ruflo swarm
   supervision.
3. **Legacy selectors** — the authorized selected-route dispatch design (ADR-035) demotes chooser / live-routing /
   failover invent paths; they may feed candidates but are not routing
   authority when execution-path authority is required.

Hard eligibility still precedes advisory ranking ([ADR-0001](0001-verdict-control-plane-invariants.md)).
OmniRoute remains inventory/execute/health only ([ADR-032](ADR-032-core-model-metadata-store.md)).

## Consequences

- ADR-023 is SUPERSEDED by this record (closes the cross-repo ADR lifecycle evidence audit `MISSING_SUCCESSOR`).
- Reviewers and the ecosystem re-audit should cite ADR-035 + the execution-path authority optimizer (ADR-035)/67/127,
  not ADR-023, for current dispatch architecture.
- Demoted selector modules may remain as feeds/escapes until a later cleanup
  story removes them; they are not a second routing policy.
