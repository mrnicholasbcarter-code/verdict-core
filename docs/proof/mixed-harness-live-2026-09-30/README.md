# Mixed-harness live proof: one run, two worker harnesses (2026-09-30)

**What this proves (BOD-284, the last AC of BOD-177).** A single live `verdict orchestrate` run supervised two different worker harnesses in the same controller session and the same cockpit. The capture author reports that the run used main `7c2d545`; the historical events/receipt do not attest the producing engine SHA:
- **node-1** ran through **Prime Agent headless** (`harness: prime-headless`);
- **node-2** ran through the **direct OmniRoute gateway** (`harness: direct-gateway`).

Both nodes were live model calls (`executor_kind: live`), both validated, and the integration barrier merged both commits. The reviewer recorded PASS and the receipt is COMPLETE with integrity OK, but the retained OCR output says `skipped` with zero selected/completed items and zero reviewed files or tokens. No semantic review was demonstrated. This run predates the reviewer check that now rejects skipped or zero-coverage OCR output as `ERROR`.

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
| Recorded review PASS by open-code-review v1.12.9 on `cx/gpt-5.5`, a different family from both workers; raw OCR says `skipped`, so this does not demonstrate semantic review | `run/events.jsonl` seq 34; `run/review/` |
| Each receipt attempt records its `harness` | `run/receipt.json` → `nodes[].attempts[].harness` |
| The cockpit shows the harness per node (`verdict watch run --runs-dir docs/proof/mixed-harness-live-2026-09-30 --replay`) | WORKERS table: `node-1 … prime-headless`, `node-2 … direct-gateway` |

Verify:

```
verdict run-receipt --runs-dir docs/proof/mixed-harness-live-2026-09-30 run
```

It reports `COMPLETE … review PASS` and `integrity: OK (events digest verified)`. The recorded PASS is not evidence of actual review coverage.

**What this does NOT prove.**
- Failover across harnesses. Both nodes succeeded on their first attempt, so there were no failures or reassignments.
- Different models per harness. Both harnesses chose the same route (`cc/claude-haiku-4-5-20251001`); the proof is about harness independence, not model selection.
- The provider-qualified identity of node-2's model. The direct-gateway attempt reported `claude-haiku-4-5-20251001`, not the selected `cc/claude-haiku-4-5-20251001`. The receipt records `route_identity: mismatch` for node-2 and a `route_identity_warning`, and which provider actually served node-2 is unverified. (node-1, through Prime, reported the full `cc/claude-haiku-4-5-20251001` and is a `match`.)
- Non-trivial work. The nodes write one line each.
- Spending. The workers were selected from routes the eligibility ladder classifies as `subscription` (scope `cc/,cx/`). node-1 reported `cost_usd: 0.0`; node-2 reported `cost_usd: null`, and the review's cost is not recorded. No run-wide cost claim is made.

**Scrubbing (the bundle author's account, with the digests to check it).** `worktrees/` was not copied. The temporary run root was replaced with `<RUN_ROOT>` in `events.jsonl` (4 occurrences), and the receipt was rewritten with `verdict.orchestration.receipt.write_run_receipt(run_dir)` so its `events_digest` matches the scrubbed log:

| | sha256 of `events.jsonl` |
|---|---|
| original run (as written by the run; its receipt carried this digest and verified with no problems) | `65e5edd75c742163d97899e9784febd79c6820117a137a967eb33f78e9eae525` |
| after `sed 's|<run root>|<RUN_ROOT>|g'` (committed here; equals the committed receipt's `events_digest`) | `1417067b18ed28a86c2bc7013e74570c5e616865e4e357b7940949bd80c027dd` |

The original log is not committed, so the first digest is the author's record. The second one can be checked with `sha256sum run/events.jsonl` and `verdict run-receipt`, which reports integrity OK.
