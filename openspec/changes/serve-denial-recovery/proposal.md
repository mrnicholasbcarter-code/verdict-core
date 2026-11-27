# The orchestrator recovers from a serve route denial by its reason

## Why

`verdict serve` refuses a worker's assigned route with HTTP 503 and the text `assigned route '<id>' denied: <code>`. Since #1374 the OpenCode executor ends the attempt on that text instead of waiting for OpenCode to give up. What happens next is still wrong: `FailureIntelligence.classify` sees a 5xx and returns `upstream_temporary` with a 60-second route cooldown, so

- the cooldown is written to the persisted health state and the failure is offered to the health cache, as if the upstream model had failed;
- after 60 seconds another node can select the same route and be refused again.

In run `20261126T120409Z` three refused attempts cost 945 s of a 2,700 s deadline, two of them on the same route (GitHub issue #1366). Self-build runs have since used `--scope cc/,cx/` to avoid routes serve refuses; this change is what lets that scope go.

## What Changes

**`verdict/orchestration/recovery.py`**

- A new failure category `route_denied` for a terminal whose error carries serve's denial text for the attempt's own route. Action `REROUTE`, scope `none`, cooldown 0, with the denial code in the evidence. A 5xx without that text stays `upstream_temporary`.

**`verdict/orchestration/eligibility.py`**

- The ladder gains a run-scoped exclusion (it narrows the admitted set, with the denial code as the reason) and a run-scoped cooldown that is held in memory and never written to the health state file.

**`verdict/orchestration/runtime.py`**

- On `route_denied` the runtime applies the code: a permanent code (`opaque_route_disallowed`, `metadata_ghost`) excludes the route for every node for the rest of the run; `provider_unhealthy` cools the routes that share the route's id prefix for the run; any other code cools only that route, briefly. It records what it did in a `controller` event.

## Capabilities

### New Capabilities

- `orchestration-serve-denial-recovery`: how a run reacts when `verdict serve` refuses the route a worker was assigned.

## Impact

`verdict/orchestration/recovery.py`, `eligibility.py`, `runtime.py`, tests. No CLI or schema change; no new event type. Receipts of runs with a denial show the new category (ledger class `other`, no longer `provider_5xx`) and, for later nodes, the route's exclusion reason in the selection record.

Invariants this touches (AGENTS.md): the admitted set only narrows and is never re-admitted within the run; nothing here authorises a route; a route's cost class is not involved.

## Evidence boundary

- **KNOWN:**
  - `verdict/orchestration/opencode_denial.py` recognises serve's denial text for a given route and returns its code; the OpenCode executor returns a failed terminal with `status_code=503` and that text as `error` (#1374).
  - `FailureIntelligence.classify` maps any 5xx to `upstream_temporary`, `REROUTE`, scope `route`, 60 s; `DagRuntime._record_failure` then calls `selector.record_failure`, which persists the cooldown, and offers the failure to session feedback.
  - `AdmittedSet.exclude` narrows and records a stage and reason; the ladder reports `first_failure` for a route that is not admitted, which is what a selection event shows.
  - Run `20261126T120409Z`: 945 s in three refused attempts, one route refused twice (recorded on #1366).
- **INFERRED:** cooling the routes that share an id prefix is the right reach for `provider_unhealthy`. Serve's health is kept per provider connection; the prefix is the nearest thing the ladder can name without asking serve.
- **NOT AVAILABLE:** a live unscoped run after this change (task 4.1); the set of denial codes serve can emit beyond the three named here; how often `provider_unhealthy` clears within one run.

## Acceptance criteria

- A denial with a permanent code removes that route for every node for the rest of the run; a later node's selection record shows the route excluded with the code; nothing is written to the health cache or the persisted cooldowns for it.
- `provider_unhealthy` puts the routes with the same id prefix in a run-scoped cooldown and does not remove them; an unknown code cools only the denied route and never narrows the set.
- A refusal of `claude/X` never cools or removes `cc/X`.
- A 5xx from the upstream with no denial text is still `upstream_temporary` with today's behaviour.
- (post-merge) On installed main, an unscoped `verdict orchestrate` run in which a refused route is selected loses under 60 s to each refusal and never selects a permanently refused route twice.
