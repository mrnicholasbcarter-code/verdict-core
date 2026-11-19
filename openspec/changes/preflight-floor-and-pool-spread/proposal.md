# Certification preflight probes only planner-floor routes and spreads across backend pools

## Why
Three local empty-state rehearsals on 2026-11-19 (FREE capacity, local gateway)
showed two faults in the preflight added by cert-preflight-planner-tier
(GitHub issue #1151):

- In every run 2 of the 8 probe slots went to Cloudflare routes below the
  planner's 100,000-token context floor (24,000 and 80,000 tokens). A pass
  there can never count as planner-eligible.
- Planner-eligible passes covered 3 backend pools each time. In two runs, two
  of the passes sat in the one `google-antigravity` pool. The workers used
  that pool, were reassigned once to `openrouter-free`, and the run ended
  BLOCKED with "no independent reviewer eligible": the reviewer excludes the
  implementers' routes and aliased pools, and no third pool had a usable route.

## What Changes
- `verdict identify --screen` gains `--min-context-tokens N` and
  `--require-tools`. A route whose context window is unknown or below N, or
  whose tools support is not known to be true, is reported as
  `skipped_task_floor`. It is not probed and takes no cap slot.
- `verdict identify --screen` gains `--max-per-pool N`, counted by
  `provider_catalog.backend_pool`, so prefixes that share one quota share one
  count. Pools are covered breadth-first. A route over the pool cap is
  reported as `skipped_pool_cap`, and each row reports `backend_pool`.
- The rehearsal preflight passes the context and tools floor from the shared
  `PLANNER_*` constants and `--max-per-pool 1`. The total cap stays 8.
- The preflight counts the distinct backend pools among planner-eligible
  passes and fails closed, before orchestration, when there are fewer than 3.
  `certification-failure.json` carries the count and the requirement as numbers.

## Impact
Admission, the planner's requirements and the reviewer's exclusions are
unchanged. The new options only remove routes from the probed set or change
which admissible route is probed first; no option adds a candidate or grants
eligibility. Screens without the options produce the same output as before
(pinned by a golden file), so the installed screen timer is unaffected. The
preflight stays FREE-only, consented and bounded by the same caps.

The requirement of 3 pools is what the evidence supports: a clean run that
reassigns once uses two pools and review needs a third. The chaos rehearsal
may need more; that has not been measured.
