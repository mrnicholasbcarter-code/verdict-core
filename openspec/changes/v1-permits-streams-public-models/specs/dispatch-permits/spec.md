# Spec Delta

## Purpose

Defines durable dispatch permits and per-scope revocation that gate every relay send and worker spawn.

## ADDED Requirements

### Requirement: A permit is committed before every send or spawn

`HealthCache.commit_dispatch` SHALL return a `DispatchPermit` only when the proof's captured revocation epoch equals the current scope epoch, the attempt id is unused, and the proof is neither expired nor stamped in the future. A minted permit SHALL be stored as `maybe_sent` and SHALL survive restart. A denied commit SHALL result in zero sends or spawns.

#### Scenario: Linearization at the permit
- **WHEN** a scope is revoked before commit
- **THEN** commit returns no permit
- **WHEN** a scope is revoked after commit
- **THEN** the permit stays `maybe_sent` and the attempt cannot mint a second permit
- Proof: `tests/test_health_cache.py::test_revocation_linearizes_at_dispatch_permit_and_survives_restart`

#### Scenario: Duplicate and late writers
- **WHEN** an attempt id is reused or a fenced writer commits late
- **THEN** no second permit is minted
- Proof: `tests/test_dispatch_permits.py::test_duplicate_attempt_id_never_obtains_a_second_permit`, `test_late_fenced_writer_cannot_mint_a_second_permit`

#### Scenario: Proof validity window
- **WHEN** a proof is expired or stamped in the future
- **THEN** it never authorizes dispatch (`tests/test_dispatch_permits.py::test_proof_expiry_window_never_authorizes_stale_or_future_proofs`)

#### Scenario: Inputs and persistence
- **THEN** invalid scope and permit inputs are rejected (`test_scope_and_permit_inputs_are_validated`)
- **THEN** caches without permits keep their on-disk shape (`test_dispatch_envelope_is_additive_for_permit_free_caches`)

### Requirement: Revocations are idempotent per scope and fail closed

`record_revocation` SHALL be idempotent per (scope, event id), including replay after restart, and SHALL increment the persisted epoch of that scope only. A future-dated negative SHALL stay in force. `latest_revocation_at` SHALL be a read-only scoped maximum.

#### Scenario: Replay and clock skew
- Proof: `tests/test_dispatch_permits.py::test_revocation_event_replay_is_idempotent_across_restart`, `test_future_dated_negative_stays_in_force`, `test_latest_revocation_at_is_read_only_scoped_maximum`

### Requirement: The API relay gates every send

The relay SHALL send only with a route-bound admission proof whose scope equals the expected scope (gateway origin, selected route, recomputed task profile digest). A passport qualified at or before the latest scoped revocation SHALL be denied. A 404 SHALL revoke only on a structured model-not-found error code.

#### Scenario: Denied paths make zero sends
- **WHEN** the proof is missing, foreign, stale, revoked or bound to another profile
- **THEN** the relay returns 503 and does not send
- Proof: `tests/test_api_dispatch_permits.py::test_dispatch_permit_gates_every_send_and_spawn`

### Requirement: Worker spawn is gated by a permit

A `WorkerController` in authority mode SHALL commit a permit immediately before `adapter.spawn`. A missing passport or denied commit SHALL spawn nothing. A failure after commit SHALL stay `maybe_sent`; a retry on the next candidate SHALL use a new attempt id and permit. Authentication, permission and model provenance mismatch failures SHALL record a revocation. A controller without `gateway_origin` SHALL behave as before.

#### Scenario: Worker paths
- Proof: `tests/test_worker_dispatch_permits.py::test_worker_and_orchestration_handoffs_require_permits`, `test_worker_no_fresh_passport_spawns_nothing`, `test_worker_without_any_passport_spawns_nothing`, `test_worker_revoked_between_bind_and_handoff_spawns_nothing`, `test_worker_passport_older_than_revocation_spawns_nothing`, `test_worker_failure_after_permit_stays_maybe_sent_and_retry_gets_new_permit`, `test_model_provenance_mismatch_records_revocation`, `test_non_authority_controller_is_unchanged`, `test_passport_confirmation_uses_the_passports_own_times`, `test_worker_profile_digest_depends_on_work_not_secrets`

### Requirement: The orchestration runtime permit gate is opt-in

`RuntimePolicy.require_dispatch_permit` SHALL default to off. When on, `DagRuntime` SHALL make zero executor calls without a proof. The production orchestration entrypoint does not enable it.

#### Scenario: Flag on
- Proof: `tests/test_orch_runtime.py::test_authority_mode_denies_every_send_without_a_proof`
