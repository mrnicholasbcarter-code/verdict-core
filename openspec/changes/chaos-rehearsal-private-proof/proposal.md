# The chaos rehearsal starts from a private copy of the certification proof

## Why
A full local rehearsal on main `27e34b07` (2026-11-19 12:06Z, FREE capacity,
local gateway, empty state) passed the preflight (planner-eligible passes in 4
backend pools) and the clean run (`COMPLETE`, review PASS, receipt verified).
The chaos run then ended `BLOCKED` after three events, with "planning failed on
every eligible frontier model; last: none eligible", before any injected fault
fired.

The cause is in `scripts/run_certification_rehearsals.py`, not in the product.
`verdict orchestrate --inject` keeps its health state in a per-run directory
and reads `health-cache.json` beside that state file, so that injected faults
never cool down real capacity. The script relied on the cache in the shared
`VERDICT_HOME`, which an `--inject` run deliberately does not read. The chaos
state directory held no health cache, so the chaos run had no agentic proof.

## What Changes
- Before the chaos run, the script copies the shared health cache, byte for
  byte, to `health-cache.json` in a directory it owns
  (`$RUNNER_TEMP/verdict-certification-chaos/`). It passes
  `--state-file <that directory>/chaos-health.json` and sets
  `VERDICT_HEALTH_CACHE` to the copy for the chaos child only, so the selector,
  admission, dispatch permits and the refresh hook all resolve that one file.
- The copy is taken when the chaos run is about to start. It therefore holds
  what the clean run really observed (cooldowns, failed routes). Proof
  timestamps are not rewritten, so no proof is extended.
- The script stops before the chaos run, with a fixed message, when the shared
  cache is missing, is not a valid health cache, or holds no agentic proof that
  is still inside its time-to-live.
- The preflight still runs once. The clean run's command and environment do
  not change. `verdict orchestrate`'s isolation rule does not change.
- The private directory is outside the run directory, so it is neither scanned
  into nor copied to the uploaded evidence.

## Impact
- `scripts/run_certification_rehearsals.py`, `docs/certification/README.md`,
  `tests/test_certification_chaos_proof.py`,
  `tests/test_certification_rehearsal_workflow.py`.
- No change under `verdict/`. `PREFLIGHT_MIN_PLANNER_POOLS` stays 3.
