# Design

## Context

`cmd_autodev` uses the selected route from an execution-path decision when one exists. Without one (a dry run), it uses `--orchestrator-model` or `DEFAULT_ORCHESTRATOR_MODEL`. That constant was a paid route.

## Goals / Non-Goals

**Goals:** a dry run with no decision plans on `cc/claude-sonnet-5-5`; `--orchestrator-model` still overrides it.

**Non-Goals:** change `_live_planner_models` or the live decision path.

## Decisions

Change the constant, not the CLI fallback line. The constant is the single default for both `DecompositionConfig` and the CLI, so one edit covers both. `cc/claude-sonnet-5-5` is the operator's chosen subscription route.

## Risks / Trade-offs

The subscription route may be unavailable on a given setup. The dry run then fails with that route's error instead of a 402, and the operator can pass `--orchestrator-model`.

## Security / trust implications

None new. The default route is a subscription route, so a dry run can no longer charge pay-per-token spend without the operator naming that route. No credential handling changes.

## Routing / context / memory implications

Only the default planner route with no decision changes. Admission, ranking and the live `_live_planner_models` selection are unchanged.

## Migration / rollback

No migration. Roll back by reverting the constant. Operators who want another planner pass `--orchestrator-model`.

## ADR impact

None. This sets a default value and changes no architecture.
