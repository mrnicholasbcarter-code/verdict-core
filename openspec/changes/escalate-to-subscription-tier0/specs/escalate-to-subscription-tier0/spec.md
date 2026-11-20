## ADDED Requirements

### Requirement: Tier-0 escalation restricted to subscription/free capacity

When `last_selected_tier == 1` and `judged_failure_count >= K` on a node, the
runtime SHALL set `escalation_tier_ceiling = 0` and attempt to select a tier-0
route.  The escalation selection SHALL be restricted to routes whose
`CapacityClass` is in `RuntimePolicy.escalation_tier0_capacity_classes`
(default: `{SUBSCRIPTION, FREE}`).  Routes with `CapacityClass.METERED` or
`CapacityClass.UNKNOWN` SHALL NOT be selected through the escalation path.

If a qualifying tier-0 route is found, an `escalation` event SHALL be emitted
with `from_tier`, `to_tier`, `capacity_classes_allowed`, and
`reason="judged_failures_threshold"`.

If no qualifying tier-0 route is found (restricted selection returns None),
an `escalation_exhausted` event SHALL be emitted with
`reason="at_strongest_tier"` and the runtime SHALL fall back to the normal
admitted set so the budget stays bounded.

#### Scenario: Tier-1 subscription route escalates to tier-0 subscription route

- **GIVEN** a node has accumulated 2 judged failures on a route with `capability_tier=1`
- **AND** a route with `capability_tier=0` and `CapacityClass.SUBSCRIPTION` is in the pool
- **WHEN** the runtime selects the next route for that node
- **THEN** `escalation_tier_ceiling=0` is set with `escalation_capacity_classes={SUBSCRIPTION, FREE}`
- **AND** the tier-0 subscription route is selected
- **AND** an `escalation` event is emitted with `from_tier=1`, `to_tier=0`, and `capacity_classes_allowed`
- **AND** no `escalation_exhausted` event is emitted

#### Scenario: METERED tier-0 route is never selected through escalation

- **GIVEN** a node has accumulated 2 judged failures on a tier-1 route
- **AND** only a tier-0 `CapacityClass.METERED` route is in the pool
- **WHEN** the runtime attempts the tier-0 escalation selection
- **THEN** the METERED route is excluded from the escalation path (reason: `escalation_capacity_excluded`)
- **AND** an `escalation_exhausted` event is emitted with `reason="at_strongest_tier"`
- **AND** the METERED route is never the target of an `escalation` event

#### Scenario: Free-only policy excludes subscription tier-0 from escalation

- **GIVEN** `RuntimePolicy.escalation_tier0_capacity_classes = {FREE}`
- **AND** only a tier-0 `CapacityClass.SUBSCRIPTION` route is in the pool
- **WHEN** the runtime attempts the tier-0 escalation selection
- **THEN** the subscription route is excluded (reason: `escalation_capacity_excluded`)
- **AND** an `escalation_exhausted` event is emitted

#### Scenario: Non-escalated selection is unchanged

- **GIVEN** `escalation_capacity_classes` is empty (default) in `TaskRequirements`
- **WHEN** the runtime performs ordinary (non-escalated) selection
- **THEN** no capacity class restriction is applied
- **AND** routes of all capacity classes (METERED, UNKNOWN, etc.) pass the capacity filter

#### Scenario: Tier-0 failure emits escalation_exhausted from _record_failure

- **GIVEN** a node is running on a tier-0 route (`last_selected_tier=0`)
- **AND** `judged_failure_count >= K`
- **WHEN** `_record_failure` is called
- **THEN** `escalation_exhausted` is emitted with `reason="at_strongest_tier"`
  from `_record_failure` (no ceiling can be set below tier-0)

### MODIFIED Requirement: escalation_exhausted event source for t==1

The `escalation_exhausted` event for `last_selected_tier=1` is now emitted from
`_drive` (after the lazy tier-0 selection attempt) rather than from
`_record_failure`.  For `last_selected_tier=0` the event continues to be
emitted from `_record_failure`.

The event fields (`from_tier`, `judged_failure_count`, `reason`) remain
identical.  The `escalation_exhausted` event from `_drive` additionally
carries a `capacity_classes_allowed` field listing the classes that were
in effect for the failed escalation attempt.
