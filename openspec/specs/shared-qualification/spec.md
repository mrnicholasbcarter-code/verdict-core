# Spec Delta

## Purpose

Defines scope-bound reuse and per-provider fairness in the shared verified-model refresh coordinator (phase 1).

## ADDED Requirements

### Requirement: Refresh reuse is bound to a stable scope

`RefreshSnapshot.scope` SHALL identify the configuration, profile and consent scope and SHALL NOT change when the coordinator's own evidence changes. A snapshot with no explicit scope SHALL derive a gateway-bound scope. Debounce and join SHALL reuse outcomes only for an equal scope. A joiner with a different scope SHALL get a bounded `lock_timeout`, make zero calls and learn no foreign job id. A scope-less legacy marker SHALL match only a gateway-derived scope.

#### Scenario: Scope isolation
- Proof: `tests/test_verified_model_refresh.py::test_cross_producer_qualification_budget_scope_crash_and_fairness`, `test_empty_scope_is_gateway_derived_never_matches_across_gateways`, `test_empty_scope_join_never_reuses_another_gateways_outcomes`, `test_join_waits_past_stale_other_scope_finished_marker`, `test_joined_job_survives_a_later_foreign_scope_marker`, `test_scope_mismatch_does_not_leak_foreign_job_id`

#### Scenario: Adapter scope
- **THEN** the adapter snapshot scope is stable, non-secret and gateway-bound (`tests/test_verified_refresh_consumers.py::test_adapter_snapshot_scope_is_stable_nonsecret_and_gateway_bound`)

### Requirement: Per-provider fairness

With `max_per_provider` set, the coordinator SHALL probe at most that many candidates per normalized provider name. Interactive configuration SHALL have no per-provider cap.

#### Scenario: Cap
- Proof: `tests/test_verified_model_refresh.py::test_per_provider_cap_normalizes_provider_names`, `test_interactive_has_no_per_provider_cap`

### Requirement: Refresh config is validated before any call

Invalid refresh configuration, including environment values, SHALL raise before any probe call.

#### Scenario: Config
- Proof: `tests/test_verified_model_refresh.py::test_invalid_config_raises`, `test_config_from_env_validates_before_calls`
