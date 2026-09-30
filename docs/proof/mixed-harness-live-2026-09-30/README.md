# Mixed-harness live proof: one run, two worker harnesses (2026-09-30)

**What this proves (BOD-284, the last AC of BOD-177).** A single live `verdict orchestrate` run on main `7c2d545` supervised two different worker harnesses in the same controller session and the same cockpit:
- **node-1** ran through **Prime Agent headless** (`harness: prime-headless`);
- **node-2** ran through the **direct OmniRoute gateway** (`harness: direct-gateway`).

Both nodes were live model calls (`executor_kind: live`), both validated, the integration barrier merged both commits, the independent review passed, and the receipt is COMPLETE with integrity OK.

Command (the controller's session; the key and gateway come from the environment):

```
verdict orchestrate --repo <RUN_ROOT>/repo --graph graph.json --runs-dir <RUN_ROOT>/runs \
  --scope cc/,cx/ --max-parallel 2 --attempt-timeout 600 --run-deadline 1800 \
  --state-file <RUN_ROOT>/health.json \
  --executor direct-gateway --executor-map node-1=prime --plain
```

| Evidence | Where |
|---|---|
| `run_started` records `executor: "mixed"` | `run/events.jsonl` seq 1 |
| node-2 terminal: `harness: direct-gateway`, `executor_kind: live`, `ok: true`, route `cc/claude-haiku-4-5-20251001` | `run/events.jsonl` seq 20 |
| node-1 terminal: `harness: prime-headless`, `executor_kind: live`, `ok: true`, same route | `run/events.jsonl` seq 25 |
| Review PASS by open-code-review v1.12.9 on `cx/gpt-5.5`, a different family from both workers | `run/events.jsonl` seq 34; `run/review/` |
| Each receipt attempt records its `harness` | `run/receipt.json` → `nodes[].attempts[].harness` |
| The cockpit shows the harness per node (`verdict watch run --runs-dir docs/proof/mixed-harness-live-2026-09-30 --replay`) | WORKERS table: `node-1 … prime-headless`, `node-2 … direct-gateway` |

Verify:

```
verdict run-receipt --runs-dir docs/proof/mixed-harness-live-2026-09-30 run
```

It reports `COMPLETE … review PASS` and `integrity: OK (events digest verified)`.

**What this does NOT prove.**
- Failover across harnesses. Both nodes succeeded on their first attempt, so there were no failures or reassignments.
- Different models per harness. Both harnesses chose the same route (`cc/claude-haiku-4-5-20251001`); the proof is about harness independence, not model selection.
- Direct-gateway identity attestation. The direct-gateway attempt's `reported_model` is `claude-haiku-4-5-20251001` without the `cc/` prefix, so the receipt records `route_identity: mismatch` for node-2 and a `route_identity_warning`. The intended and actual routes are the same model; the gateway response simply omits the provider prefix. This is recorded, not hidden.
- Non-trivial work. The nodes write one line each. The run spent only subscription capacity (scope `cc/,cx/`); node-1's Prime usage reports `cost_usd: 0.0`.

**Scrubbing.** The copied run replaces the temporary run root `/tmp/verdict-mixed-live-…` with `<RUN_ROOT>` (4 occurrences in `events.jsonl`). `worktrees/` was not copied. The receipt was then rewritten with the real `write_run_receipt` so its events digest matches the scrubbed log. `verify_run_receipt` reports no problems, and `verdict run-receipt` reports integrity OK.
