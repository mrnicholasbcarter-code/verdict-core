# autodev-live-planner-selection Specification

## Purpose
Autodev (including `--dry-run`) chooses its planner from the live eligibility ladder when no execution-path decision supplies the planner. It never falls back to a fixed model and never to a paid or pay-per-token route. If the ladder offers no eligible planner, it fails closed with a named error (no send). Subscription pools (`claude`, `codex`) are eligible through admission (operator ruling 2026-11-17, Linear BOD-342: D22 lifted fully).

## Requirements

### Requirement: Autodev SHALL select its planner from the live eligibility ladder

When no execution-path decision supplies the planner route (a dry run, or no decision), autodev SHALL select its planner by calling `_live_planner_models`, which reads the live eligibility ladder for routes with `reasoning`, `coding`, `frontier_worthy` capabilities, `FREE` or `SUBSCRIPTION` capacity class, from at least two model families. The first eligible route from the ladder SHALL be used as the planner model for `DecompositionConfig`. The ladder selection SHALL be the same as the live path (`prepare_live_autodev_selected`).

#### Scenario: Dry run with no decision and eligible ladder
- **WHEN** `verdict autodev --objective <text> --dry-run` runs with no execution-path decision and no `--orchestrator-model`, and the live ladder has eligible free/subscription planner routes
- **THEN** the planner is configured with the first route returned by `_live_planner_models`

#### Scenario: Dry run with empty ladder
- **WHEN** `verdict autodev --objective <text> --dry-run` runs with no execution-path decision and no `--orchestrator-model`, and the live ladder has no eligible planner routes (fewer than two families, or no FREE/SUBSCRIPTION routes with required capabilities)
- **THEN** autodev fails closed with a named error (e.g., "autodev planner selection needs eligible free/subscription planner routes from at least two model families; found none (no paid or frontier fallback)") and sends nothing

#### Scenario: Explicit --orchestrator-model that passes admission
- **WHEN** the dry run is given `--orchestrator-model free/alpha:free` (or any admissible route)
- **THEN** the planner is configured with `free/alpha:free`

#### Scenario: Explicit --orchestrator-model that is paid or inadmissible
- **WHEN** the dry run is given `--orchestrator-model mistral/codestral-latest` (pay-per-token) or an unknown/inadmissible route
- **THEN** autodev refuses with a named error (e.g., "orchestrator model 'mistral/codestral-latest' is not admissible: pay-per-token routes are not eligible for planning") and sends nothing

#### Scenario: No paid default
- **WHEN** the planner model is selected by the ladder
- **THEN** it is a FREE or SUBSCRIPTION route, never a pay-per-token route such as `mistral/codestral-latest`

### Requirement: Subscription pools SHALL be eligible for planning and execution

The `claude` and `codex` credential pools SHALL NOT be excluded from planner or executor selection. The `_PLANNER_EXCLUDED_POOLS` constant SHALL be removed. The `Decomposer.decompose` method SHALL NOT refuse `cc/*` planner routes. Routes from `claude` and `codex` pools SHALL be admitted through the normal admission ladder (operator ruling 2026-11-17, Linear BOD-342: D22 lifted fully for `cc/*` and `cx/*` routes through admission; pay-per-token stays excluded).

#### Scenario: Ladder returns a claude pool route
- **WHEN** the live ladder returns `cc/claude-sonnet-5-5` as an eligible planner route (FREE or SUBSCRIPTION capacity, reasoning+coding+frontier_worthy)
- **THEN** it MAY be selected as the planner model

#### Scenario: Decomposer receives a cc/* planner model
- **WHEN** `Decomposer.decompose` is called with a `cc/*` model in `planner_models` or `model`
- **THEN** it proceeds normally (no refusal)

#### Scenario: Pay-per-token stays excluded
- **WHEN** the only admitted routes are metered (pay-per-token)
- **THEN** autodev fails closed with a named "no paid or frontier fallback" error and sends nothing

### Requirement: The planner model SHALL have no fixed default

The planner configuration SHALL carry no built-in default model. A plan is requested only with a model chosen by the live eligibility ladder or given by an admitted `--orchestrator-model`. With neither, planning SHALL fail closed with a named error before any request is sent. The former fixed default `mistral/codestral-latest` (a pay-per-token route) SHALL NOT be used.

#### Scenario: Planner configured without a model
- **WHEN** a plan is requested with no planner model configured
- **THEN** planning fails with a named "no planner model configured" error and no request is sent

#### Scenario: Pinned executor path plans on the live ladder
- **WHEN** `verdict autodev --allow-live --executor-model <provider/model:free>` runs with no execution-path decision
- **THEN** the planner routes come from the live eligibility ladder, chosen before the executor probe, and an empty ladder fails closed before any probe is sent
