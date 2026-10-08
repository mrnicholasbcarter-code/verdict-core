# Live failover smoke run (BOD-268)

A real `verdict orchestrate` run through the operator's local OmniRoute gateway
and Prime, recorded 2026-09-27 with `scripts/live_failover_smoke.py --scope kr/`.
Unlike `docs/proof/demo-run` (fixtures only), every model call here was live.

Chain recorded in `events.jsonl` and projected in `receipt.json`:

1. selection: bounded low-risk node -> `kr/claude-haiku-4.5` (cheapest sufficient)
2. attempt 1: synthetic route-scoped 5xx injected (`fault_injected: true`)
   -> `upstream_temporary`, cooldown on that route only (per-run state file;
   real capacity was never cooled)
3. reassign the same node -> `kr/claude-sonnet-4`, attempt 2 completes in 17s
4. node verification `grep -qx ok smoke.txt` passes -> VALIDATED, integrated
5. open-code-review on `kr/gpt-5.6-terra` (different family from the
   implementer) recorded observed model == selected and PASS. Its retained raw
   output says `skipped`, with no items selected/completed and zero reviewed
   files or tokens; no semantic review was demonstrated
6. run COMPLETE

Verify locally:

```bash
verdict run-receipt live-smoke-run --runs-dir docs/proof
```

Paths under `/tmp/verdict-live-smoke-*` are the throwaway repo the run used;
they are part of the digest-bound event log and are left unedited.
