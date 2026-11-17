# Spec Delta

## Purpose

Defines `GET /v1/models` as a local verified projection kept fresh by a lifespan task.

## ADDED Requirements

### Requirement: /v1/models publishes only locally verified models

`GET /v1/models` SHALL NOT call the upstream gateway. It SHALL list only rows with a fresh positive basic proof younger than 600 s, projected from source snapshots that are also younger than 600 s, then apply the access allow and deny lists. Incomplete, absent, corrupt or expired inventory SHALL return a classified 503. A valid empty inventory SHALL return 200 with an empty list.

#### Scenario: Projection and readiness
- Proof: `tests/test_relay_admission.py::test_public_models_are_local_verified_projection_and_readiness_recovers`, `test_public_models_proof_age_is_strictly_below_600_seconds`, `test_public_models_inventory_authority_is_strictly_below_600_seconds`, `test_public_models_valid_current_empty_inventory_is_200_empty`, `test_public_models_incomplete_inventory_never_publishes`

#### Scenario: Upstream catalog is not forwarded
- Proof: `tests/test_proxy.py::test_models_endpoint_does_not_forward_the_upstream_catalog`, `tests/test_verified_models_projection.py::test_public_model_entry_is_built_only_from_a_verified_projection_row`

### Requirement: Lifespan refresh never outlives its source

One lifespan task SHALL refresh the readiness snapshot and SHALL be cancelled on shutdown. A failed load SHALL keep last-good data only until its original deadline. The relay admission cache SHALL be capped at the source deadline.

#### Scenario: Lifespan and deadlines
- Proof: `tests/test_relay_admission.py::test_public_models_last_good_never_outlives_its_original_deadline`, `test_lifespan_stop_closes_the_public_models_refresh_task`, `test_relay_admission_cache_recovers_from_local_source_and_never_outlives_it`, `test_cached_relay_admission_source_deadline_caps_the_ttl`
