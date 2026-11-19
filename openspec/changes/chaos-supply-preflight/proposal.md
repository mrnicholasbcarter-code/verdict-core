# Chaos rehearsal: enough proven FREE supply, and a start independent of the clean run

## Why
A full local rehearsal on main `aeaa917e` code (2026-11-19 13:06-13:11Z, FREE
capacity, local gateway, empty state) passed the preflight with
planner-eligible passes in 4 backend pools and the clean run `COMPLETE`. The
chaos run then ended `BLOCKED` with "planning failed on every eligible frontier
model" after its two injected planner faults fired.

The offline diagnosis (real runtime, stub executor, the three injections)
found two causes, both in `scripts/run_certification_rehearsals.py`:

- Supply. The chaos run consumes 5 independent pools: the quota fault cools
  one pool for two hours, the no-final-answer fault drops a second pool's
  model, the worker rate limit excludes the worker's pool and reassigns to a
  fourth, and the reviewer excludes implementer routes and their aliased pools
  (`verdict/orchestration/review.py`), so it needs a fifth. Measured: 3 and 4
  pools `BLOCKED`, 5 `COMPLETE` from the preflight's state. The 8-route cap
  spent half its probes on pools that fail every run and never reached others.
- Inherited state. The chaos run shared the clean run's
  `session-evidence.jsonl`. The clean run's real 429 was recorded there and
  scored a proven route 0.4, under the 0.5 bar of the session-evidence gate
  (`verdict/orchestration/eligibility.py`, `_task_gate`), so the route was
  rejected at `TASK_ELIGIBLE` and never re-probed. The expired pool cooldown
  itself did not block it: an expired negative is half-open `unprobed`.

## Decision
Recorded by the orchestrator under the operator's standing authority to act
autonomously (2026-11-19): chaos starts from the proven state as it stood
after the preflight and before the clean run, for both the health cache and
the session-evidence ledger. This reverses #1157's choice that the chaos copy
"also holds what the clean run really observed". The two runs become
independent.

## What Changes
- `PREFLIGHT_MIN_PLANNER_POOLS` 3 -> 5, with the same named fail-closed
  message and the budget it covers.
- `PREFLIGHT_MAX_ROUTES` 8 -> 13, one probe per pool: at most 13 x 3 agentic
  turns = 39 model requests, under 40. FREE only, as before.
- `_seed_chaos_state` runs right after the preflight, before the clean run. It
  makes private byte copies (0600, `O_EXCL`) of `health-cache.json` and
  `session-evidence.jsonl` (empty when the preflight left none). The chaos
  command gets `--state-file`, `VERDICT_HEALTH_CACHE` and
  `VERDICT_SESSION_EVIDENCE` (the path `SessionLedger()` already honours). A
  missing, invalid or proof-less cache now stops the script before either run.
- The clean run's command and environment do not change.

## Not in this change (follow-ups)
- `verdict/orchestration/runtime.py:_record_session_outcome` (about line 2152):
  do not score pool-cooldown categories (real 429/402/401) as session failures.
- `verdict/capacity.py:89-90` vs `:125-126`: a `:free` suffix outranks the
  credit-provider rule, so a credit provider's `:free` route counts as a FREE
  pool and refuses in band at first use.
- `verdict/identify.py:decide` (326-371): skip routes whose connection is
  inactive or auth/payment-failed without spending a cap slot.

## Impact
- `scripts/run_certification_rehearsals.py`, `docs/certification/README.md`,
  `tests/test_certification_chaos_proof.py`,
  `tests/test_certification_rehearsal_workflow.py`,
  `tests/test_certification_failure_diagnostics.py`.
- No change under `verdict/`.
