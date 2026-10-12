# ADR-038: Measured improvement cycle above a supervised run

- **Status:** Accepted
- **Date:** 2026-10-12
- **Amends:** [ADR-036](ADR-036-goal-to-receipt-orchestration.md) (adds a layer above one run)
- **Related:** [ADR-0001](0001-verdict-control-plane-invariants.md),
  [ADR-037](ADR-037-supervisor-concurrency-governor.md)

## Context

ADR-036 gives Verdict one bounded run: goal, plan, DAG, workers, review, receipt.
`verdict supervise` keeps that run's controller alive. After the run ends, nothing
in Verdict decides what to do next: an operator reads the receipt, works out why a
run was BLOCKED, decides whether a relaunch could succeed, changes the inputs and
starts again. Verdict already had the parts this needs (the planner's
`CONSTRAINTS:` section, `verify_run_receipt`, `completion_verdict`,
`derive_claims`), but they were not connected.

## Decision

`verdict improve` runs bounded **cycles** above `verdict supervise`
(`verdict/orchestration/improvement.py`). Each cycle:

1. **Plans.** It fingerprints its inputs: goal, repo HEAD, orchestrate arguments,
   the lesson text, and the Verdict build (version, commit, dirty flag). If an
   earlier cycle with the same fingerprint was NOT_MET, the cycle is refused
   before anything runs, unless that cycle recorded a capacity reset that has
   since passed.
2. **Acts.** It runs one supervised orchestrate run, passing earlier planner
   lessons through `verdict orchestrate --constraints-file`.
3. **Measures.** Only from evidence Verdict can recompute: `verify_run_receipt`,
   `completion_verdict` and the `validation_passed`, `independent_review` and
   `receipt_integrity` claims. A cycle is MET only when the run is COMPLETE and
   all three claims are VERIFIED. A missing or unverifiable receipt is NOT_MET.
4. **Learns.** A deterministic classifier maps the run's recorded `run_finished`
   cause to one layer (environment, selection, planning, context, review,
   verification, controller, execution) or, when the run completed but
   independence was not shown, `review_independence`. Lesson text comes from a
   fixed per-layer table, never from files, so the cycle log can only select
   among Verdict's own lessons. Environment and review-independence lessons need
   the operator and are never sent to the planner. When capacity stopped the run
   (a selection blocker, or every failed attempt a quota/rate limit) and a quota
   cooldown is still in force, the cycle records when capacity returns.

`--once` runs one cycle; `--cycles N` (at most 5) stops at the first MET cycle, a
refused relaunch, or an operator lesson, and waits once for a capacity reset of at
most 15 minutes. `--verify` recomputes every measured cycle from its run receipt.

## Consequences

- An operator gets a measured, receipt-backed answer and a concrete next lesson
  instead of a free-text judgement; a self-reported result is never accepted.
- Verdict cannot spin: the cycle count is bounded, and the same inputs after a
  failure are refused unless time (a capacity reset) or code changed.
- The cycle log (`<cycles-dir>/cycles.jsonl`) is a working record, not
  tamper-evident storage. The relaunch guard stops wasted runs; it is not a
  boundary against the operator. Run receipts stay digest-verified.
- Not yet built: lessons from many runs (only the per-layer table), bounded
  `RuntimePolicy` changes as lessons, and event-driven waiting instead of a
  bounded sleep.

## Evidence

On 2026-10-12, four live cycles on a throwaway repository: cycles 1 and 2 were
NOT_MET because the reviewer shared a model family with a replaced worker attempt
(fixed: review now excludes every attempted route); cycle 3 was refused as
unchanged (fixed: the Verdict build is part of the fingerprint); cycle 4 was MET
with all three claims VERIFIED and `verify_cycles` clean. Offline behaviour is
covered by `tests/test_orch_improvement.py`.
