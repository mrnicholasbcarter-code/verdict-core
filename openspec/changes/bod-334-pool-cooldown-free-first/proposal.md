# BOD-334: Pool cooldowns and free-first planning

## Why

The BOD-331 self-hosting run selected Claude aliases after a Claude cooldown.
Workers also created stray summaries, and planning repairs repeated ownership conflicts.

## What Changes

- Reuse credential-pool identity for cooldowns and planner/worker retry exclusions.
- Resolve no-think wrappers through their inner route's credential pool.
- Rank sufficient free routes before subscription routes for all ladder roles.
  **Superseded in part by BOD-342 (2026-10-21):** price-free selection
  (strongest proven-capable route first) replaces this default free-first
  ordering; see the superseded note in `specs/orchestration/spec.md`.
- Apply a tier-2 planner floor and record it in planning selection events.
- Forbid worker summary/notes files and expose stray paths in ownership barriers.
- Tell planners to serialize or merge nodes named in ownership conflicts.

## Scope

Reviewer route/family independence and admission gates remain unchanged.
No live provider calls or census changes are part of this story.
