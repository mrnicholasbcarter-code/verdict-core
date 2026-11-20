# Spec Delta

## Purpose

Defines the behaviour of Chatbot Arena data as an advisory reordering source
for admitted route candidates in the Verdict eligibility ladder.

## ADDED Requirements

### Requirement: Arena evidence is advisory and never affects admission

The system SHALL use Arena leaderboard data only to reorder already-admitted
route candidates. Arena evidence SHALL NOT admit a route, change its free/paid
classification, or make any unknown-tier route appear healthy or ranked with
known-tier routes.

#### Scenario: Unknown-tier route excluded from Arena augmentation
- **WHEN** an Arena VR is available for a route
- **AND** the route's tier is unknown (`tier_known = False`)
- **THEN** the Arena VR SHALL NOT be applied to the route's rating

#### Scenario: Arena augmentation guarded by admission
- **WHEN** Arena data would reorder route candidates
- **THEN** only routes already admitted by the admission ladder SHALL be reordered

#### Scenario: Arena unavailable does not degrade routing
- **WHEN** the Arena snapshot is missing, corrupt, or unavailable
- **THEN** the routing system SHALL fall back to OmniRoute-score ranking with no error returned to the caller

### Requirement: Arena join is exact — no fuzzy or similarity matching

The system SHALL join Arena model names to store model identifiers using an
exact canonical `(org, slug)` key only. Fuzzy, similarity, or substring
matching SHALL NOT be used. Any Arena row that cannot be exactly matched SHALL
be recorded as a named drop, never silently ignored.

#### Scenario: Iteration forbidden on store index
- **WHEN** Arena matching code accesses the store index
- **THEN** the index SHALL raise `RuntimeError` on `__iter__`, `__len__`,
  `.keys()`, `.values()`, and `.items()`, permitting only `.get()` calls

#### Scenario: One-character name perturbation is a miss
- **WHEN** an Arena model name is perturbed by one character (letter added, removed, or changed)
- **THEN** the join SHALL return a Drop, not a Match

#### Scenario: Explicit crosswalk target missing from store
- **WHEN** a crosswalk map entry lists a `models_dev_id` that is not present in the store index
- **THEN** the Arena row SHALL be dropped with reason `crosswalk_target_missing`

### Requirement: Arena Verdict Rating is a bounded percentile

The system SHALL compute Arena VR as the percentile of a model's lower-bound
metric against a shared reference set of store-matched models with usable data,
yielding a value in the range [0, 100]. VR SHALL NOT be computed when the
reference set has fewer than 15 usable models.

#### Scenario: VR in range
- **WHEN** VR is computed for any model
- **THEN** the result SHALL be in the range [0, 100] inclusive

#### Scenario: Reference set too small
- **WHEN** fewer than 15 store-matched models have usable Arena data for a config
- **THEN** VR SHALL return None for that config and the chain SHALL continue to the next config

#### Scenario: Floor exclusion
- **WHEN** a model's vote count or session count is below the floor for the config
- **THEN** that model SHALL be excluded from both VR computation and the reference set

#### Scenario: Stale data exclusion
- **WHEN** the leaderboard_publish_date for a model's slot is more than 45 days old
- **THEN** that model SHALL be excluded from both VR computation and the reference set

### Requirement: Arena snapshot is refreshed inside the metadata refresh schedule

The system SHALL fetch Arena data inside `verdict metadata refresh` using a
24-hour per-config staleness gate. Arena fetch SHALL use stdlib `urllib` only.
The fetch target host SHALL be validated against `ALLOWED_METADATA_HOSTS` before
any network connection is opened. A failed fetch SHALL keep the existing snapshot
intact.

#### Scenario: 24-hour gate skips fresh configs
- **WHEN** the saved Arena snapshot for a config is less than 24 hours old
- **THEN** that config SHALL be skipped and the existing data SHALL be preserved

#### Scenario: Failed fetch preserves existing snapshot
- **WHEN** an Arena fetch attempt fails (network error, HTTP error, or parse error)
- **THEN** the previously saved snapshot SHALL remain intact and unchanged

#### Scenario: Host outside allowlist rejected
- **WHEN** the fetch URL resolves to a host not in `ALLOWED_METADATA_HOSTS`
- **THEN** the fetch SHALL be rejected before any network connection is opened

### Requirement: Doctor reports Arena snapshot freshness

The system SHALL include an `Arena ratings` row in `verdict doctor` output
reporting one of four states: `missing`, `ok`, `warning`, or `stale`.

#### Scenario: Snapshot missing
- **WHEN** no Arena snapshot file exists on disk
- **THEN** doctor SHALL report `Arena ratings: missing` with a prompt to run
  `verdict metadata refresh`

#### Scenario: Snapshot fresh
- **WHEN** all Arena configs have `fetched_at` less than 48 hours ago
- **THEN** doctor SHALL report `Arena ratings: ok`

#### Scenario: Snapshot fetch overdue
- **WHEN** at least one Arena config has `fetched_at` 48 or more hours ago
- **THEN** doctor SHALL report `Arena ratings: warning`

#### Scenario: Leaderboard data stale
- **WHEN** at least one Arena config has `leaderboard_publish_date` more than 45 days ago
- **THEN** doctor SHALL report `Arena ratings: stale`

### Requirement: Coverage report available via CLI

The system SHALL provide a `verdict metadata arena-coverage` subcommand that
reports join match / drop / twin-group counts and reference-set sizes per role
from the on-disk snapshot.

#### Scenario: Coverage report runs without live network access
- **WHEN** `verdict metadata arena-coverage` is invoked
- **THEN** the command SHALL load only the on-disk snapshot and store, without
  making any outbound network request

#### Scenario: JSON output option
- **WHEN** `verdict metadata arena-coverage --json` is invoked
- **THEN** the output SHALL be valid JSON
