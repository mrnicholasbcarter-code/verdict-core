# Spec Delta: orchestrate effort dispatch

## ADDED Requirements

### Requirement: Orchestrate SHALL record the chosen effort on the selection it dispatches
For each implement, review, or research attempt, the runtime SHALL choose effort with the existing task policy before emitting the selection event. Implement and integrate map to `implement`. Review maps to `review`. Research maps to `plan`. The selection event SHALL include `chosen_effort` and `effort_reason`. When no model row is available, the effort SHALL be the task default and the reason SHALL say the row was unavailable. The runtime SHALL NOT invent a thinking-level map.

#### Scenario: Node kind sets the default
- **WHEN** an implement node, a review node, and a research node are selected with no model row
- **THEN** the selection events record `medium`, `high`, and `high` respectively

#### Scenario: Reasoning disabled omits the override
- **WHEN** the selected model row has `reasoning` false
- **THEN** `chosen_effort` is null and the reason clamp is `reasoning_disabled`

#### Scenario: Unsupported default clamps down
- **WHEN** the task default is `medium` and the explicit map has `low` but not `medium`
- **THEN** the chosen effort is `low` and is never a higher level than the default

### Requirement: Dispatch SHALL pass chosen effort only to executors that accept it
The per-call effort SHALL override that call only. Omitting the argument SHALL keep the executor's constructor behaviour. Explicit null SHALL omit the provider override. An executor whose `run` does not accept `effort` SHALL still be called with the same arguments as before. Parallel calls on one executor SHALL NOT change each other's effort.

#### Scenario: Direct gateway payload
- **WHEN** effort `high` is passed
- **THEN** the chat completions body includes `reasoning_effort` `high`
- **WHEN** effort is omitted or null
- **THEN** the body has no `reasoning_effort` key

#### Scenario: Prime argv
- **WHEN** a per-call effort is passed
- **THEN** that call's argv contains `--thinking` and that effort, and the constructor value is unchanged

#### Scenario: Legacy fake
- **WHEN** the executor `run` method does not accept `effort`
- **THEN** the call succeeds and does not raise `TypeError`

### Requirement: The attempt receipt SHALL bind chosen effort to the dispatched route
The attempt row SHALL copy `chosen_effort` and `effort_reason` from the selection whose route was dispatched. A selection replaced before dispatch SHALL NOT leak its effort onto a different route. `executed_effort` SHALL be `unverified` unless the executor reports applied effort, which this gateway does not. A legacy event stream with no `chosen_effort` SHALL produce the previous receipt shape. JSON null SHALL be preserved.

#### Scenario: Replaced selection
- **WHEN** a selection for one route is replaced by a selection for another route before dispatch
- **THEN** the dispatched attempt carries only the replacement route's chosen effort

#### Scenario: Legacy stream
- **WHEN** events have no `chosen_effort`
- **THEN** the attempt row has no chosen-effort, effort-reason, or executed-effort field
