# ADR-037: Supervisor concurrency governor

**Status:** Accepted (operator decisions recorded 2026-09-29; `VERDICT_MULTI_STORY` stays off by default)  
**Date:** 2026-09-28  
**Story:** BOD-157  
**Authors:** verdict-core team

## Context

The supervisor (`scripts/prime_supervisor.py`) today runs one story at a time.
A process-wide `fcntl.flock` on `supervisor.lock` (`acquire_lock` at line ~398,
acquired at line ~2206) is held for the entire run — from stale-lease reaping through controller launch, worker
dispatch, and completion.  No second story can start until the first releases
the lock.

This is safe but wasteful.  Stories that touch disjoint parts of the repo
(e.g. a docs change and an API feature) block each other for no reason.
Provider pool capacity often sits idle while one story waits on review or CI.

Three pure-library modules landed in PRs #672–#674 on `main` to support
multi-story concurrency without any I/O or network coupling:

| Module | Role |
|---|---|
| `verdict/orchestration/ready_gate.py` | Per-story READY / EXCLUDED / WAIT decision based on dependency evidence and labels (AC 4, 5). |
| `verdict/orchestration/story_footprint.py` | `StoryFootprintV1` + `collide()` — symmetric, case-insensitive, fail-closed path and authority collision detection (AC 2, 3). |
| `verdict/orchestration/concurrency_governor.py` | `GlobalConcurrencyGovernor.admit()` — stateless gate over story, coding-worker, and integration-slot caps with resource-pressure scaling (AC 9, 10, 11). |

When `VERDICT_MULTI_STORY=on`, the supervisor already wires `_check_admission`
through `ready_gate`, `story_footprint`, and `concurrency_governor` (line ~2223).
The flag defaults to off.  This ADR describes
how the supervisor would integrate them to move from single-story to governed
multi-story execution.

## Decision

### Admission pipeline

When the supervisor considers a candidate story it runs three checks in order.
Each check is fail-closed: unknown or missing evidence produces the safe
conservative outcome (WAIT, SERIALIZE, or DEFER — never ADMIT).

```
candidate story
    │
    ▼
ready_decision(labels, deps, main_sha)
    │
    ├─ EXCLUDED → skip (automation:manual)
    ├─ WAIT     → defer (deps not MAIN_VERIFIED)
    │
    ▼  READY
collide(candidate_footprint, running_footprint)   ← for each running story
    │
    ├─ SERIALIZE → defer (path / authority / unknown overlap)
    │
    ▼  PARALLEL (all pairs)
governor.admit(request, run_state, pressure)
    │
    ├─ SERIALIZE → defer (story cap)
    ├─ DEFER     → defer (worker/integration/pool cap, pressure)
    │
    ▼  ADMIT
start story
```

The supervisor evaluates one `--story` at a time (as today) and stops
admitting once the governor returns SERIALIZE or all candidates are evaluated.

### Lock scope changes

| Lock | Scope | Duration | Purpose |
|---|---|---|---|
| `supervisor.lock` (flock) | Repo-wide | **Short** — held only during candidate evaluation and process spawn, then released | Prevents two supervisor processes from racing on admission |
| Per-story worktree lock | Per-story | Full story lifetime | Existing lease model (`prime_state.py`) already provides this |
| Integration lock | Repo-wide | Short — held during merge / rebase only | Serializes merge-to-main (AC 13); stories run concurrently but integrate one at a time |

Today's flock is held for the entire run.  Under this design it becomes a
short admission lock: acquire, evaluate candidates, spawn workers, release.
Each story's lifetime isolation is handled by the existing per-story lease.

The integration lock is new.  It ensures only one story merges to `main` at a
time, preventing merge conflicts from concurrent fast-forward attempts.  After
a merge completes, other stories rebase against the new `main` HEAD before
their own integration attempt (AC 14, base-drift reconciliation).

### Fail-closed invariants

1. **Unknown footprint → SERIALIZE.**  `StoryFootprintV1.is_known()` returns
   `False` when no write paths are declared; `collide()` returns SERIALIZE.
2. **Unknown dependency evidence → WAIT.**  `ready_decision()` returns WAIT
   when any `DepEvidence` field is `None`.
3. **Unknown provider pool → DEFER.**  `governor.admit()` returns DEFER when
   the requested pool is absent from `pool_healthy_capacity`.
4. **NaN resource pressure → 1.0.**  `_clamp()` treats NaN as maximum
   pressure, reducing effective caps to 1.
5. **Invalid paths → SERIALIZE.**  Absolute paths, `..`-escapes, and
   empty-after-normalization paths force SERIALIZE.

### Late collision detection

A story's actual write set may diverge from its declared footprint at runtime.
The supervisor checks for unexpected overlap when a story completes (before
integration).  If the actual diff touches files claimed by a concurrently
running story, the completing story enters a SERIALIZE queue for integration
rather than proceeding immediately.  This is AC 15 (late collision fence).

## Consequences

**Positive:**
- Independent stories run concurrently, reducing wall-clock time for a backlog
  of disjoint work.
- Resource pressure (CPU, RAM, disk) automatically throttles admission,
  preventing overload without manual intervention.
- All three library modules are pure, deterministic, and already tested in
  isolation — the integration risk is in the supervisor wiring, not the logic.

**Negative:**
- Supervisor complexity increases.  The single-story path is simple; the
  multi-story scheduler must track running stories, footprints, and caps.
- Footprint declarations must be accurate for parallelism to be useful.
  Inaccurate or missing footprints fail to SERIALIZE (safe but slow).
- Debugging concurrent story failures is harder than single-story debugging.

**Neutral:**
- The existing lease model and per-story worktree isolation are unchanged.
- The CLI, API, and orchestration pipeline (`verdict/orchestration/`) are
  not affected by this change — the governor lives between the supervisor
  and worker dispatch only.

## Rollout

### Feature flag

A boolean flag `VERDICT_MULTI_STORY` (env var or config key) controls the
behaviour.  **Default: OFF** — today's single-story flock behaviour is
preserved until the operator explicitly enables multi-story.

| Flag value | Behaviour |
|---|---|
| `off` (default) | Single-story flock, identical to today. Governor and collision modules are not called. |
| `on` | Full admission pipeline: ready gate → collision → governor. |

### Staged caps

When enabled, the governor's `GovernorCaps` start conservative and can be
raised by the operator:

| Phase | `max_stories` | `max_coding_workers` | `max_integration_slots` |
|---|---|---|---|
| Initial | 2 | 3 | 1 |
| Stable (operator raises) | 3 | 6 | 2 |
| Library default | 3 | 6 | 2 |

### Kill switch

Setting `VERDICT_MULTI_STORY=off` at any time returns to single-story mode.
In-flight stories complete normally; no new stories are admitted until the
running count drops to zero, then the single-story flock resumes.

## Verification plan

### Unit tests

All three library modules already have unit tests merged on `main`.  Additional
integration-level tests for the admission pipeline:

- **Admission sequence test:** mock two candidate stories with disjoint
  footprints; assert both are ADMIT.
- **Collision serialization test:** two stories with overlapping write paths;
  assert the second is SERIALIZE.
- **Pressure reduction test:** at high pressure, effective caps drop; assert
  DEFER when running count equals reduced cap.
- **Flag-off test:** with `VERDICT_MULTI_STORY=off`, assert only one story
  is ever admitted regardless of footprints.
- **Late collision test:** a story's actual diff overlaps a running story's
  footprint; assert integration is serialized.

### Live dogfood

A 2-story concurrent run with disjoint footprints as the first live validation:

1. Story A: docs-only change (write paths: `docs/`).
2. Story B: test-only change (write paths: `tests/`).
3. Verify both stories complete without serialization.
4. Verify integration lock serializes their merges to `main`.
5. Verify base-drift rebase triggers for the second-to-merge story.

### Monitoring

- Supervisor logs emit structured JSON with `admit`/`defer`/`serialize`
  decisions and reasons for each candidate.
- A `supervisor.json` status file records current running stories, governor
  caps, and pressure readings.

## Operator decisions (2026-09-29)

The operator resolved the open decisions below on 2026-09-29. The feature flag `VERDICT_MULTI_STORY` stays **off by default**. These decisions define what "on" means.

| # | Decision | Resolution |
|---|---|---|
| 1 | Initial cap values | 2 stories / 3 coding workers / 1 integration slot (the "Initial" row above). The operator raises them explicitly. |
| 2 | Footprint source of truth | (b) Inferred from the story branch's diff against `main`. Unknown or empty footprints SERIALIZE (fail-closed). |
| 3 | Resource pressure inputs | Memory and swap. This host is memory-bound (8 GB). Pressure is the higher of used-RAM fraction and swap-use fraction; swap above 3 GB counts as pressure 0.8. CPU load is recorded but is not an input. |
| 4 | Provider pool health | OmniRoute live inventory plus the most recent probe result per route. A pool with no probe result is unknown, so DEFER. |
| 5 | Late collision policy | (a) Wait for the conflicting story to finish, then re-check before integration. |
| 6 | Kill-switch drain | Graceful drain. In-flight stories finish, and no new stories are admitted. |
| 7 | Base-drift after another story merges | Automatic rebase. The story re-runs its proof only when the rebase changed files in its footprint. |
| 8 | Integration slots | Always 1 for the single-`main` workflow. |
| 9 | Monitoring and alerting | Alert when pressure lowers the effective caps below the configured values, or when more than 50% of admissions are DEFER/SERIALIZE over 30 minutes. |

## Superseded: original open decisions

All 9 decisions originally left open in this ADR were resolved by the operator
on 2026-09-29.  The resolutions are recorded in the **Operator decisions** table
above.  The live `SupervisorGovernorConfig` defaults (2/2/1) predate this ADR;
the ADR initial caps (2/3/1) supersede them when enabled.

