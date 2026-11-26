# Spec Delta

## ADDED Requirements

### Requirement: A refresh keeps the stored values of a source it skipped

When `refresh_metadata` skips a source, it SHALL keep that source's values from
the previous store in the rewritten store, unchanged and with their original
`fetched_at`, wherever the refresh produced no value of its own for that field.
Readers' freshness rules SHALL still decide whether a carried value counts.

#### Scenario: Fresh ratings survive a refresh without include-p1

- **GIVEN** a store holding fresh `openrouter.artificial_analysis` ratings
- **WHEN** `verdict metadata refresh` runs without `--include-p1`
- **THEN** the stored ratings are present with their original `fetched_at`

#### Scenario: Carried ratings past 12 hours stay stale

- **GIVEN** a store whose ratings were fetched more than 12 hours ago
- **WHEN** a refresh without `--include-p1` carries them forward
- **THEN** the planner band still counts no rated models from them

#### Scenario: A value from a source that ran is not carried

- **GIVEN** a source that ran in this refresh and no longer returns a value
- **WHEN** the refresh completes
- **THEN** the previous value for that source is not added by carry-forward

### Requirement: The refresh output says values were carried forward

The skipped source's `reason` in the refresh result SHALL state how many values on
how many records were carried forward, and SHALL keep the original skip reason.
Repeated skipped refreshes SHALL state it once.

#### Scenario: Output names the carry-forward

- **GIVEN** a refresh that skipped a source and carried values forward
- **WHEN** the refresh output is read
- **THEN** that source's status is `skipped` and its reason contains
  `carried forward N value(s) on M record(s)`

#### Scenario: Two skipped refreshes in a row

- **GIVEN** a store that two refreshes in a row carried values forward in
- **WHEN** the second refresh's reason is read
- **THEN** it contains the carry-forward note once and the original skip reason
  once
