# Spec Delta

## ADDED Requirements

### Requirement: Admitted routes are ordered by verified success within a capacity class

For a node selection that carries the node's kind, the selector SHALL order admitted routes by a verified success score as the second sort key: after the capacity class and before slack and every rating or name-based key. The score SHALL NOT admit a route, change a rejection reason, or move a route across capacity classes. A route without enough verified results SHALL have the neutral score 0.5.

#### Scenario: recent failures for this kind

- **WHEN** two admitted routes share a capacity class, one has at least the minimum sample of verified failures for the node's kind and the other has no results
- **THEN** the route with the failures ranks below the other, and for a node of another kind in which the first route has its own results at or above the minimum sample, scored neutral, it ranks as it did without any history

#### Scenario: many passes never cross a class

- **WHEN** a route has many verified passes and a route of a cheaper capacity class has none
- **THEN** the cheaper-class route still ranks first, and the route with passes ranks above routes of its own class that have none

#### Scenario: membership is unchanged

- **WHEN** the same routes are evaluated against the same ledger and health evidence, once for a node that carries a kind (success ranking on) and once for a selection that carries none (success ranking off)
- **THEN** each route reaches the same stage with the same reason in both; the success rank only reorders, and admission evidence that already reads the ledger (the FREE and UNKNOWN gate) is not part of it

#### Scenario: a selection without a node kind

- **WHEN** a selection carries no node kind (planner, reviewer, report)
- **THEN** every route has the same neutral rank and the order is unchanged

### Requirement: The score comes from verified real work, keyed by route and node kind

The score SHALL be computed by the existing verified-history scorer from the ledger's controller-verified rows of real work inside a configured window. A route SHALL be scored by its results for the node's kind when it has at least the configured minimum sample of them, otherwise by its results for every kind when it has at least that minimum, otherwise it SHALL be neutral. Imported, probe and unverified rows SHALL NOT count. Aliases of one model in one backend pool SHALL share a record.

#### Scenario: fall back to the route

- **WHEN** a route has fewer than the minimum sample of results for the node's kind and at least the minimum across all kinds
- **THEN** it is scored by the route alone and the key used is `route`

#### Scenario: rows that do not count

- **WHEN** a route's only results are unverified, imported or probe rows
- **THEN** it is neutral with n equal to 0

### Requirement: The receipt records the score and what it was based on

Each candidate a selection event lists SHALL carry `rank_components.success` with the score, the sample size n, the key used (`route+kind`, `route` or null), the node kind, the source, the window in days and the minimum sample. When no score could be computed (no ledger, no node kind, unreadable ledger) the score and n SHALL be null and the source SHALL name the reason. The success rank SHALL add at most one ledger read per selection (the existing pooled summary reads are separate), and an unreadable ledger SHALL leave the order and membership unchanged.

#### Scenario: key and settings in the receipt

- **WHEN** a route is scored by its results for the node's kind with window 10 and minimum 4
- **THEN** its `success` shows key `route+kind`, the n, source `session_ledger`, window 10 and minimum 4

#### Scenario: unreadable ledger

- **WHEN** the ledger cannot be read during a selection
- **THEN** every candidate's `success` has a null score and source `unreadable`, and the order and membership are those of the same selection with success ranking off and the same admission evidence
