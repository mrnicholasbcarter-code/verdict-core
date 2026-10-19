# ADR-035: Authorized selected-route dispatch (post-swarm)

> Removed integration notice: Ruflo and RuVector are removed; references below are historical, not current setup requirements.

- **Status:** Accepted — successor to ADR-023
- **Lifecycle:** PARTIALLY_TRUE (cross-repo ADR lifecycle audit evidence bar: source/test present; withholding CURRENT until ecosystem re-audit)
- **Date:** 2026-09-24
- **Deciders:** Verdict Core maintainers
- **Supersedes:** [ADR-023](ADR-023-governed-swarm-supervision.md)
- **Related:** Ruflo/swarm removal from Core, authorized dispatcher binding (`verdict/dispatcher.py`), execution-path strategy authority (`verdict/serve_path.py`), legacy selector demotion, [ADR-0001](0001-verdict-control-plane-invariants.md), [ADR-032](ADR-032-core-model-metadata-store.md)

## Context

ADR-023 described Ruflo/governed-swarm supervision. The Ruflo/swarm removal work deleted that
architecture from Core. ADR-023's banner correctly declares SUPERSEDED, but
the [ecosystem V2 ADR lifecycle index](https://github.com/mrnicholasbcarter-code/verdict-ecosystem/blob/main/docs/ADR_LIFECYCLE.md)
classified it `MISSING_SUCCESSOR` because no numbered successor ADR named the
replacement decision.

## Decision

Canonical Core dispatch after swarm deletion is:

1. **Strategy authority** — execution-path strategy authority / `verdict/serve_path.py`
   owns strategy selection on the API serve path.
2. **Binding** — authorized dispatcher binding / `verdict/dispatcher.py` binds an authorized
   `selected_route` only (hydrate → authorize → assign). The historical
   `SwarmDispatcher` name is authorize-only dispatch, not Ruflo swarm
   supervision.
3. **Legacy selectors** — chooser / live-routing / failover invent paths are
   demoted; they may feed candidates but are not routing
   authority when execution-path authority is required.

Hard eligibility still precedes advisory ranking ([ADR-0001](0001-verdict-control-plane-invariants.md)).
OmniRoute remains inventory/execute/health only ([ADR-032](ADR-032-core-model-metadata-store.md)).

4. **Price-invariant selection** — `optimize_execution_path` orders surviving offers by
   trusted `pool_receipt` shortlist position then stable candidate ID, rather than
   expected price. Candidate pool ranking removes `cost_fit` and passport price injection,
   retaining non-price feature coefficients (task outcomes .40, eval .25, health .15,
   latency .05, context fit .10, uncertainty penalty -.30). Missing price remains unknown
   and is not an extra gate without an explicit hard spend budget.
5. **Server task authority** — Server-owned task requirements (context, reasoning, tools,
   schema, stream) govern public entrypoints. Caller-supplied raw decisions, weakening
   flags (`allow_legacy_selector`, `require_execution_path_authority`), and false
   protected/client certificate fields cannot lower server policy. Offline catalog mode
   cannot authorize execution.

## Consequences

- ADR-023 is SUPERSEDED by this record (closes the cross-repo ADR lifecycle audit `MISSING_SUCCESSOR` finding).
- Reviewers and the ecosystem re-audit should cite ADR-035 (serve-path strategy authority,
  dispatcher binding, legacy selector demotion), not ADR-023, for current dispatch architecture.
- Demoted selector modules may remain as feeds/escapes until a later cleanup
  story removes them; they are not a second routing policy.


## Addendum: BOD-433 API relay dispatch permits (2026-10-19)

Trusted execution-path preparation retains the fresh passport for each confirmed
admitted route. Selection is not dispatch authority. Immediately before each relay
send, Core commits a durable, unique-attempt permit against that proof's captured
revocation epoch and original expiry. Missing or superseded proof makes zero sends.

The shared relay scope binds the resolved gateway origin, concrete provider/model
and route id (canonical SHA-256 `config_generation`), prepared task profile digest,
and `binding_id = "api_relay:" + route_id`. This reduced binding does not represent
the full effective provider configuration or include key material.

Complete explicit rejection responses already classified as retryable (for example
429 or context overflow) may try another admitted route, with its own fresh attempt
id and permit. A pre-byte 5xx fallback additionally requires the existing idempotency
key and no-tools safety rule. Timeouts, transport failures, identity mismatch,
output already forwarded, or a revocation after commit end the request without
retry. Auth/forbidden,
model-not-found, and identity-mismatch denials revoke the affected scope. The durable
attempt remains MAYBE_SENT. Existing transport cancellation and stream cleanup
remain responsible for stopping and closing upstream work where possible.

CLI selection does not mint permits. Worker handoffs now carry admitted node
proofs and mint permits at the worker dispatch seam. Only DagRuntime adoption
of that handoff remains deferred to phase 2b.

## Addendum: Dispatch scope binding (reduced)

`DispatchScope` binds the gateway origin, the selected route (provider, model and
route id, hashed into `config_generation`) and the task profile digest. It never
contains key material. It does not represent the full effective configuration;
a full effective-config generation is a follow-up.
