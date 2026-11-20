# ADR-047: Verdict-native fusion planning benchmark

**Status:** Accepted (operator, 2026-11-20)
**Date:** 2026-11-20
**Deciders:** Operator
**Relates to:** ADR-043 (role-dependent selection order), ADR-036 (goal-to-receipt),
ADR-0001 (control-plane invariants)

## Context

The operator asked whether a panel of lesser (cheaper, free-tier) models could
collectively match a single frontier planner's decomposition quality. OmniRoute
provides its own `fusion` strategy, but three constraints rule it out for Verdict
(operator, 2026-11-20):

1. OmniRoute `fusion` is a `combo/*` route. Opaque routes are never candidates
   (AGENTS.md invariant; `tests/test_admission.py::test_opaque_routes_are_never_admitted`).
2. A request that carries tools skips the panel inside OmniRoute and goes to one
   model, so it cannot help agentic worker nodes.
3. OmniRoute's judge synthesises prose; Verdict's objective judge is the node's
   verification command.

The plan (`docs/plan/20261120-learning-loop/plan.md`, story FU1) calls for a
benchmark, not an immediate production path change. Production planning stays
strongest-first (ADR-043) until benchmarked evidence says otherwise.

## Decision

Build a **Verdict-native fusion planning benchmark** (`verdict bench fusion-plan`,
`verdict/fusion_plan_bench.py`) that:

### Panel

For each recorded goal, N admitted FREE routes from different model families
(default N=3, minimum 2) each produce a plan via the existing
`build_planning_prompt` + `parse_plan` path. Routes are admitted individually
by the normal `EligibilityLadder`, preserving every admission invariant.

### Synthesis

A synthesizer route receives the panel plans, anonymised as "Plan 1", "Plan 2",
etc. (no route or model names reach the prompt). It writes one plan from the
agreements across plans, the gaps any single plan caught, and the conflicts it
resolves. The existing `parse_plan` validator must accept the result; an invalid
synthesized plan is recorded as invalid and never silently replaced.

### Synthesizer options benched

- **(a)** Highest-rated admitted planner route (FREE or SUBSCRIPTION).
- **(b)** A strong FREE route, different family from the panel.

### Baselines

- **top_planner**: single highest-rated admitted planner (FREE or SUBSCRIPTION),
  the same call `plan_with_failover` makes in production.
- **best_panel**: the panel plan with the highest structural score (deterministic,
  pure function).

### Structural score

A deterministic 0.0–1.0 score over a `WorkGraph`:
- **disjoint** (0.35): fraction of implement nodes whose owned files do not
  overlap any other implement node.
- **single_file** (0.25): fraction of implement nodes with exactly one owned file.
- **verify_quality** (0.25): fraction of implement nodes with a non-empty
  verification command that references a test file.
- **count** (0.15): ideal 2–6 total nodes; penalised outside that range.

### Spend rules

- Panel members and synthesizer option (b): `CapacityClass.FREE` only. Fail
  closed on METERED, UNKNOWN, or SUBSCRIPTION.
- Synthesizer option (a) and top-planner baseline: FREE or SUBSCRIPTION.
- Never METERED or UNKNOWN capacity. Never `combo/*` or `auto/*` routes.
- Per-run caps: `max_calls` (default 10) and `max_tokens` (default 500,000).
  The cap check runs before each call; exceeding the cap records `cap_exceeded`.

### Output

One JSON file per goal per arm under `$VERDICT_HOME/fusion-bench/`, plus a
summary table. The fields written to disk are restricted to an explicit allowlist
(`_RECEIPT_FIELDS`) to prevent internal state from leaking.

### Not changed

`plan_with_failover`, `PLANNER_REQUIREMENTS`, and the production planning path
are unchanged. ADR-043's role-dependent ordering holds. The benchmark is
additive only.

## Consequences

- The benchmark produces evidence to decide whether to promote fusion planning
  from shadow to production (a future FU2 story).
- Running the benchmark live is a post-merge task; no live provider calls are
  made in CI.
- If fused plans match the top planner on executed pass rate, planning can
  move down a tier. If not, production is unaffected.
- Each panel call is bounded and recorded individually, keeping the admission
  audit trail intact.
