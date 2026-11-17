# Proposal: autodev plans on a subscription route by default

## Why

`verdict autodev --objective ... --dry-run` failed in planning with HTTP 402 Payment Required. With no execution-path decision, `verdict/cli.py` fell back to `verdict.decomposer.DEFAULT_ORCHESTRATOR_MODEL`, which was `mistral/codestral-latest`, a paid route. Operator ruling 2026-11-17: the planner default is the subscription route `cc/claude-sonnet-5-5`, never a paid route.

## What Changes

- `DEFAULT_ORCHESTRATOR_MODEL` becomes `cc/claude-sonnet-5-5`. It is the default of `DecompositionConfig.model` and the dry-run fallback in `cmd_autodev`.
- An explicit `--orchestrator-model` still wins.
- The live path, which picks planners through `_live_planner_models`, is unchanged.

## Capabilities

### New Capabilities

- `autodev-planner-default`: when no execution-path decision supplies the planner route, autodev plans on the subscription route `cc/claude-sonnet-5-5`, never on a paid route.

### Modified Capabilities

## Impact

- `verdict/decomposer.py` constant only.
- Tests in `tests/test_autodev_wiring.py` and `tests/test_decomposer.py`.
- No API, dependency or install change.

## Evidence boundary

KNOWN: the dry run fell back to `mistral/codestral-latest` and got HTTP 402. NOT AVAILABLE before merge: a dry run on the operator setup from merged main. That check is the post-merge task.

## Acceptance criteria

- With no execution-path decision and no `--orchestrator-model`, the dry-run planner is configured with `cc/claude-sonnet-5-5`.
- An explicit `--orchestrator-model` overrides the default.
- The default is not a pay-per-token route such as `mistral/codestral-latest`.
