# Spec Delta — fusion-plan-bench

## Purpose

`verdict bench fusion-plan` benchmarks a panel-plus-synthesis planning strategy
against a single-planner baseline. It never changes the production planning path.

## ADDED Requirements

### Requirement: verdict bench fusion-plan SHALL run a panel of admitted FREE planners

`verdict bench fusion-plan` SHALL select N admitted FREE routes (default N=3,
minimum 2) from different model families. Each route SHALL be admitted
individually via `selector.select(requirements, now)`. If fewer than the minimum
number of distinct FREE model families can be admitted, the benchmark SHALL record
`panel_too_small` for that goal and skip synthesis. It SHALL NOT use `combo/*` or
`auto/*` routes.

#### Scenario: Panel with enough distinct FREE families
- **WHEN** `verdict bench fusion-plan` runs with `--panel-size 3` and the
  eligibility ladder returns at least three admitted FREE routes from at least
  three different model families
- **THEN** each of the three routes produces a plan via `build_planning_prompt` +
  `parse_plan`; failures are recorded per-route and do not stop the bench

#### Scenario: Panel too small
- **WHEN** fewer than 2 distinct admitted FREE model families are available
- **THEN** the benchmark records `panel_too_small` for that goal; synthesis is
  skipped; the process exits 0

#### Scenario: Opaque routes excluded
- **WHEN** the catalog contains `combo/*` or `auto/*` routes
- **THEN** those routes are never selected as panel members or synthesisers

### Requirement: Panel plans SHALL be anonymised before synthesis

The synthesis prompt SHALL label panel plans "Plan 1", "Plan 2", etc. with no
route IDs, model names, provider names, or any identity information.

#### Scenario: Synthesis prompt contains no route IDs
- **WHEN** the synthesis prompt is assembled from panel plans
- **THEN** it contains none of the original route IDs; plans are labelled
  "Plan 1", "Plan 2", etc.

### Requirement: Synthesiser SHALL be benched on two arms

The benchmark SHALL bench two synthesiser arms. Arm `synth_a`: the highest-rated
admitted planner (FREE or SUBSCRIPTION). Arm `synth_b`: a strong FREE route from
a different model family than any panel member.

#### Scenario: synth_a selects highest-rated FREE or SUBSCRIPTION planner
- **WHEN** `synth_a` selects its synthesiser
- **THEN** the route has `CapacityClass.FREE` or `CapacityClass.SUBSCRIPTION`;
  METERED and UNKNOWN routes are never selected

#### Scenario: synth_b selects a FREE route outside the panel families
- **WHEN** `synth_b` selects its synthesiser
- **THEN** the route has `CapacityClass.FREE`; it comes from a family not
  already in the panel; SUBSCRIPTION, METERED, and UNKNOWN routes are not used

### Requirement: Synthesised plan SHALL pass the planner validator

After synthesis, `parse_plan` SHALL be called on the result. If it raises
`OrchestrationError`, the arm SHALL record `plan_valid=False` and the error
message. The benchmark SHALL NOT silently fall back to a panel plan.

#### Scenario: Valid synthesised plan
- **WHEN** the synthesiser returns text that `parse_plan` accepts
- **THEN** the arm records `plan_valid=True` and the parsed `WorkGraph`

#### Scenario: Invalid synthesised plan
- **WHEN** the synthesiser returns text that `parse_plan` rejects
- **THEN** the arm records `plan_valid=False` and the parse error; no fallback
  to a panel plan occurs

### Requirement: Per-run call and token caps SHALL be enforced

The benchmark SHALL check `call_count < max_calls` and `token_count < max_tokens`
before each model call. When either cap is reached, the remaining uncompleted
calls for that arm SHALL record `cap_exceeded`. The benchmark SHALL NOT block
or hang after cap exhaustion.

#### Scenario: Call cap reached
- **WHEN** `call_count` reaches `max_calls` before the panel is complete
- **THEN** remaining panel calls record `cap_exceeded`; synthesis is skipped
  for that goal; the benchmark continues to the next goal

#### Scenario: Token cap reached
- **WHEN** cumulative `input_tokens + output_tokens` reaches `max_tokens` before
  the panel is complete
- **THEN** remaining panel calls record `cap_exceeded`

### Requirement: Structural score SHALL be a deterministic pure function

`structural_score(graph: WorkGraph) -> float` SHALL return a value in [0.0, 1.0]
computed from the graph's topology only. The same `WorkGraph` SHALL always produce
the same score.

Weights: disjoint file ownership 0.35, single-file implement nodes 0.25,
verify_quality (non-empty verification command referencing a test file) 0.25,
node count in ideal range 2–6 inclusive 0.15.

#### Scenario: Score is deterministic
- **WHEN** `structural_score` is called twice with the same `WorkGraph`
- **THEN** both calls return the same float

### Requirement: Output files SHALL use an explicit field allowlist

Fields written to `$VERDICT_HOME/fusion-bench/*.json` SHALL be restricted to the
`_RECEIPT_FIELDS` allowlist. Internal state (route IDs used in calls, raw
executor objects, etc.) SHALL NOT appear in output files.

#### Scenario: Output JSON contains only allowlisted fields
- **WHEN** a benchmark arm completes and writes its result to disk
- **THEN** the JSON file contains only fields in `_RECEIPT_FIELDS`; no route IDs
  from the selector, no executor objects, and no internal tracking state appear
