# Spec Delta

## ADDED Requirements

### Requirement: The extra review runs with a prompt ceiling

When the ready gate runs its extra cross-family review, delivery SHALL pass the reviewer tool a per-group prompt token ceiling. The default SHALL be 200,000 tokens; `--review-max-tokens N` SHALL change it and 0 SHALL turn it off. The in-run review of `verdict orchestrate` SHALL NOT be given a ceiling by this change.

#### Scenario: default ceiling

- **WHEN** `verdict deliver` runs the extra review with no option
- **THEN** the reviewer tool is called with a prompt ceiling of 200,000

#### Scenario: ceiling turned off

- **WHEN** `verdict deliver --review-max-tokens 0` runs the extra review
- **THEN** the reviewer tool is called without a prompt ceiling

### Requirement: Preferred reviewer routes are tried first and never override admission

Delivery SHALL accept a list of preferred reviewer routes (`--reviewer-route`, repeatable, or `VERDICT_DELIVER_REVIEWER_ROUTES`). When the list is not empty, the gate SHALL first choose its reviewer among the listed routes that the ladder admits and that are in none of the implementer families. When no listed route qualifies, the gate SHALL choose as it does without a preference. A preference SHALL NOT admit a route, bypass a health, quota or policy gate, or allow a route in an implementer family.

#### Scenario: preferred route is admitted

- **WHEN** a preferred route is admitted and its family is none of the implementer families
- **THEN** that route runs the extra review and `delivery.json` names it

#### Scenario: preferred route is not admitted

- **WHEN** the only preferred route is not admitted by the ladder
- **THEN** the reviewer is chosen as without a preference, and the preferred route is not used

#### Scenario: preferred route is in an implementer family

- **WHEN** the only preferred route is in an implementer family
- **THEN** it does not review; the reviewer is chosen as without a preference, or the gate stops with `no_cross_family_reviewer`
