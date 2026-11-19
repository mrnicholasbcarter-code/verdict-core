## MODIFIED Requirements

### Requirement: Lifespan refresh never outlives its source

One lifespan task SHALL refresh the readiness snapshot and SHALL be cancelled on shutdown. Shutdown SHALL end the refresh task even when a cancel is lost inside an in-flight load. A failed load SHALL keep last-good data only until its original deadline. The relay admission cache SHALL be capped at the source deadline.

#### Scenario: Lifespan and deadlines
- **WHEN** the lifespan stops, a load fails, or the source deadline passes
- **THEN** the refresh task ends, last-good data expires at its original deadline, and the relay cache never outlives the source
- Proof: `tests/test_relay_admission.py::test_public_models_last_good_never_outlives_its_original_deadline`, `test_lifespan_stop_closes_the_public_models_refresh_task`, `test_lifespan_stop_is_bounded_when_a_refresh_swallows_the_cancel`, `test_relay_admission_cache_recovers_from_local_source_and_never_outlives_it`, `test_cached_relay_admission_source_deadline_caps_the_ttl`
