# Proposal: autodev chooses its planner from the live eligibility ladder

## Why

`verdict autodev --objective ... --dry-run` failed in planning with HTTP 402 Payment Required. With no execution-path decision, `verdict/cli.py` fell back to `verdict.decomposer.DEFAULT_ORCHESTRATOR_MODEL`, which was `mistral/codestral-latest`, a paid route. The `_live_planner_models` function in `verdict/autodev_run.py` already picks planners from the live eligibility ladder (reasoning, coding, frontier_worthy, free or subscription capacity, two families), but the dry-run path did not use it. Additionally, BOD-342 D22 excluded `cc/*` and `cx/*` routes from planning and execution; operator ruling 2026-11-17 (Linear BOD-342) lifts D22 fully: `cc/*` and `cx/*` routes may be used for launches, planner fallbacks and reviews through the normal admission ladder; pay-per-token stays excluded.

## What Changes

- The dry-run path (and any path without an execution-path decision) now selects its planner from the live eligibility ladder using `_live_planner_models`, the same selection as the live path. It never falls back to a fixed model and never to a paid or pay-per-token route. If the ladder offers no eligible planner, it fails closed with a named error (no send).
- An explicit `--orchestrator-model` is still honoured but must pass admission (a paid or inadmissible one is refused with a named error).
- Subscription pools (`claude`, `codex`) are now eligible for planning and execution through admission (D22 lifted, cite operator decision recorded on Linear BOD-342, 2026-11-17).
- The `DEFAULT_ORCHESTRATOR_MODEL` constant and its use as a fallback are removed.
- The `cc/*` planner refusal in `Decomposer.decompose` and `_PLANNER_EXCLUDED_POOLS` are removed.

## Capabilities

### New Capabilities

- `autodev-live-planner-selection`: when no execution-path decision supplies the planner, autodev (including `--dry-run`) selects its planner from the live eligibility ladder using the same selection as the live path; it never falls back to a fixed model and never to a paid or pay-per-token route; if the ladder offers no eligible planner, it fails closed with a named error (no send).

### Modified Capabilities

- `autodev-planner-default` (replaced): the old fixed subscription default `cc/claude-sonnet-5-5` is replaced by live ladder selection.
- Subscription pools (`claude`, `codex`) are now admitted for planning and execution.

## Impact

- `verdict/decomposer.py`: remove `DEFAULT_ORCHESTRATOR_MODEL` constant, remove `cc/*` planner refusal, update `DecompositionConfig` default.
- `verdict/cli.py`: wire dry-run path to live planner selection via `_live_planner_models`.
- `verdict/autodev_run.py`: remove `_PLANNER_EXCLUDED_POOLS` and its uses; `_live_planner_models` no longer excludes `claude`/`codex` pools.
- Tests in `tests/test_decomposer.py`, `tests/test_autodev_wiring.py`, `tests/test_orch_planner.py`, `tests/test_orch_review.py`, `tests/test_orch_runtime.py`, `tests/test_orchestration_eligibility_output.py` (only those asserting D22/cc exclusion).
- Docs mentioning the old default or D22.

## Evidence boundary

KNOWN: the dry run fell back to `mistral/codestral-latest` and got HTTP 402. KNOWN: `_live_planner_models` already implements live ladder selection. KNOWN: D22 exclusion of `cc/*`/`cx/*` is enforced in `Decomposer.decompose` and `_PLANNER_EXCLUDED_POOLS`. OPERATOR RULING 2026-11-17 (Linear BOD-342): D22 lifted fully for `cc/*` and `cx/*` routes through normal admission; pay-per-token stays excluded. NOT AVAILABLE before merge: a dry run on the operator setup from merged main. That check is the post-merge task.

## Acceptance criteria

- With no execution-path decision and no `--orchestrator-model`, the dry-run planner is selected from the live eligibility ladder (free or subscription, reasoning+coding+frontier_worthy, at least two families).
- An explicit `--orchestrator-model` overrides the ladder selection but must pass admission; a paid or inadmissible model is refused with a named error.
- The `cc/*` planner refusal is removed; a `cc/*` route can be chosen by the ladder.
- `_PLANNER_EXCLUDED_POOLS` is removed; `claude`/`codex` pools are eligible.
- If the ladder offers no eligible planner, autodev fails closed with a named error and sends nothing.
- No pay-per-token route is ever selected by the ladder.