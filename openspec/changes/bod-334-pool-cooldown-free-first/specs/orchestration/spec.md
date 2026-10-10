# Orchestration changes

## ADDED Requirements

### Requirement: Pool-bound recovery

The ladder SHALL block every alias in a cooled credential pool until expiry.
Planner and worker retry exclusions SHALL use the same pool identity.

#### Scenario: Claude alias is cooling

- **WHEN** cc/claude-haiku is cooled and claude/ or no-think/cc/ aliases are candidates
- **THEN** dispatch and selection reject those aliases during the cooldown window

### Requirement: Sufficient free-first planning

Planning SHALL rank admitted sufficient free routes before subscription routes.
The planner SHALL require capability tier 2 or better and record the selected floor.

#### Scenario: Free candidate is sufficient

- **WHEN** a free tier-2 route and subscription tier-1 route pass all gates
- **THEN** the free route is selected

#### Scenario: Free candidate is insufficient

- **WHEN** only tier-3 free routes exist and a subscription route meets tier 2
- **THEN** the subscription route is selected

### Requirement: Actionable ownership repair

Worker prompts SHALL forbid summary/notes files and direct reports to final messages.
Ownership barriers SHALL list stray paths for retry feedback.
Planner repair prompts SHALL name conflicting nodes/files and suggest serializing or merging nodes.

#### Scenario: Parallel nodes share a file

- **WHEN** two unordered nodes own shared.py
- **THEN** repair names both nodes and shared.py and suggests depends_on or merging
