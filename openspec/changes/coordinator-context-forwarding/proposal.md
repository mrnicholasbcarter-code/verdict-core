# Proposal: BOD-303 §3 coordinator context forwarding

## Why

Today every worker node gets a retrieval-only pack built from local code facts alone
(`build_worker_context`). A research node's verified answer never reaches a dependent
implement node's context. An implement node's diff never reaches a downstream integrate
node's context. The goal, constraints and contracts that bind the whole run are absent
from every pack. Workers repeat research that earlier nodes already settled, spending
tokens on questions the run already answered.

This change wires the coordinator's existing knowledge — predecessor verified outputs,
the run goal, applicable constraints and node contracts — into dependent packs, with no
frontier call added per node. The lift bench (extended to a two-node synthetic shape)
gates the change on measured pass-rate gain.

KNOWN: `verdict/orchestration/run.py:1162–1236` — `hydrate_with_research_pack` builds
per-node packs with no upstream content. `verdict/orchestration/runtime.py:2584` —
`_node_base` merges predecessor commits; `NodeRun.commit` holds the validated SHA.

NOT AVAILABLE: a `run_dir/results/` artifact or full-output storage at VALIDATED.

## What Changes

### Part 1 — Verified output storage at VALIDATED

`verdict/orchestration/runtime.py` (`_validate`): after verification passes, write
`run_dir/results/{node_id}.json` containing `{"output": terminal.output[:32768],
"git_head": run.commit, "node_id": node_id, "validated_at": iso_now()}` with
`redact_text` applied. The existing `_save_attempt` tail artifact is unchanged.

### Part 2 — Upstream forwarding in hydration

`verdict/orchestration/run.py` (`hydrate_with_research_pack`): load `run_dir/results/`
for each `node.depends_on` predecessor. Build `ContextUnit` objects with:
- `slot_type="evidence"`, `key="upstream:{pred_id}"`, `source_uri="urn:verdict:run:{run_id}:{pred_id}:validated"`, `source_digest=sha256(output_text)`.
- Research predecessors: include full output text (bounded to 32KB via D8).
- Implement predecessors: include the git diff from `run_dir/results/{node_id}-diff.patch` (8KB cap), `source_uri` including `diff_sha256`.
- If the results artifact is absent or unreadable, emit an omission and continue without blocking.

`verdict/orchestration/runtime.py` (`_validate`): capture `git diff --stat` on `node.owned_files` relative to `_node_base`; write to `run_dir/results/{node_id}-diff.patch` (bounded to 8KB). Apply `redact_text`.

### Part 3 — Shared core

`verdict/orchestration/run.py` (`hydrate_with_research_pack`): build one `ContextUnit`
with `key="required:shared-core"`, `slot_type="instructions"`, bounded to 1500 tokens,
containing the goal (from parameter), constraints text (from parameter; if absent, include
only SHA and emit omission), and ADR/decision ids from `node.required_context`.
Source URI: `urn:verdict:run:{run_dir.name}:goal` for goal; `urn:verdict:run:{run_dir.name}:constraints` for constraints.

### Part 4 — Lift bench extension

`verdict/context_lift.py` (`run_context_lift`): extend to a two-node synthetic shape.
- Node A ("research"): writes the planted token as its verified output.
- Node B ("dependent"): must recall the token using the forwarded pack (treatment arm) or a same-budget-padded pack (control arm C).
- Run at two `max_context_bytes` values: 4000 tokens (small) and 8000 tokens (large).
- Arm C uses the same byte budget as arm B, padded with noise units.
- A stop rule file must be present before the first run; its hash is embedded in the evidence JSON.
- Routes: `_FREE_COST_CLASSES` only. Routes that fail the liveness check are skipped.
- Evidence written to `$VERDICT_HOME/context-lift/ctx-forwarding-{stamp}.json` and `latest-forwarding.json`.
- `verdict bench context-lift --forwarding` runs the new shape; plain `--forwarding` flag disables pad if absent.

## Capabilities

### Modified Capabilities

- `orchestration.worker-context` (from `bod-303-worker-context-packs`): adds upstream
  forwarding and shared core. Pack may include `upstream:{pred_id}` and
  `required:shared-core` units.
- `context-lift` (from `receipt-pack-size-and-lift-schedule`): adds two-node shape,
  control arm, two pack sizes, stop-rule enforcement.

## Impact

- **Admission invariants**: forwarding is advisory context; it never changes admission,
  route selection or the execution-path authority. Passes `tests/test_admission.py`.
- **Receipt binding**: `run_dir/results/` artifacts are durable and redacted; they do
  not enter `events.jsonl` and do not change the receipt digest binding. Passes
  `tests/test_orch_receipt.py`.
- **Free models**: shared core is capped at 1500 tokens; upstream items drop under
  priority-based overflow (D5). No new frontier calls are added.
- **Non-.py code facts**: still absent from worker packs. Recorded in bench output as
  `"non_python_code_facts_absent": true`. Remains a known gap for a future change.
- **PK1 (#1247) scope boundary**: contradiction withdrawal (withdrawing a forwarded item
  when a later verified result contradicts it) is PK1's scope. This change does not
  implement it; a stale or missing results artifact produces an omission, not a failure.
- **RS1 (#1246) scope boundary**: the full three-arm research bench (free gatherers +
  frontier synthesis) remains RS1's scope. This change adds only the two-node synthetic
  forwarding bench.

## Acceptance criteria

 Dependent node packs include upstream verified results per recommended option C (research→full answer; implement→diff from merged state; no interface extraction).

 Diff summaries come from `git diff {base}..{NodeRun.commit}` on owned files, captured at VALIDATED and written to `run_dir/results/{node_id}-diff.patch`.

 Every run's packs include a shared core (goal + constraints text if available + ADR/decision ids from required_context), bounded to 1500 tokens, with per-item source URIs.

 The lift bench (`verdict bench context-lift --forwarding`) runs the two-node shape at two pack sizes with a control arm. A stop rule file must be committed before the first run. The bench shows B pass-rate ≥ A pass-rate at ≥ one pack size on ≥ 6 tasks.

 `run_dir/results/{node_id}.json` written at VALIDATED with full output capped to 32KB, `redact_text` applied.

- Every upstream-forwarded ContextUnit carries `source_uri` and `source_digest`; uncited items are excluded by the ContextPackCompiler.
- Upstream items absent (artifact missing) → omission note, not a blocked node.
- No frontier call is added per node.
- `verdict bench context-lift --forwarding` exits 0 on blocked routes (consistent with existing bench behaviour).
- ruff check/format, mypy --strict, reachability ratchet, check_lint_gates.py pass.
- (post-merge) Context-lift forwarding evidence appears under `$VERDICT_HOME/context-lift/`; `verdict doctor` shows context-lift timer ok.
