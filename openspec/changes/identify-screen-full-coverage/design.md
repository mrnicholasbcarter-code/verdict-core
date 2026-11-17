# Design

## Context

Agentic proof expires after 24 hours, and a small daily sample leaves stale routes behind. On 2026-11-17, 36 free routes needed an agentic probe. The installed 00:00 UTC run with caps 1/8 and capacity free,subscription checked 8 routes and left 582 `skipped_cap` (129 free, 453 subscription). Forty routes per run does not clear that backlog in one run.

## Goals / Non-Goals

Goals: run every six hours and increase bounded stale-route coverage. Each run probes at most 40 stale free and subscription routes using one shared counter, at most 16 per provider, in task-rating order, within the built-in 600-second wall budget. Non-goals: claim all stale routes finish in one run, change CLI validation, consent, route selection, proof freshness, or doctor behavior.

## Decisions

Use max-per-provider 16, max-routes 40, and `*-*-* 00/6:00:00`. The screen's existing 600-second wall budget remains authoritative. Routes beyond route/provider caps are reported `skipped_cap` with the applicable cap stop reason; routes beyond the wall budget are `skipped_deadline` with `stopped_reason=wall_cap` and remain for the next run. `TimeoutStartSec=30min` is only a systemd backstop above the built-in budget.

## Risks / Trade-offs

A larger run consumes more probe budget and still cannot clear the observed backlog in one run; the six-hour cadence gives later runs more opportunities. Existing consent, aggregate budget, provider cap, route cap, and wall budget remain controls.

## Security / trust implications

No new credentials or network paths are introduced. Existing explicit live-probe consent and budget enforcement remain authoritative.

## Routing / context / memory implications

No route ranking or context behavior changes. The existing strongest-task-rating ordering and identify screen implementation remain in use.

## Migration / rollback

Reinstalling the identify service renders the new units. Reverting this change restores the prior generated caps, daily calendar, and timeout; systemd unit installation remains the operator's post-merge task.

## ADR impact

No ADR is required; this is a bounded operational schedule adjustment under the existing identify maintenance design.
