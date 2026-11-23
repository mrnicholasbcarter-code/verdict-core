# Design: Coordinator context forwarding

## Context

`verdict/orchestration/run.py::hydrate_with_research_pack` builds a per-node pack from
local code facts only (`build_worker_context`). `verdict/orchestration/runtime.py` stores
only the last 2000 characters of each node's output in `run_dir/attempts/{node_id}-a{n}.json`.
No path exists to load a predecessor's verified output into a dependent node's pack.

The run goal and constraints are available as parameters to `hydrate_with_research_pack`
but the constraints text is not persisted in `events.jsonl` (only its SHA-256 is emitted
in `run_started`). Node contracts (`acceptance`, `verification_command`) are in the graph
JSON in `run_dir/`.

`verdict/context_lift.py::run_context_lift` tests single-node retrieval (unaided vs packed).
It cannot test a two-node forwarding shape because no "upstream result" exists in the
single-node model.

Decision log references: D9 (research once, workers reuse), D10 (context autopilot).
Issue references: BOD-303 §3 (#1227), RS1 (#1246), PK1 (#1247).

## Goals / Non-Goals

**Goals:**
- Forward verified upstream outputs into dependent node packs with no frontier call.
- Add a shared core (goal, constraints, applicable decisions) to every pack.
- Ensure every forwarded item carries a source URI and content digest.
- Extend the lift bench to a two-node shape with a control arm at two pack sizes.
- Durable per-node results artifact written at VALIDATED.

**Non-Goals:**
- Interface extraction from implement nodes (Python-only AST; deferred, see D6).
- Non-.py code facts in worker packs (known gap; record in bench output).
- Contradiction withdrawal mid-run (PK1 scope, #1247).
- Research stage (free gatherers + frontier synthesis) for pack building (RS1 scope, #1246).
- Changes to admission, ranking or the execution-path authority.

## Decisions

**Agreed (operator, 2026-11-23), D1:** Upstream results to forward per node kind.
**Chosen: C** — research→full verified answer (capped 32KB); implement→diff from
`git diff {base}..{NodeRun.commit}` on owned files (capped 8KB); no interface extraction
(avoids D6 dependency). Record interface extraction as a known gap.

**Agreed (operator, 2026-11-23), D2:** Source of diff summaries for implement nodes.
**Chosen: A** — capture at VALIDATED via a new `run_dir/results/{node_id}-diff.patch`
artifact written by `_validate` in runtime.py. Diff = `git diff --stat + selective diff`
on `node.owned_files` relative to `_node_base`. Bounded to 8KB. `redact_text` applied.
Hook point: immediately after verification passes, before `_set(run, NodeState.VALIDATED)`.

**Agreed (operator, 2026-11-23), D3:** Shared core contents and size budget.
**Chosen: B + 1500-token cap** — goal (from hydration parameter, `source_uri=urn:verdict:run:{run_dir.name}:goal`) + constraints text if available (from hydration parameter; source_uri `urn:verdict:run:{run_dir.name}:constraints`; if absent, include SHA only with omission note) + ADR/decision ids from `node.required_context`. Stored as `ContextUnit(key="required:shared-core", slot_type="instructions")`.

**Agreed (operator, 2026-11-23), D4:** Citation and SHA binding for forwarded items.
**Chosen: A** — store upstream items as `ContextUnit` objects with
`source_uri="urn:verdict:run:{run_id}:{pred_id}:validated"` and
`source_digest=sha256(output_text)`. The `reuse_research_pack` stale-check only
validates `research.repo_revision` (the dependent's own SHA); `source_uri` fields are
not checked. No collision path exists. No new dataclass needed.

**Agreed (operator, 2026-11-23), D5:** Token budget overflow handling.
**Agreed (operator, 2026-11-23): drop retrieval first.** Priority-based drop through ContextPackCompiler. Shared core items use the `required:shared-core` key and are never dropped. Forwarded items use `upstream:{pred_id}` and are kept ahead of native retrieval units: native retrieval units drop first, and upstream units drop only when no retrieval unit is left. Omissions are recorded in `WorkerContext.omissions`. The research draft recommended the opposite order. The operator chose to keep verified upstream results because they carry the interfaces a dependent node must use.

**Agreed (operator, 2026-11-23), D6:** Non-.py code provider scope.
**Chosen: A** — out of scope. Bench output must include `"non_python_code_facts_absent": true`.

**Agreed (operator, 2026-11-23), D7:** Bench design for DoD gate.
**Chosen: A** — extend `run_context_lift` to a two-node synthetic shape:
Node A writes the planted token as its verified output; Node B must recall it.
Arm B (treatment): Node B gets forwarded pack. Arm C (control): same budget, padded without forwarded content.
Run at two `max_context_bytes` values: 4000 tokens (small) and 8000 tokens (large).
Routes: `_FREE_COST_CLASSES` only. Stop rule committed before first run.
Evidence: `$VERDICT_HOME/context-lift/ctx-forwarding-{stamp}.json`.
Full task-based bench (real closed-story DAGs) deferred to RS1 (#1246).

**Agreed (operator, 2026-11-23), D8:** Full output storage for verified nodes.
**Chosen: B** — write `run_dir/results/{node_id}.json` at VALIDATED with
`{"output": terminal.output[:32768], "git_head": run.commit, ...}` with `redact_text` applied.
The existing `_save_attempt` tail artifact is unchanged (no breaking change).

## Risks / Trade-offs

**R1: Stale upstream results on resume.** A predecessor's results artifact may be absent
if the run is resumed on a different worktree. Mitigation: absent artifact → omission note,
not a blocked node. Source digest binding detects tampering.

**R2: Context bloat for small free models.** Free models typically have 8–32K context windows.
1500-token shared core + 2000-char predecessor output leaves <10K tokens for the worker's
own task on a 16K model. Mitigation: D3's 1500-token cap; D5's priority-based drop; bench
at two pack sizes (4K and 8K) measures the effect.

**R3: Security of research output in results artifacts.** `redact_text` must be applied
before writing `run_dir/results/{node_id}.json`. The ContextPackCompiler's
`_secret_assignment_re` guard applies to all ContextUnit content; the forwarding code
must not bypass it. The `run_dir/` tree already carries per-node attempt evidence with
the same redaction contract.

**R4: Contradicted upstream results.** This change does not implement contradiction
withdrawal (PK1 scope). A dependent node may receive a forwarded result that a later
upstream correction supersedes. This is acceptable for the bench gate; full withdrawal
is PK1's concern.

## Security / trust implications

Forwarded items are drawn from the run's own `run_dir/results/` artifacts, which are
produced by Verdict's own verification harness under the same redaction pipeline as
`_save_attempt`. They are not drawn from model output verbatim — they pass through
`redact_text` before being written and through the ContextPackCompiler's secret guard
before reaching the worker prompt. The `source_uri` namespace (`urn:verdict:run:`)
is distinct from any gateway URL, making injection via source URI impractical. Workers
already receive internal repository source under a citation contract; this extends that
contract to predecessor outputs. Unknown labels (e.g. `upstream:` from an unknown node_id)
are admitted only if the ContextPlan allows them; a plan mismatch is an explicit unknown,
consistent with existing admission invariants.

## Routing / context / memory implications

Forwarding is entirely advisory (D9 principle). Forwarded items do not change admission,
ranking, the execution-path authority, or any gate. They are added to the ContextPack
under `slot_type="evidence"`, the same slot as other advisory context. The pack budget
is controlled by the selected route's `ContextPlan` (via `bod-303-selected-context-bridge`),
so the same context-window guard applies. Memory is not changed by this feature; Hindsight
is not written or read as part of forwarding.

## Migration / rollback

No schema changes to `events.jsonl` or `receipt.json`. New files in `run_dir/results/`
are written only by this change and are silently absent in older runs (no migration needed).
`hydrate_with_research_pack` checks for the results artifact before reading; absent → omission.
Rollback: revert the results-write in `_validate` and the forwarding in
`hydrate_with_research_pack`; older runs remain valid; newer run dirs may contain unused
`results/` artifacts (harmless).

## ADR impact

No new ADR needed. The change follows ADR-036 (goal-to-receipt) receipt projection conventions
(results artifacts are durable evidence, not events), ADR-012 (consented-budgeted probes)
for the bench timer (free routes only, no paid route unless `allow_priced` is set explicitly
by the operator), and ADR-050 (Hindsight is the memory of record; this change does not
write to memory). BOD-342 D9 (research once, workers reuse) and D10 (context autopilot)
are the motivating decisions.
