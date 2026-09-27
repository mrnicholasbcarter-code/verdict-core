# Live controller failover run (BOD-264)

A real `verdict supervise` run through the operator's local OmniRoute gateway
and Prime, recorded 2026-09-27 (`--scope kr/`, throwaway two-node repo).
Unlike `docs/proof/demo-run` (fixtures only), every model call was live.

Command shape:

```bash
VERDICT_CHAOS_G0="#2=hang" verdict supervise --run-id ctrl-demo --runs-dir <runs> \
  --stall-seconds 60 --max-restarts 2 -- \
  --repo <repo> --graph graph.json --scope kr/ --max-parallel 1 --state-file <per-run>
```

What `controller-g0.log`, `controller-g1.log` and `events.jsonl` show:

1. generation 0: node `alpha` on `kr/claude-haiku-4.5` passes verification -> VALIDATED;
   its second executor call (node `beta`) is forced to hang (`VERDICT_CHAOS_G0`)
2. the supervisor sees no progress for 60 s, kills generation 0's process group
   (STALLED) and starts generation 1 with `--resume`
3. generation 1: `controller RESUMED 1 validated node(s) reused`; `alpha` is not
   re-executed; `beta`'s abandoned attempt is recorded and it runs as attempt 2
4. `beta` passes verification; both commits integrate
5. independent review by open-code-review on `kr/gpt-5.6-terra` -> PASS
6. supervisor: `COMPLETE`, 1 restart, exit 0

Cooldowns went to a per-run state file; real capacity was never cooled.

Verify locally:

```bash
verdict run-receipt live-controller-run --runs-dir docs/proof
```

Paths under `/tmp/verdict-live-ctrl2-*` are the throwaway repo the run used;
they are part of the digest-bound event log and are left unedited.
