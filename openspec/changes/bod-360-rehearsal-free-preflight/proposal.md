# Certification rehearsal FREE-only agentic preflight

## Why
The certification rehearsal starts with an empty `VERDICT_HOME`. Since the
agentic gate in `EligibilityLadder._task_gate`, a FREE frontier planner that
needs tools must have fresh agentic proof in the health cache. A cold home has
none, so every FREE route fails `no_agentic_probe` and planning ends with
"none eligible". The rehearsal has never passed.

## What Changes
- Before the orchestrate loop, `scripts/run_certification_rehearsals.py` runs
  `verdict identify --screen --allow-live-probe --capacity free` with small
  explicit bounds (2 routes per provider, 4 routes, 10 minute wall cap), in the
  same environment and `VERDICT_HOME` as orchestrate.
- The screen runs the existing consented three-turn agentic probe and writes
  the health cache that the ladder reads. No proof is copied or synthesized.
- When the screen fails, prints invalid JSON, or reports zero checked or zero
  passed routes, the rehearsal stops with `RehearsalConfigurationError` before
  orchestrate runs.
- Orchestrate still runs with `--capacity free`.

- Preflight error messages are fixed text; child output is never included,
  because `__main__` prints these errors verbatim.

## Capabilities

### New Capabilities
- `certification-rehearsal`: FREE-only bounded agentic preflight before rehearsal orchestration, failing closed.

### Modified Capabilities
- None.

## Impact
Admission, the agentic gate and capacity policy are unchanged. Rehearsals stay
FREE-only; subscription, metered and unknown capacity are not screened or
dispatched. The preflight makes FREE provider calls only, within its bounds.
No new dependencies. Code: `scripts/run_certification_rehearsals.py`. Tests:
`tests/test_certification_rehearsal_workflow.py`,
`tests/test_certification_failure_diagnostics.py`.

## Evidence boundary
KNOWN: `_task_gate` returns `no_agentic_probe` for FREE tool routes with no
health-cache entry, and `identify --screen` writes agentic results to
`health_cache.default_cache_path()`, which honours `VERDICT_HOME`. KNOWN: the
screen JSON has `routes_checked` and `passed` (`verdict/identify.py`
`screen_models`). INFERRED: one passing FREE route that meets the planner floor
lets planning start. NOT AVAILABLE until a live run: whether the screened
routes meet the planner's context and tier floor on the real gateway.

## Acceptance criteria
- The preflight command is FREE-only and bounded (test).
- Zero passing routes, a non-zero exit or invalid JSON stop the rehearsal
  before orchestrate with `RehearsalConfigurationError` (test).
- Orchestrate still runs with `--capacity free` (existing policy test).
- On merged main, `certify-rehearsal.yml` gets past planning (post-merge task).
