# Spec Delta: Bounded planner confirmation fallback

## ADDED Requirements

### Requirement: Planner confirmation SHALL reach a fresh fallback within its budget
Within each capacity class, planner confirmation SHALL try fresh healthy routes
before cold routes. When earlier candidates would spend every confirmation
slot, the last existing slot SHALL be reserved for a fresh SUBSCRIPTION
fallback; a confirmed FREE route still wins. Admission, capability floors,
cooldown scopes and probe limits SHALL stay unchanged. Planner events SHALL
record the reserved-slot selection reason.

#### Scenario: Cold free aliases fill the budget
- **WHEN** cold FREE aliases would use every confirmation slot and a fresh SUBSCRIPTION route exists
- **THEN** the last slot confirms the SUBSCRIPTION route and the planner event names the reserved slot

#### Scenario: A free route confirms
- **WHEN** a FREE route confirms before the reserved slot
- **THEN** the FREE route is selected

### Requirement: Invalid planner output SHALL be normalized narrowly and retained
Only non-empty acceptance strings SHALL be normalized to singleton lists.
Invalid planner outputs SHALL be kept in the run directory, bounded and redacted.

#### Scenario: Acceptance given as a string
- **WHEN** a planner returns `acceptance: "pytest -q"`
- **THEN** the plan holds `["pytest -q"]`, and an empty string stays invalid
