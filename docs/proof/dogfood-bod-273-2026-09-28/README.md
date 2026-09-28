# First complete real-model dogfood run (BOD-273)

A full `verdict orchestrate` dogfood run on real subscription capacity through
Claude Code Max (`cc/*`) and tracked as Linear issue BOD-273.

**Goal**: BOD-273 — `verdict doctor` must report a shared-memory auth failure as 
its own degraded reason. Today diagnose_shared_memory maps provider status 
'auth_failed' to reason 'unwritable'. Acceptance: diagnose_shared_memory returns 
a reason starting with 'auth_failed' when the provider status is auth_failed; 
'unwritable' is still reported for real write-permission failures; an integration 
test runs the real doctor entry point.

**Engine SHA**: 08083f6 (origin/main, pinned via PYTHONPATH for the run; includes #698)

**Models**:
- Planner: `cc/claude-haiku-4-5-20251001`
- Workers: `cc/claude-sonnet-4-5-20250929`
- Review: `codex/gpt-6-sol` (open-code-review v1.12.9)

**Token usage** (workers only, from the receipt; planner usage is not recorded):
- `cc/claude-sonnet-4-5-20250929`: 1,246 input, 1,692 output across 3 attempts

**Wall time**: 23.2 minutes (1,391 seconds)

**Review verdict**: PASS (20.6 seconds)

**Result**: PR #703

**Capacity**: Real-model run on subscription capacity (cc/*)

This is the first complete orchestration run using real models (not fixtures)
on a real Verdict story, end to end: planning, worker dispatch, per-node
verification, ownership and integration barriers, and independent review.

The planner chose the `WORKER_CRITIC` topology (4 nodes in 4 sequential layers,
`max_parallel` 1), so the workers ran one after another, not in parallel. Every
node passed on its first attempt, so this run shows no failover; see
`docs/proof/live-controller-run/` for a recorded controller failover.

Verify locally:

```bash
verdict run-receipt docs/proof/dogfood-bod-273-2026-09-28
```

Paths under `/tmp/dogfood-runs3/` and `/home/nick/dev/verdict-core/` in 
`events.jsonl` are part of the digest-bound event log and are left unedited 
per receipt integrity requirements.
