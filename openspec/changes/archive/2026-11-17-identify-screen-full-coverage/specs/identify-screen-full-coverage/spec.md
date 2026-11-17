# identify-screen-full-coverage Specification

## Purpose

Scheduled screening runs every six hours with larger bounded caps for stale free and subscription routes, improving coverage over the prior daily sample.

## Requirements

### Requirement: Scheduled screening runs every six hours with larger bounded caps

Each scheduled screen run MUST run every six hours and probe at most 40 stale free and subscription routes (a shared cap) and at most 16 routes per provider, in task-rating order. It MUST use the existing explicit live-probe consent and budget rules and remain within the screen's built-in 600-second wall budget. `TimeoutStartSec=30min` is only a systemd backstop above that built-in budget.

Routes not reached because a cap or the wall budget was reached MUST be reported as `skipped_cap` or `skipped_deadline`, with `stopped_reason` `route_cap`, `provider_cap`, or `wall_cap` as applicable, and left for a later run. The command still exits successfully when it reports these bounded skips.

#### Scenario: Six-hourly run screens the highest-rated stale routes within bounds

- **WHEN** a scheduled screen runs with explicit live-probe consent
- **THEN** it considers stale free and subscription routes in task-rating order
- **AND** probes no more than 40 routes across both capacities and no more than 16 routes for any provider
- **AND** stops at the built-in 600-second wall budget if that is reached first
- **AND** reports routes not reached as `skipped_cap` or `skipped_deadline` for the next run
