# Design

## Context

`prepare_live_autodev` (verdict/autodev_run.py) probes ONE explicitly pinned
`provider/model:free` route, offers it as the only candidate, and returns the
optimizer decision, the plan and the probe headers. `cmd_autodev`
(verdict/cli.py) calls it only when `executor_model` is truthy, so an unpinned
live run reaches `_require_launch_decision(None, ...)` and fails. The
eligibility ladder already ranks live routes
(verdict/orchestration/eligibility.py: `evaluate`/`select`, rank assigned from
the sorted candidate list, lower is better) and
verdict/orchestration/eligibility_report.py `build_selector` is the admitted
way to build it (verdict/actions/registry.py `_action_eligibility` is the
working call pattern).

## Goals / Non-Goals

**Goals:**
- Unpinned `verdict autodev --allow-live` picks a FREE executor from live
  eligibility evidence and probes it before any launch authority exists.
- The optimizer, not the CLI, chooses between the probed routes, so the
  decision carries `why_selected` and the losing candidates in `rejected`.
- The selection is visible in the CLI report and in the persisted receipt.
- Fail closed: no paid or frontier fallback, ever.

**Non-Goals:**
- No retry loop over the ladder (the next slice consumes the returned order).
- No change to the pinned `--executor-model` path or its probe contract.
- No new eligibility, admission or certification gate; the existing ladder and
  optimizer stay the only authorities.

## Decisions

**Decision 1: select from the ladder, then probe, then offer all survivors.**
The ladder's `select` already probes for health, but autodev's own launch
evidence is stricter: exact served-model identity, the exact "OK" content and a
zero response cost. So the ladder supplies the ranked FREE candidates and
autodev re-probes each one with the pinned path's checks. Every survivor is
offered to `optimize_execution_path` as its own `direct_cheap` candidate;
distinct `route_id`s give distinct `expected_cost.strategy_id`s
(verdict/subagent_resolver.py), so the optimizer's duplicate check does not fire
and the loser lands in `rejected` with its cost reason.

Alternative considered: pick the ladder's single winner and offer only that.
Rejected because then `rejected` stays empty and the receipt cannot show what
the selection beat, which is the point of the row.

**Decision 2: `prefer=""`.** `build_selector` turns `prefer` into
`prefer_providers`, and a route's `provider_pref` rank component is its index in
that tuple (else the tuple length). An empty value gives every route the same
`provider_pref`, so capacity class, capability slack, price and measured quota
decide the order and no provider family wins by name.

**Decision 3: free means the ladder's own evidence.** A route is kept when it
reached `TASK_ELIGIBLE` or `SELECTED` (the predicate the ladder itself uses for
`eligible_after_probe`, and `routing_render` for "admitted") AND it is free by
account evidence (`capacity_class is CapacityClass.FREE`) or by the gateway's
explicit `:free` route id. Model names are never the evidence.

**Decision 4: plan after probing, decide after planning.** The request needs the
plan's unit ids, acceptance and proof criteria, so the order is: probe the
ladder, decompose, then build the request and call the optimizer. The one-hour
expiry guard now spans probing plus planning, which is stricter than the pinned
path's planning-only window.

## Risks / Trade-offs

- Probing up to three routes costs up to three bounded gateway calls per run.
  They are the same 120 s, 1024-token, zero-cost probes the pinned path makes,
  and a route that fails is dropped with its reason instead of being retried.
- A ladder that ranks a route first but whose probe fails now falls to the next
  free route rather than failing the run. That is the intended behaviour and it
  is recorded: the drop reason is reported when nothing survives.
- Ties between two zero-cash free routes are broken by
  `expected_cost.strategy_id` (verdict/expected_cost.py `compare_strategies`),
  so the winner is deterministic but not operator-chosen. The receipt shows the
  reason, and a pin still overrides everything.

## Migration Plan

Additive. The pinned path is untouched, so any caller passing
`--executor-model` behaves exactly as before. Rollback is reverting this branch:
the two new receipt keys disappear with it, and receipts already written keep
their extra keys (the store only reads what a payload holds).

## ADR impact

None. ADR-035 keeps `optimize_execution_path` as the only strategy/route
authority for automatic launch; this change gives that authority more candidates
and never bypasses it. No new ADR is needed because no boundary moves.

## Security / trust implications

The gateway key is resolved through `verdict.credentials_store.resolve_api_key`
(environment, credential store, owner-only key file, Prime auth) instead of one
environment variable, and it is never printed or persisted. Probe headers are
recorded as before. The two new receipt keys hold only route ids, strategy
names and optimizer reasons; their key segments match neither the sensitive nor
the raw redaction patterns in verdict/receipt_store.py, and no token counts are
nested inside them, so no allowlist change was needed or made.

## Routing / context / memory implications

Selection reads the ladder as-is: no Prime visibility write
(`sync_visibility=False`), no health-cache widening, and the bounded selection
refresh hook is passed only when the environment enables it. Nothing is cached
across runs by this change, and no context or memory store is touched.
