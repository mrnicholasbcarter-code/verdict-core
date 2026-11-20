## ADDED Requirements

### Requirement: Capability escalation after repeated judged failures

After K=2 judged failures on a node, the runtime SHALL prefer a route whose
`capability_tier` is strictly lower (stronger) than the tier of the most
recently selected route.  "Judged failure" means the failure classification
category is in `{diff_rejected, verification_failed}` or the attempt was
independently validated wrong (`validated=True`).  Strength is determined
exclusively from `capability_tier` in route metadata or the name-based
classifier fallback; model name strings are never used for strength comparison.

The effective tier ceiling after K judged failures is `last_selected_tier - 1`.
The ceiling is monotone: it can only decrease, never increase within a node run.
If no admitted route satisfies the ceiling, the runtime falls back to the full
admitted set so the budget stays bounded; in that case no `escalation` event is
emitted.  Interrupted failures (e.g. `quota_exhausted`, `rate_limited`) that do
not indicate wrong work do not count toward K.

Admission is never widened by escalation: routes in `exclude_routes` (tried set)
remain excluded, and opaque routes (`auto/*`, `combo/*`) are never candidates.

#### Scenario: Two judged failures escalate to a stronger route

- **GIVEN** a node has accumulated 2 judged failures on a route with `capability_tier=3`
- **WHEN** the runtime selects the next route for that node
- **THEN** the selection applies `escalation_max_tier=2` (ceiling = failing_tier - 1)
- **AND** a route with `capability_tier <= 2` is selected
- **AND** an `escalation` event is emitted with `from_tier`, `to_tier`, `judged_failure_count`, and `reason="judged_failures_threshold"`
- **AND** the escalation event is recorded in `run.history`

#### Scenario: Escalation never selects a non-admitted route

- **GIVEN** a route R was excluded before escalation (in the tried set)
- **WHEN** escalation ceiling is active and R's tier satisfies the ceiling
- **THEN** R is NOT selected via escalation
- **AND** only routes not in the tried set are candidates

#### Scenario: Fallback when no stronger route exists

- **GIVEN** escalation ceiling is active but no route with the required tier exists in the pool
- **WHEN** the runtime attempts selection
- **THEN** the escalation path returns no route
- **AND** the runtime falls back to the full admitted set (no `escalation_max_tier`)
- **AND** no `escalation` event is emitted
- **AND** the run does not hang or exceed its attempt budget

#### Scenario: Non-judged failures do not count toward escalation

- **GIVEN** a node accumulates quota or rate-limit failures (non-judged)
- **WHEN** the runtime records these failures
- **THEN** `judged_failure_count` is NOT incremented
- **AND** no escalation ceiling is set from these failures
- **AND** no `escalation` event is emitted
