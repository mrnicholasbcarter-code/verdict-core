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

`RuntimePolicy.require_dispatch_permit` SHALL default to off. When on, `DagRuntime` SHALL make zero executor calls without a proof. The production orchestration entrypoint (`verdict orchestrate`) enables it and supplies a `DispatchAuthority` (PR 920, a79e787d).

#### Scenario: Flag on
- Proof: `tests/test_orch_runtime.py::test_authority_mode_denies_every_send_without_a_proof`

### Requirement: Dispatch denies on an executor-reported gateway-origin mismatch

Immediately before `_commit_permit`, the dispatch path SHALL compare the executor's reported gateway origin (`executor_gateway_origin(executor, node_id)`) against the bound `DispatchScope.gateway_origin`, both normalized (`normalize_gateway_origin`). When the executor reports a non-`None` origin that does not match, the node SHALL be BLOCKED with failure `dispatch_denied` / BLOCK / `"dispatch_denied: executor origin mismatch"`. No permit SHALL be committed and the executor SHALL NOT be called; there is no reselect or retry for this failure.

#### Scenario: Mismatch denies dispatch
- Proof: `tests/test_orch_gateway_origin.py` — other origin: zero sends, BLOCKED, one `dispatch_denied` failure, no permit for the attempt.

#### Scenario: Equivalent origin forms match
- Proof: same origin with a plain URL, a trailing slash, `/v1`, or an upper-case host plus explicit `:80` all mint a permit and send once.

### Requirement: An executor reporting no gateway origin is denied (U1)

`executor_gateway_origin` SHALL return `None` when the executor reports nothing. When a dispatch authority is set, a `None` report SHALL be BLOCKED with failure `dispatch_denied` / BLOCK / `"dispatch_denied: executor origin unknown"`: no permit is committed and the executor is not called. `PrimeHeadlessExecutor.gateway_origin_for(node_id, *, route_id, cwd)` SHALL prepare and pin a launch dir keyed by the `route_id` and `cwd` its `run()` will receive, and report `providers[provider].baseUrl` from that dir's private `models.json` copy, the exact file the launch's Prime reads. Every launch SHALL re-read the copy it is about to exec: a checked send launches only with its pinned dir, and that copy must still name the checked origin (a missing or changed pinned copy is a terminal `harness_configuration: executor origin changed` error with no exec); a send whose `route_id`/`cwd` was not checked, on an executor that has reported an origin, gets a fresh dir whose origin must match a checked origin or the same error; an executor with no origin check at all (planner, no dispatch authority) launches with a fresh dir as before. Re-pinning a send removes its previous dir, and when the runtime does not send after the check (origin, permit or admission denial), it SHALL call `release_executor_origin_pin` (forwarded by `MixedExecutor` and `FaultInjectingExecutor`) so the pinned dir is removed. It reports `None` when that copy, provider or URL is missing or unparseable. Executors that never touch the network (fake, offline demo) SHALL declare `LOCAL_EXECUTOR_ORIGIN` (`"local:"`), which matches any bound origin; no executor that sends to a gateway may declare it. `ScriptedExecutor` runs arbitrary code, so it reports no origin unless its constructor is given one (`gateway_origin=`).

#### Scenario: Unknown origin denies
- Proof: `tests/test_orch_gateway_origin.py` — a fake executor reporting no origin: zero calls, BLOCKED `executor origin unknown`, no permit.

#### Scenario: Prime origin comes from models.json
- Proof: `tests/test_orch_gateway_origin.py` — a matching `baseUrl` mints a permit and sends once; another host denies as a mismatch; a missing or unparseable `models.json`, a missing provider or a non-string `baseUrl` denies as unknown; rewriting the source `models.json` after the check leaves the launch on the pinned `baseUrl`; with a changed source, a launch on a cwd other than the checked one, or with a missing pinned dir, fails `executor origin changed` and never execs; a `baseUrl` of `local:` is unknown; three denials for the same send, or a permit denial after the check, leave no launch dir on disk.

#### Scenario: Local sentinel is allowed
- Proof: `tests/test_orch_gateway_origin.py` — a `LOCAL_EXECUTOR_ORIGIN` executor mints a permit and sends; a `ScriptedExecutor` without a declared origin is denied, with `gateway_origin=LOCAL_EXECUTOR_ORIGIN` it is allowed.

### Requirement: Gateway-origin normalization fails closed on a malformed URL

`normalize_gateway_origin(url)` SHALL return `scheme://host:port` with a lower-cased host and an explicit default port, dropping path/query/fragment, and SHALL return `None` for no scheme, no host, an invalid port, or a `urlsplit` `ValueError` (for example a malformed IPv6 literal such as `http://[::1`). `gateway_origin_matches(reported, bound)` SHALL be `True` only when both sides parse and are equal after normalization; a value that fails to parse matches nothing and is denied.

#### Scenario: Malformed origin denies
- Proof: `tests/test_orch_gateway_origin.py` — `:0`, `:99999`, and `http://[::1` cases all FAIL (deny), never send.

### Requirement: Wrapped executors resolve origin per node

`MixedExecutor.gateway_origin_for(node_id)` SHALL resolve the mapped executor's origin or the default executor's origin. `FaultInjectingExecutor.gateway_origin_for` SHALL delegate to its inner executor.

#### Scenario: Wrapper delegation
- Proof: `tests/test_mixed_executor.py`, `tests/test_orch_gateway_origin.py` wrapper cases.
