# Tasks

## 1. Verified output storage at VALIDATED

Write `run_dir/results/{node_id}.json` and `run_dir/results/{node_id}-diff.patch` at the
moment a node is validated, before `_set(run, NodeState.VALIDATED)` fires.

- [ ] 1.1 `verdict/orchestration/runtime.py` (`_validate`): after verification passes, capture
  `git diff --stat` + selective diff on `node.owned_files` relative to `_node_base`; write
  `run_dir/results/{node_id}-diff.patch` (bounded to 8KB, `redact_text` applied).
 
- [ ] 1.2 `verdict/orchestration/runtime.py` (`_validate`): write `run_dir/results/{node_id}.json`
  with `{"output": terminal.output[:32768], "git_head": run.commit, "node_id": ...,
  "validated_at": iso_now()}` with `redact_text` applied.
 
- [ ] 1.3 Write focused unit tests: `tests/test_orch_runtime.py` — a VALIDATED node produces
  `run_dir/results/{node_id}.json`; content is redacted; diff artifact is bounded to 8KB.
  Verify: `uv run python -m pytest -q tests/test_orch_runtime.py -k test_results_artifact`

## 2. Upstream forwarding in hydration

Extend `hydrate_with_research_pack` to load predecessor results and inject them as ContextUnit
objects with source URIs.

- [ ] 2.1 `verdict/orchestration/run.py` (`hydrate_with_research_pack`): for each `node.depends_on`
  predecessor, attempt to load `run_dir/results/{pred_id}.json`. If absent or unreadable,
  emit an omission note and continue.
- [ ] 2.2 For each loaded predecessor, build a `ContextUnit(key="upstream:{pred_id}",
  slot_type="evidence", source_uri="urn:verdict:run:{run_id}:{pred_id}:validated",
  source_digest=sha256(output_text), content=<output or diff>)`.
  Research predecessors use full output (capped 32KB). Implement predecessors use the diff patch.
 
- [ ] 2.3 Build the shared core `ContextUnit(key="required:shared-core", slot_type="instructions",
  source_uri="urn:verdict:run:{run_dir.name}:goal", ...)` from the `goal` parameter, the
  `constraints` parameter if non-empty, and `node.required_context` ADR/decision ids.
  Cap at 1500 tokens.
- [ ] 2.4 Pass all new units alongside the existing ResearchPack units into `project_research_pack`
  via the `ContextPlan` budget. Confirm priority ordering: `required:` > `upstream:` >
  native retrieval units.
- [ ] 2.5 Write focused unit tests: `tests/test_orch_worker_context.py` — dependent node pack
  includes upstream unit; absent predecessor artifact → omission, not block; shared core
  present in every pack; shared core absent when goal is empty; overflow drops native units before
  upstream units; the shared core is never dropped.
  Verify: `uv run python -m pytest -q tests/test_orch_worker_context.py -k test_upstream_forward`

## 3. Lift bench extension

Extend `verdict bench context-lift` to the two-node forwarding shape.

- [ ] 3.1 `verdict/context_lift.py`: add `run_context_lift_forwarding(*, ...)` function. Builds a
  synthetic two-node shape: Node A writes the planted token as its verified output;
  Node B recalls it. Arm B uses forwarded pack; arm C uses same-budget padded pack.
  Runs at two `max_context_bytes` values (4000 and 8000 token equivalents).
  Requires a stop-rule file; refuses to run without it; embeds its hash in evidence JSON.
  Routes: `_FREE_COST_CLASSES` only.
- [ ] 3.2 `verdict/commands/parsers_autodev.py`: add `--forwarding` flag to `bench context-lift`
  subcommand; routes to `run_context_lift_forwarding`.
- [ ] 3.3 Evidence written to `$VERDICT_HOME/context-lift/ctx-forwarding-{stamp}.json` and
  `latest-forwarding.json`. Include `"non_python_code_facts_absent": true`.
- [ ] 3.4 Write unit tests: `tests/test_context_lift.py` — forwarding bench refuses without stop
  rule; produces evidence JSON with both pack sizes; control arm uses same byte budget.
  Verify: `uv run python -m pytest -q tests/test_context_lift.py -k test_forwarding_bench`

## 4. Full validation

Run the complete suite and gates, write the stop rule, run the bench.

- [ ] 4.1 Write the stop rule file before the first bench run; commit it.
  Verify: file exists, hash matches the value embedded in the first bench evidence JSON.
- [ ] 4.2 Run `verdict bench context-lift --forwarding --dry-run`; exits 0.
  Verify: `verdict bench context-lift --forwarding --dry-run`
- [ ] 4.3 Run the full test suite and all gates.
  Verify: `uv run python -m pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy verdict --strict && uv run python scripts/check_reachability.py --ratchet-base origin/main && uv run python scripts/check_lint_gates.py`
- [ ] 4.4 (post-merge) Run `verdict bench context-lift --forwarding` on the production host;
  verify evidence appears under `$VERDICT_HOME/context-lift/`; `verdict doctor` shows
  context-lift timer ok; bench result meets the stop rule.
  Verify: `verdict bench context-lift --forwarding` exits 0 and evidence JSON is written.
