# Proposal: FU1 — fusion planning benchmark

## Why

The operator asked whether a panel of cheaper (free-tier) models could collectively
match a single frontier planner's decomposition quality. OmniRoute's `fusion`
strategy is off-limits because `combo/*` routes are opaque and are never admitted
(AGENTS.md invariant `test_opaque_routes_are_never_admitted`). This story adds a
shadow benchmark that collects evidence for a future FU2 story that promotes
Verdict-native fusion planning to production.

## What Changes

- Add `verdict bench fusion-plan` subcommand (`verdict/fusion_plan_bench.py`).
- Panel: N admitted FREE routes from different model families (default N=3, min 2)
  each produce a plan via `build_planning_prompt` + `parse_plan`.
- Synthesis: a synthesiser route receives the panel plans anonymised as "Plan 1",
  "Plan 2", etc. Two synthesiser arms are benchmarked:
  - **(a)** highest-rated admitted planner (FREE or SUBSCRIPTION).
  - **(b)** a strong FREE route from a different family than the panel.
- Baselines: `top_planner` (single highest-rated planner, FREE or SUBSCRIPTION)
  and `best_panel` (best panel plan by deterministic structural score).
- Output: one JSON file per goal/arm under `$VERDICT_HOME/fusion-bench/`;
  summary table to stdout.

## Capabilities

### New Capabilities

- `fusion-plan-bench`: `verdict bench fusion-plan` runs a panel of admitted FREE
  planners from different model families, synthesises the plans via an admitted
  route, and benchmarks the result against a single-planner baseline; every call
  is admitted individually by the normal `EligibilityLadder`; panel members and
  synthesiser option (b) require FREE capacity; synthesiser option (a) and the
  top-planner baseline may use SUBSCRIPTION; METERED and UNKNOWN capacity are
  never used; the benchmark records a JSON arm per goal under
  `$VERDICT_HOME/fusion-bench/` and prints a summary table.

### Modified Capabilities

None. The production planning path (`plan_with_failover`, `PLANNER_REQUIREMENTS`,
ADR-043 strongest-first ordering) is unchanged.

## Impact

- `verdict/fusion_plan_bench.py` (new): core benchmark module.
- `verdict/commands/parsers_autodev.py`: add `fusion-plan` subparser under `bench`.
- `verdict/commands/dispatch.py`: add `_cmd_bench_fusion_plan` handler and dispatch.
- `benchmarks/fixtures/fusion-plan-bench-goals.json` (new): default goals fixture.
- `tests/test_fusion_plan_bench.py` (new): 27 tests, no live calls.
- `docs/adr/ADR-047-verdict-native-fusion-planning.md` (new).
- `docs/adr/README.md`: add ADR-047 entry.
- No changes to production planning, admission, or orchestration runtime.

## Evidence boundary

KNOWN: `build_planning_prompt` + `parse_plan` is the production planning path and
can be reused. KNOWN: `CapacityClass.FREE` and `CapacityClass.SUBSCRIPTION` are the
only admitted capacity classes (never METERED/UNKNOWN). KNOWN: `combo/*` and
`auto/*` routes are invariantly excluded. INFERRED: a panel of free-tier models
may collectively catch different decomposition gaps than any one model; this
remains to be measured. NOT AVAILABLE before merge: live acceptance run on the
operator setup showing whether fused plans match or exceed the top-planner baseline.
That measurement is the post-merge task.

## Acceptance criteria

- `verdict bench fusion-plan --dry-run` exits 0 and prints a plan-only summary.
- With a live gateway, panel members are FREE routes from distinct model families.
- Synthesiser option (a) is FREE or SUBSCRIPTION; option (b) is FREE only.
- If fewer than 2 FREE families are admitted, the benchmark records `panel_too_small`
  and exits 0 (not an error).
- The synthesis prompt contains no route IDs; plans are labelled "Plan 1", etc.
- An invalid synthesised plan is recorded with `plan_valid=False`; no silent fallback.
- Per-run caps (`max_calls`, `max_tokens`) halt the bench and record `cap_exceeded`.
- Output files contain only the `_RECEIPT_FIELDS` allowlist fields.
