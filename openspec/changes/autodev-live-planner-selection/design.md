# Design

## Context

`cmd_autodev` uses the selected route from an execution-path decision when one exists. Without one (a dry run), it used `--orchestrator-model` or `DEFAULT_ORCHESTRATOR_MODEL` (which was `mistral/codestral-latest`, a paid route). The `_live_planner_models` function in `verdict/autodev_run.py` already picks planners from the live eligibility ladder (reasoning, coding, frontier_worthy, free or subscription capacity, two families), but the dry-run path did not use it. BOD-342 D22 excluded `cc/*` and `cx/*` routes from planning and execution; operator ruling 2026-11-17 (Linear BOD-342) lifts D22 fully for `cc/*` and `cx/*` routes through normal admission; pay-per-token stays excluded.

## Goals / Non-Goals

**Goals:**
- Dry run with no decision selects its planner from the live eligibility ladder using `_live_planner_models` (same as live path).
- Explicit `--orchestrator-model` is honoured but must pass admission; paid/inadmissible models are refused with a named error.
- `cc/*` and `cx/*` routes are eligible for planning and execution through admission (D22 lifted).
- If the ladder offers no eligible planner, fail closed with a named error (no send).
- No pay-per-token route is ever selected by the ladder.

**Non-Goals:**
- Change the live path (`prepare_live_autodev_selected`) which already uses the ladder correctly.
- Change the eligibility ladder logic itself.

## Decisions

1. **Remove `DEFAULT_ORCHESTRATOR_MODEL` constant** from `verdict/decomposer.py` — it was the hardcoded fallback causing HTTP 402.
2. **Wire dry-run path to `_live_planner_models`** in `verdict/cli.py` — when no execution-path decision and no `--orchestrator-model`, call the live planner selection to get eligible planners, then use the first one for `DecompositionConfig`.
3. **Remove `cc/*` planner refusal** in `Decomposer.decompose` (lines 161-165).
4. **Remove `_PLANNER_EXCLUDED_POOLS`** and all its uses in `verdict/autodev_run.py` — the ladder's `exclude_pools` parameter should not exclude `claude`/`codex` pools anymore.
5. **Update `_live_planner_models`** to not exclude `claude`/`codex` pools — remove the `exclude_pools=_PLANNER_EXCLUDED_POOLS` from `TaskRequirements` and the `pool_of(route_id) in _PLANNER_EXCLUDED_POOLS` checks.
6. **`DecompositionConfig.model` default** — change to `None` (or remove default), requiring explicit model or ladder selection.
7. **Explicit `--orchestrator-model`** — validate it through admission before use; if inadmissible (paid, unknown, etc.), refuse with named error.

## Risks / Trade-offs

- The ladder may return no eligible planners (e.g., no free/subscription routes with reasoning+coding+frontier_worthy from two families). In this case, autodev fails closed with a clear error — this is correct behavior (fail closed, no send).
- The ladder selection requires a live gateway; dry runs now need gateway connectivity. This is acceptable since the alternative was a paid route fallback.
- Explicit `--orchestrator-model` validation adds admission overhead but prevents accidental paid route usage.

## Security / trust implications

- No pay-per-token route can be selected by the ladder (capacity filter: only FREE or SUBSCRIPTION).
- Explicit paid routes are refused by admission check.
- Subscription routes (`claude`, `codex`) are now admitted through normal admission (D22 lifted per operator ruling).

## Routing / context / memory implications

- Dry-run planner selection now goes through the live eligibility ladder, same as live path.
- Admission, ranking unchanged — only the pool exclusion is removed.

## Migration / rollback

- No data migration. Roll back by reverting the constant removal, re-adding the `cc/*` refusal, and re-adding `_PLANNER_EXCLUDED_POOLS`.
- Operators who want a specific planner can still pass `--orchestrator-model`.

## ADR impact

- ADR-001 (control plane invariants): admission before ranking still holds; the ladder selection is the admission step.
- Operator ruling 2026-11-17 (Linear BOD-342) documented: D22 lifted for `cc/*`/`cx/*` through admission.