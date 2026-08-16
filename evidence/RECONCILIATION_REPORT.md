# Post-Implementation Reconciliation

## 1. Working-tree & baseline summary

- **Active checkout**: `/home/nick/dev/verdict-core` — a **non-git** source checkout on
  branch label `feat/ecosystem-stories-20260803` (`git rev-parse --is-inside-work-tree` is
  false; the six-issue files live here, NOT in the sibling git worktrees).
- **Snapshot (recoverable)**: `/tmp/verdict-core-reconciliation-20260803-092541.tar.gz`
  (15.5MB, verified readable) of `verdict/`, `tests/`, `contracts/src/`, `docs/`.
- **Baseline**: `uv run pytest -q` → **1092 passed, 1 failed, 1 warning**.
  - Failure: `test_provider_receipts.py::...hashes_inputs` — pre-existing `_freeze` NameError
    (`verdict/gateway_adapters.py:417`), unrelated to #256–#261.
  - Warning: Starlette `httpx` deprecation (flaky; may read 1090/1091 run-to-run).
  - Recorded at `evidence/RECONCILIATION_BASELINE.md`.

## 2. Issue-by-issue implementation map (#256–#261)

| Issue | Files (implemented) | Tests |
|-------|--------------------|-------|
| #256 MODEL-001 | `verdict/model_passports.py`, `availability.py` (QUARANTINED), `api.py` `/v1/models/*`, `schemas/model-passport.v1.json`, `contracts/src/index.ts` (modelPassport) | `test_model_passports.py` (24), `test_probes.py`, `test_availability.py` |
| #257 CONTEXT-001 | `verdict/context_envelope.py`, `hive_workspace.py`, `mcp_server.py` (code tools) | `test_context_compiler.py` (16), `test_hive_workspace.py` (8) |
| #258 CONT-001 | `verdict/execution_session.py`, `failover_engine.py` | `test_execution_session.py` (7), `test_failover_engine.py` (8) |
| #259 AUTO-001 | `verdict/workflows/autodev.py` (12-stage) | `test_autodev_workflow.py` (14) |
| #260 GOV-001 | `memory_bridge.py` (hooks), `adaptive_ranker.py` (wall), `tests/test_learning_policy_wall.py` | `test_memory_bridge.py`, `test_learning_policy_wall.py` (5) |
| #261 CLI-001 | `verdict/simulator.py`, `cli.py` (simulate/replay/plan/inspect) | `test_simulator.py`, `test_cli.py` (32) |

## 3. Reconciliation table (future requirement → existing issue/ADR → action)

| Future requirement | Existing issue/ADR | Current status | Action |
|-------------------|-------------------|----------------|--------|
| ExecutionStrategy | none (net-new) | — | **create STRAT-001** |
| Execution surfaces | #222 VER-005 (governed Ruflo/agent adapter) | specified only | update/link to STRAT-001 |
| Environment discovery | none (net-new) | — | **create ENV-001** |
| Entitlement/capacity pools | none | — | **create ENV-001** (or fold) |
| Mission journal | #228 (append-only EvidenceChain) | PARTIAL | update #228 / fold into STRAT |
| Replay | #261 `cmd_replay` | implemented (typed events) | keep + extend |
| Configuration snapshots | none | — | **create ENV-001** or JOURNAL |
| Side-effect semantics | **none** (net-new, critical) | — | **create STRAT-001** / small seam in #258 |
| Direct-frontier benchmarks | none | — | **create BENCH-001** |
| Learned strategy ranking | #260 wall + ADR-016 (learning boundary) | satisfy | update #260 → STRAT |
| Cockpit design/replay | COCKPIT-001 (issue) | — | keep (deferred) |

## 4. What to create (minimal, not duplicate)

Only genuinely-missing foundational issues:
1. **STRAT-001** — execution-strategy contract + direct-strategy compatibility + candidate/decision records + strategy outcome + **side-effect/idempotency classification seam** for #258.
2. **ENV-001** — environment snapshot, capability graph, execution surfaces, entitlement/capacity pools, configuration snapshots.
3. **JOURNAL-001** — typed mission journal + config snapshots + replay/analytical contracts.
4. **BENCH-001** — direct-frontier baseline, verified-work-per-capacity, cost/latency/completion/regression/failover comparisons.

Do NOT create STRAT-001/ENV-001/JOURNAL-001/BENCH-001 duplicates of existing #218–#239 work — only the net-new gaps above.

## 5. Recommended publication structure

Six issues are tightly coupled through shared files (`api.py`, `contracts/src/index.ts`,
schemas). Recommend **one integration PR** on a feature branch with:
- logical commits (one per issue #256–#261)
- issue-mapping in the PR body
- test evidence + migration notes + rollback guidance
- remote CI run, review, fix-until-green, then close #256–#261 with evidence + update #262.

Do NOT close #256–#261 until pushed + review complete + CI green + merged.

## 6. Recommendation

**Publish with minimal seams.** Only add:
- a strategy type discriminator on `route` output (so a route can later be a strategy, not just a model id),
- ensure replay events carry `schema_version` (already present),
- an optional side-effect/idempotency seam on `ExecutionSession.StepRecord` (for #258) so durable effects aren't retried,
- reference `STRAT-001`/`ENV-001`/`JOURNAL-001`/`BENCH-001` as future issues.

Do NOT reimplement the six issues. Defer the full strategy compiler / surfaces / pools / benchmark / learning until those new issues are planned.