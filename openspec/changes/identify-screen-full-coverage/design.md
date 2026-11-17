# Design

## Context

The installed screen uses fixed caps that can leave stale proof unchecked. A six-hour schedule and full-inventory bounds address expiry while retaining bounded execution.

## Goals / Non-Goals

Goals: cover every currently stale free and subscription route within a wall-clock bound. Non-goals: change CLI validation, consent, route selection, proof freshness, or doctor behavior.

## Decisions

Use max-per-provider 16, max-routes 40, `*-*-* 00/6:00:00`, and a 30-minute service timeout. These values cover the observed inventory and provide headroom over the observed 10-minute run.

## Risks / Trade-offs

A larger run consumes more probe budget and may take longer; the existing consent, aggregate budget, and systemd timeout remain the controls. The six-hour cadence increases probe frequency but prevents 24-hour proof expiry gaps.

## Security / trust implications

No new credentials or network paths are introduced. Existing explicit live-probe consent and budget enforcement remain authoritative.

## Routing / context / memory implications

No route ranking or context behavior changes. The scheduled command continues using the existing identify screen implementation.

## Migration / rollback

Reinstalling the identify service renders the new units. Reverting this change restores the prior generated caps, daily calendar, and timeout; systemd unit installation remains the operator's post-merge task.

## ADR impact

No ADR is required; this is a bounded operational schedule adjustment under the existing identify maintenance design.
