# Design

## Context

Production planning uses `plan_with_failover` → strongest-first ordering (ADR-043).
This story does not touch that path. It adds a shadow benchmark that can collect
evidence for a future FU2 story that promotes fusion planning to production.

OmniRoute's `fusion` strategy is out because it uses `combo/*` routes, which are
opaque and never admitted (AGENTS.md invariant `test_opaque_routes_are_never_admitted`).
Verdict-native fusion runs each panel call through the normal `EligibilityLadder`,
so every admission invariant is preserved and every call is auditable.

## Goals / Non-Goals

**Goals:**

- `verdict bench fusion-plan` runs a panel of N (default 3, min 2) admitted FREE
  routes from different model families. Each produces a plan via
  `build_planning_prompt` + `parse_plan`.
- A synthesiser route receives the panel plans anonymised as "Plan 1", "Plan 2",
  etc. and writes one plan. Two synthesiser arms are benchmarked:
  - **(a)** highest-rated admitted planner route (FREE or SUBSCRIPTION).
  - **(b)** a strong FREE route from a different family than the panel.
- Baselines: `top_planner` (single highest-rated planner, FREE or SUBSCRIPTION)
  and `best_panel` (panel plan with the highest structural score, deterministic).
- Structural score (0.0–1.0): disjoint file ownership (0.35), single-file nodes
  (0.25), verify_quality (0.25), node count in ideal range 2–6 inclusive (0.15).
- Per-run caps: `max_calls` (default 10) and `max_tokens` (default 500,000);
  cap check runs before each call; exceeding the cap records `cap_exceeded`.
- Output: one JSON file per goal/arm under `$VERDICT_HOME/fusion-bench/`;
  summary table to stdout.

**Non-Goals:**

- Promote fusion planning to production (FU2 story).
- Change `plan_with_failover`, `PLANNER_REQUIREMENTS`, or ADR-043 ordering.
- Make live provider calls in CI.

## Decisions

1. **`verdict/fusion_plan_bench.py`** — core benchmark module. Production caller
   is `_cmd_bench_fusion_plan` in `verdict/commands/dispatch.py`.
2. **Spend guard** — `CapacityClass` from `verdict/orchestration/contracts.py`:
   `_FREE_ONLY = {CapacityClass.FREE}` for panel members and synth_b;
   `_FREE_OR_SUB = {CapacityClass.FREE, CapacityClass.SUBSCRIPTION}` for synth_a
   and top_planner baseline.
3. **Panel diversity** — `TaskRequirements.exclude_families` accumulates across
   panel iterations so that each panel member comes from a different model family.
4. **Anonymisation** — plans are `WorkGraph` objects whose `to_dict()` output
   contains no route IDs, so anonymisation is automatic. The synthesis prompt
   labels them "Plan 1", "Plan 2", etc.
5. **Synthesis validation** — `parse_plan` must accept the synthesised text;
   an invalid result is recorded with `plan_valid=False` and an error; it is
   never silently replaced.
6. **Admission** — each panel call uses `selector.select(requirements, now)`;
   if the admitted set is empty the benchmark records `panel_too_small` for that
   arm and skips synthesis.
7. **Goals fixture** — `benchmarks/fixtures/fusion-plan-bench-goals.json` provides
   the default goals. No host paths in fixture (vgate host-path guard).
8. **Parser** — `fusion-plan` subparser added to `bench_sub` in
   `verdict/commands/parsers_autodev.py` after `context-lift`.
9. **Cap enforcement** — cap check (`call_count < max_calls` and
   `token_count < max_tokens`) runs before each call. After cap, the arm records
   `cap_exceeded` for any remaining uncompleted call.

## Routing / context / memory implications

No routing changes. The benchmark uses the existing `selector.select` path from
`verdict/orchestration/runtime.py` and does not touch routing or context-packing.
No memory writes: the benchmark output is written to `$VERDICT_HOME/fusion-bench/`
and not to the ai-memory store. Advisory signals and decision signals are not
invoked.

## ADR impact

- **ADR-047** (new): documents the Verdict-native fusion planning benchmark,
  the spend rules, and the structural score. Relates to ADR-043 (planner ordering
  unchanged), ADR-036 (goal-to-receipt), ADR-0001 (control-plane invariants).
- **ADR-043**: planner uses strongest-first ordering for production; unaffected.
- **ADR-0001 invariants**: opaque routes never admitted, admission only narrows —
  both preserved; each panel call is admitted individually.

## Migration / rollback

No production path changes; rollback is simply reverting the branch. The benchmark
writes output only to `$VERDICT_HOME/fusion-bench/`, which is a new directory;
removing it reverts all disk state. No schema migration, no service changes, no
timer installed by this story (the live acceptance run is a post-merge task).

## Risks / Trade-offs

- If fewer than `min_panel_size` distinct FREE families exist, the benchmark
  records `panel_too_small` and skips synthesis — it does not fail the process.
- The structural score is a proxy; it cannot predict execution pass rate. That
  measurement requires the live acceptance run (post-merge).
- `top_planner` and synth_a admit SUBSCRIPTION routes; they may invoke a paid
  model when no FREE planner with sufficient rating exists.

## Security / trust implications

- Panel members never see each other's route IDs; anonymisation is structural.
- No secrets are written to output files; `_RECEIPT_FIELDS` allowlist guards disk output.
- No live provider calls in tests; `tests/conftest.py` strips credentials.
