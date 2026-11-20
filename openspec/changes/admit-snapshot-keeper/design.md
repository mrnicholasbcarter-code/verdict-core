# Design — admit-snapshot-keeper

## Context

The hourly `verdict-acceptance` job's `serve` check was red at 15:00, 16:00 and
18:00Z on 2026-11-20.  Each time a freshly started `verdict serve` could not
fetch a live OmniRoute admission snapshot within its 30 s aggregate deadline
(host was loaded).  The on-disk last-good snapshot (`~/.verdict/admit-last-good.json`)
was older than `ADMIT_LAST_GOOD_MAX_AGE_SECONDS = 900` s (at 18:00Z it was last
written at 17:02:42Z).  With no fresh snapshot and an expired last-good, the relay
refused dispatch.  PR #1198 made the refusal report the true reason.  Nothing
keeps the last-good file fresh except live traffic and the hourly acceptance job.

## Goals / Non-Goals

**Goal**: a background timer that calls `load_omniroute_admit_snapshot` every 5
minutes so the last-good file is always younger than the 15-minute freshness
window, even when the host is idle and serve restarts on a loaded host.

**Goal**: the timer makes no model or inference calls.

**Goal**: `verdict doctor` reports the timer state and the last-good file's age.

**Non-goal**: change the serve or relay code paths — only the last-good file and
catalog cache need to be kept warm.

**Non-goal**: probe or verify routes — that is `census`, not admission snapshot.

## Decisions

**D1**: Use `catalog_scope="full"` (the default). The serve/relay path calls
`load_omniroute_admit_snapshot` without specifying scope, which defaults to
`"full"`. The keeper must use the same scope so it populates both
`admit-last-good.json` (non-catalog surfaces) and `catalog-cache.json` (the
full `/v1/models` catalog that the serve fallback path reads with
`max_age_seconds=900`). Using `"gated"` would skip the catalog update and leave
the serve fallback unable to use a stale `admit-last-good.json`.

**D2**: `aggregate_deadline_seconds=120` (vs serve's 30 s). The keeper is a
background job; it must succeed on a loaded host, which is exactly when the
serve path cannot.

**D3**: New top-level CLI group `verdict admission`. No existing group fits:
`census` probes routes (model calls), `identify` is a different surface,
`discovery` is read-only diagnostics. The admission snapshot refresh is a
distinct operational concern.

**D4**: Use `OnUnitInactiveSec=300` + `OnStartupSec=60` (not `OnCalendar`).
`OnCalendar` is wall-clock; `OnUnitInactiveSec` ensures 5 minutes between
run completions regardless of drift. `Persistent=true` is omitted — it is
meaningful only with `OnCalendar`.

**D5**: Doctor freshness row uses `"warning"` (not `"failed"`) so it does not
block `verdict doctor` before the timer is first installed. The timer row itself
uses `"failed"` → `"not-configured"` pre-setup via `first_run_status`.

## Risks / Trade-offs

**Network cost**: every ~5-min run downloads the full `/v1/models` catalog (~27 MB)
when the catalog cache (TTL=300 s) expires. The catalog TTL matches the timer
interval so each run incurs one catalog fetch. Total: ~6 HTTP requests per run,
~27–28 MB per run. This is acceptable; the timer replaces ad-hoc fetches by a
freshly starting serve process.

**Security / trust implications**: the keeper reads OmniRoute catalog and
provider endpoints only; no model calls; `admit-last-good.json` is 0600.

**Routing / context / memory implications**: the keeper is advisory — it only
populates caches. The admission and routing logic is unchanged.

**Migration / rollback**: `verdict admission refresh --uninstall` removes both
units. The last-good file is never deleted; the serve path continues to work
from the file until it expires.

**ADR impact**: none. No new ADRs required. BOD-342 D15 mission: the keeper
keeps free routes available without paid model calls.

## Tasks

### 1. Implementation

- [x] 1.1 `verdict/admit_snapshot_service.py` — service manager
- [x] 1.2 `verdict/commands/parsers_admission.py` — CLI parser
- [x] 1.3 `verdict/commands/dispatch.py` — `_cmd_admission` and `_cmd_admission_refresh`
- [x] 1.4 `verdict/cli.py` — register parsers_admission
- [x] 1.5 `verdict/setup_host.py` — `ADMIT_SNAPSHOT` action with `_observe`/`_ACTIONS`/`run_host_action`
- [x] 1.6 `verdict/doctor_diagnostics.py` — `_admit_snapshot_timer_row`, `_admit_snapshot_freshness_row`, wired into `installed_service_rows`
- [x] 1.7 `tests/test_admit_snapshot_service.py` — unit tests

### 2. Live checks

- [ ] (post-merge) 2.1 install the timer with `verdict setup --non-interactive --apply --allow host.admit-snapshot-timer` and confirm `~/.verdict/admit-last-good.json` stays younger than 15 min for 2 h with no manual step; record raw output
- [ ] (post-merge) 2.2 the hourly acceptance `serve` check is green for 3 consecutive runs after the timer is installed
