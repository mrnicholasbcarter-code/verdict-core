# Spec Delta

## Purpose

Defines verified-history scoring: a smoothed, time-decayed, verified-only task-history score feeding order-only demotion in candidate ranking.

## ADDED Requirements

### Requirement: Verified history is scored over a 30-day window with a 7-day half-life

`score_task_history(observations, *, route_id, task_family, as_of)` SHALL only consider observations within `[0, 30d)` before `as_of`, weighted by `2 ** (-age_days / 7)`. The score SHALL be `(weighted_accepted + 0.5*weighted_partial + 1) / (weighted_eligible + 2)` (a 0.5 prior when no qualifying data exists). Observations SHALL be deduplicated by a stable `observation_id` before counting toward `observed_n`.

#### Scenario: Window and half-life boundaries
- Proof: exact 30-day and 7-day boundary tests in `tests/test_strength_profiles.py`

#### Scenario: Dedup
- **THEN** two observations with the same `observation_id` count once toward `observed_n`/`weighted_n`.

### Requirement: Demotion is order-only and never changes shortlist membership

A route SHALL be marked `demoted` only when `score < 0.4` and `observed_n >= 3` unique verified observations. Demotion SHALL only reorder an already-admitted shortlist via a final sort pass `(demoted, -score, route_id)`; membership (top-k plus provider-diversity fallback) SHALL remain score-only and unaffected by demotion.

#### Scenario: Demotion never drops a member
- Proof: `tests/test_candidate_pool.py` wiring test proving non-tie-break with alphabetically-reversed route ids

### Requirement: Only independently verified rows score history; no unverified fallback

`_score_features` SHALL call `score_task_history` only once a matched observation is independently verified (`quality_credit` and `observed_at` both set, sourced from a joined `checker_result` row per the attempt-ledger capability). Unverified or raw matched rows SHALL contribute nothing to ranking: no raw-accept-rate path and no classification-style fallback SHALL exist in production.

#### Scenario: Unverified rows never affect rank
- Proof: `tests/test_candidate_pool.py::test_unverified_outcome_rows_never_affect_rank`

### Requirement: History is loaded only from independently verified checker-joined rows

`load_verified_outcome_observations(roots, *, task_family, as_of, ...)` SHALL reuse the same shadow-run bounds as `load_shadow_rows_from_run` (8 roots / 2MB events / 20000 lines / 512KB receipt), find `implement`-role attempts, and join only independently-verified `checker_result` rows via `load_checked_attempts` (attempt-ledger capability). Only a joined row SHALL supply `observed_at`/`quality_credit`. Each observation SHALL carry `observation_id = f"{run_id}:{node_id}:{attempt_id}"`.

#### Scenario: Omissions are disclosed, never silently dropped
- **THEN** skipped roots, rejected checker rows and checker rows with no matching implement attempt are counted in `omissions`; a different task family is not counted as an omission.

### Requirement: A history-loader failure fails closed to no history, never blocks admission

An exception from `load_verified_outcome_observations` SHALL NOT block candidate-pool admission. It SHALL be logged locally with the real message and SHALL be disclosed on the candidate-pool receipt's `history_disclosure` field using only bounded counts and fixed reason codes (`status`, `reason`, `error_class`, `observations_count`, `omitted`, `reasons: dict[reason_code, count]`). Raw omission strings and exception messages SHALL NOT appear in `history_disclosure`.

#### Scenario: Disclosure never leaks a local path
- Proof: `tests/test_intelligence_service.py` disclosure tests asserting the schema carries counts/reason-codes only
