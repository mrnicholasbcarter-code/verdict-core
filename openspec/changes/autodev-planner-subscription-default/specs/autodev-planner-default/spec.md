# Spec Delta

## Purpose

Autodev plans on a subscription route when no execution-path decision supplies the planner, and never defaults to a paid route.

## ADDED Requirements

### Requirement: Autodev SHALL default its planner to a subscription route
When no execution-path decision supplies the planner route (a dry run, or no decision), autodev SHALL plan on the subscription route `cc/claude-sonnet-5-5`. The default SHALL NOT be a paid or pay-per-token route. An explicit `--orchestrator-model` SHALL take precedence over the default.

#### Scenario: Dry run with no decision
- **WHEN** `verdict autodev --objective <text> --dry-run` runs with no execution-path decision and no `--orchestrator-model`
- **THEN** the planner is configured with `cc/claude-sonnet-5-5`

#### Scenario: Explicit override
- **WHEN** the dry run is given `--orchestrator-model other/model`
- **THEN** the planner is configured with `other/model`

#### Scenario: No paid default
- **WHEN** the default planner model is read
- **THEN** it is `cc/claude-sonnet-5-5` and is not a pay-per-token route such as `mistral/codestral-latest`
