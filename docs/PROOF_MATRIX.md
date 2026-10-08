<!-- generated — do not edit by hand; run: python scripts/generate_proof_matrix.py -->
# Proof Matrix

> Auto-generated from [`docs/proof/proof_matrix.v1.json`](proof/proof_matrix.v1.json).
> Regenerate with `python scripts/generate_proof_matrix.py`.

| ID | Area | Claim | Source | Test / Locator | Evidence Artifact | Verify Command | Status |
|----|------|-------|--------|----------------|-------------------|----------------|--------|
| PM-001 | routing safety | Verdict filters candidates through hard eligibility gates before advisory ranking. | `verdict/eligibility.py` | EligibilityGate.evaluate | implementation | `pytest -q tests/test_eligibility_gate.py tests/test_adaptive_ranker.py` | verified |
| | | | `tests/test_eligibility_gate.py` | test_ranker_cannot_reintroduce_excluded_candidate | test | | |
| | | | `tests/test_adaptive_ranker.py` | test_excluded_cannot_be_reintroduced | test | | |
| PM-002 | availability | The local adapter handles malformed and contradictory observations explicitly; cached reports can remain stale during the grace window and must not be described as universally fail-closed for protected work. | `verdict/availability.py` | normalize_observation and select_capable_candidates | implementation | `pytest -q tests/test_availability.py tests/test_availability_adapter.py tests/test_availability_cache.py` | partial |
| | | | `verdict/availability_cache.py` | AvailabilityCache | implementation | | |
| | | | `tests/test_availability_adapter.py` | test_contradictory_and_malformed_observations_are_unknown_or_malformed | test | | |
| | | | `tests/test_availability_cache.py` | test_stale_window_serves_stale_then_triggers_single_refresh | test | | |
| PM-003 | evidence contracts | Passports separate declared, observed, and negotiated evidence for exact executable identities. | `verdict/capability_passports.py` | CapabilityEvidence and CapabilityPassport | implementation | `pytest -q tests/test_capability_passports.py tests/test_runtime_passports.py` | verified |
| | | | `verdict/runtime_passports.py` | RuntimeSubjectIdentity and RuntimeCapabilityPassport | implementation | | |
| | | | `verdict/schemas/runtime-passport.v1.json` | versioned runtime passport JSON schema | schema | | |
| | | | `docs/CAPABILITY_PASSPORTS.md` | full contract description | documentation | | |
| | | | `tests/test_capability_passports.py` | test_serialization_is_canonical_and_digest_detects_changes | test | | |
| | | | `tests/test_runtime_passports.py` | test_identity_is_secret_free_and_distinguishes_runtime_subjects | test | | |
| PM-004 | runtime compatibility | Verdict can render a deterministic, fail-closed compatibility matrix from existing runtime passport evidence. | `verdict/runtime_compatibility.py` | build_runtime_compatibility_report | implementation | `pytest -q tests/test_runtime_compatibility.py` | verified |
| | | | `tests/test_runtime_compatibility.py` | test_order_and_digest_are_deterministic | test | | |
| | | | `tests/test_runtime_compatibility.py` | test_limitations_are_visible_as_degraded_and_output_is_secret_safe | test | | |
| | | | `docs/proof/dogfood-bod-273-2026-09-28/README.md` | BOD-273 auth_failed diagnosis | evidence | | |
| PM-005 | policy and transitions | Routing policy and legal transition checks are deterministic and versioned. | `verdict/policy.py` | compile_policy and explain_policy | implementation | `pytest -q tests/test_policy_transitions.py` | verified |
| | | | `verdict/transitions.py` | TransitionCompiler | implementation | | |
| | | | `tests/test_policy_transitions.py` | all policy and transition invariants | test | | |
| | | | `verdict/schemas/policy.v1.json` | schema_version 1 | schema | | |
| PM-006 | receipts and privacy | Receipt contracts support scoped durable evidence, independent verification, claim history, redaction, and integrity checks. | `verdict/receipt_store.py` | ReceiptStore | implementation | `python -m pytest -q tests/test_proof_receipts.py tests/test_durable_receipts.py tests/test_security.py tests/test_evidence.py` | verified |
| | | | `verdict/evidence_receipts.py` | EvidenceReceipt | implementation | | |
| | | | `verdict/proof_receipts.py` | ProofReceipt and privacy-safe claim history | implementation | | |
| | | | `verdict/receipt_verifier.py` | dependency-free serialized receipt and manifest verifier | implementation | | |
| | | | `verdict/schemas/proof-receipt.v1.json` | strict versioned proof receipt schema | schema | | |
| | | | `docs/THREAT_MODEL_RECEIPTS.md` | threat model and retention boundary | documentation | | |
| | | | `tests/test_durable_receipts.py` | test_wal_and_integrity_chain | test | | |
| | | | `tests/test_proof_receipts.py` | round-trip, field tamper, redaction, missing evidence, and independent verification | test | | |
| | | | `tests/test_security.py` | test_decision_logging_never_writes_full_prompt | test | | |
| PM-007 | evaluation | The standalone evaluation controller checks evidence for its promotion decisions and supports rollback; adaptive ranker canary activation is not bound to that approval. | `verdict/evaluation.py` | EvaluationController and PromotionPolicy | implementation | `pytest -q tests/test_evaluation.py` | partial |
| | | | `tests/test_evaluation.py` | test_promotion_requires_passport_and_heldout_evidence | test | | |
| | | | `tests/test_evaluation.py` | test_promotion_lifecycle_kill_switch_and_rollback_are_fail_closed | test | | |
| | | | `verdict/adaptive_ranker.py` | AdaptiveRanker canary_active condition | implementation | | |
| PM-008 | credential-free proof | The repository includes a credential-free deterministic quickstart fixture with explicit exclusions. | `verdict/flagship_demo.py` | run_demo and validate_demo_result | implementation | `pytest -q tests/test_flagship_demo.py` | verified |
| | | | `scripts/flagship_demo.py` | source-checkout wrapper | implementation | | |
| | | | `tests/test_flagship_demo.py` | test_cli_output_is_reproducible_without_network_or_credentials | test | | |
| | | | `tests/test_flagship_demo.py` | test_cli_output_ignores_provider_environment_variables | test | | |
| PM-009 | reproducibility | Fixture structure and digests can be checked locally; measured benchmark timings and threshold verdicts are not byte-deterministic. Evidence bundles are reproducible when collected artifact bytes are identical. | `verdict/benchmarking.py` | run_reproducible_benchmarks | implementation | `pytest -q tests/test_benchmarking.py tests/test_evidence_bundle.py` | verified |
| | | | `benchmarks/fixtures/reproducible.json` | checked-in fixture | fixture | | |
| | | | `scripts/evidence_bundle.py` | collect_evidence, create_bundle, verify_bundle | implementation | | |
| | | | `tests/test_benchmarking.py` | test_reproducible_benchmark_report_is_deterministic_in_structure | test | | |
| | | | `tests/test_evidence_bundle.py` | test_identical_inputs_produce_identical_manifest_and_bundle | test | | |
| PM-010 | security and supply chain | Repository CI and security workflow definitions include checks for lint, types, tests, dependencies, static security, and CodeQL. | `.github/workflows/ci.yml` | test, lint, security, type-check, install-smoke, build jobs | workflow | `Inspect workflow definitions and require all branch-protection checks to pass on the release PR.` | verified |
| | | | `.github/workflows/security.yml` | security scan jobs | workflow | | |
| | | | `.github/workflows/codeql.yml` | CodeQL workflow | workflow | | |
| | | | `SECURITY.md` | security reporting policy | documentation | | |
| PM-011 | runtime catalog observation | The recorded catalog snapshots are bounded observations, not proof that every listed route is runnable. | `docs/evidence/omniroute-catalog-qualification-2026-07-28.json` | public, management, and liveness_sample | observed-artifact | `Validate the checked-in JSON artifacts and inspect their recorded hashes, aggregate consistency, and limitations; raw-payload digest replay is not available.` | observed |
| | | | `docs/evidence/omniroute-catalog-qualification-2026-07-29.json` | projection_reconciliation and limitations | observed-artifact | | |
| | | | `docs/adr/ADR-007-omniroute-catalog-qualification.md` | qualification boundary | documentation | | |
| PM-012 | portfolio claims | No quantified portfolio claim is currently approved by this matrix unless it resolves to a listed reproducible artifact. | `docs/portfolio/KALSHI_TRADING_BOTS_CASE_STUDY.md` | throughput and latency claims | claim-source | `For each metric, locate a reproducible artifact with definition, date, environment, and raw result before changing status.` | blocked |
| PM-013 | release readiness | The repository defines release evidence gates; the current checkout is not authorized to claim all gates passed. | `ACCEPTANCE_GATES.md` | G1-G7 gate definitions | requirement | `Run the release checklist against an exact tagged artifact and attach the resulting bundle and CI records.` | partial |
| | | | `scripts/evidence_bundle.py` | bundle verifier | implementation | | |
| | | | `RELEASE_CHECKLIST.md` | pre-release validation | checklist | | |
| | | | `VERSIONING.md` | release versioning | documentation | | |
| | | | `docs/proof/dogfood-bod-273-2026-09-28/receipt.json` | first complete real-model dogfood run | evidence | | |
| | | | `docs/proof/dogfood-bod-225-live-2026-09-29/receipt.json` | live dogfood COMPLETE receipt for BOD-225 producer provenance | evidence | | |
| PM-014 | local memory | The repository contains tested local memory and code-graph components with explicit scope and offline-oriented behavior. | `verdict/memory_plane.py` | MemoryPlane storage and scope boundary | implementation | `pytest -q tests/test_memory_plane.py tests/test_code_graph.py` | verified |
| | | | `verdict/code_graph.py` | CodeGraphEngine parsing and graph queries | implementation | | |
| | | | `tests/test_memory_plane.py` | restart, scope, export, and integrity tests | test | | |
| | | | `tests/test_code_graph.py` | code graph engine tests | test | | |
| PM-015 | security and privacy launch gate | Blocking static security and privacy checks are defined for PR/main. Mandatory security/privacy dependency for every tagged release and exact successful release checks remain unverified. | `.github/workflows/security.yml` | blocking bandit, pip-audit, and npm audit steps with no continue-on-error | implementation | `pytest -q tests/test_launch_gates.py tests/security/ tests/privacy/` | partial |
| | | | `.github/workflows/release.yml` | actions/attest-build-provenance OIDC-based supply-chain attestation | implementation | | |
| | | | `tests/test_launch_gates.py` | asserts security workflow keeps non-advisory audits and required CodeQL/OSV gates | test | | |
| | | | `tests/security/test_launch_gate_evidence.py` | memory-plane boundary, PII redaction, evidence-schema parity, supply-chain evidence tests | test | | |
| | | | `tests/privacy/test_retention_erasure.py` | retention/erasure and telemetry consent tests | test | | |
| | | | `specs/277-security-privacy-gate/quickstart.md` | end-to-end verification steps for the launch gate | test | | |
| PM-016 | bounded failover | Supervisor-driven recovery detects controller-progress stalls, preserves validated nodes and retries unfinished work. | `verdict/orchestration/recovery.py` | FailureIntelligence.classify and cooldown logic | implementation | `pytest -q tests/test_assignment_failover_e2e.py tests/test_orch_recovery.py` | verified |
| | | | `verdict/orchestration/supervisor.py` | Supervisor controller-progress stall detection and restart | implementation | | |
| | | | `docs/proof/live-controller-run/README.md` | live failover narrative | documentation | | |
| | | | `docs/proof/live-controller-run/events.jsonl` | STALLED and RESUMED events | evidence | | |
| | | | `docs/proof/live-controller-run/receipt.json` | completion receipt with digest | evidence | | |
| | | | `tests/test_assignment_failover_e2e.py` | test_provider_quota_failover_to_independent_review | test | | |
| | | | `docs/proof/dogfood-bod-225-live-2026-09-29/README.md` | live worker failover and controller resume narrative | documentation | | |
| | | | `docs/proof/dogfood-bod-225-live-2026-09-29/events.jsonl` | injected and real worker faults, cooldowns, reassignments, RESUMED | evidence | | |
| | | | `docs/proof/dogfood-bod-225-live-2026-09-29/receipt.json` | COMPLETE receipt with integrity digest after resume | evidence | | |
| PM-017 | context coverage contract | Context coverage contracts enforce named required-fact satisfaction before hydrated status is granted. | `verdict/free_tier_admit.py` | CheapPathContextPack and _check_fact_satisfaction | implementation | `pytest -q tests/test_bod272_coverage_contract.py` | verified |
| | | | `verdict/pack_state.py` | classify_pack_state with required_facts | implementation | | |
| | | | `tests/test_bod272_coverage_contract.py` | test_partial_when_required_fact_unsatisfied | test | | |
| | | | `tests/test_bod272_coverage_contract.py` | test_missing_fact_yields_partial_with_named_unsatisfied | test | | |
| | | | `tests/test_bod272_coverage_contract.py` | test_ac_proof_packed_as_mandatory_units | test | | |
| PM-018 | multi-story admission | When VERDICT_MULTI_STORY=on, multi-story admission serializes file-overlapping work and respects concurrency caps. | `scripts/prime_supervisor.py` | VERDICT_MULTI_STORY=on governor wiring | implementation | `pytest -q tests/test_supervisor_governor_flag.py tests/test_worker_admission_authority.py` | verified |
| | | | `tests/test_supervisor_governor_flag.py` | TestDisjointFootprintsAdmit | test | | |
| | | | `tests/test_supervisor_governor_flag.py` | TestSameFileSerialized | test | | |
| | | | `tests/test_supervisor_governor_flag.py` | TestGovernorCapDefers | test | | |
| | | | `tests/test_supervisor_governor_flag.py` | TestManualLabelExcluded | test | | |
| | | | `tests/test_worker_admission_authority.py` | test_worker_admission_can_require_measured_usage_evidence | test | | |
| PM-019 | outcome records | Node-level records include aggregate tokens, status, and reported/unknown costs; a separate per-attempt spend log rejects ambiguous request IDs. | `verdict/outcome_records.py` | OutcomeRecord and node-level aggregate usage | implementation | `pytest -q tests/test_outcome_records.py tests/test_outcome_log.py` | verified |
| | | | `tests/test_outcome_records.py` | TestLiveReceipt | test | | |
| | | | `tests/test_outcome_records.py` | receipt verifies event digest but not outcome JSONL sidecar | test | | |
| | | | `tests/test_outcome_log.py` | test_measured_spend_refuses_to_multiply_a_reused_request_id | test | | |
| | | | `tests/test_outcome_log.py` | test_measured_spend_sums_every_billed_attempt_not_just_the_final_one | test | | |
| PM-020 | dynamic assignment | Dynamic assignment handles quota and context-length failures and fails closed on pool exhaustion. Context overflow itself does not impose provider cooldown. | `verdict/orchestration/recovery.py` | FailureIntelligence.classify, request-scoped context overflow, and provider cooldown classification | implementation | `pytest -q tests/test_assignment_failover_e2e.py tests/test_failover_matrix_e2e.py` | verified |
| | | | `tests/test_assignment_failover_e2e.py` | test_provider_quota_failover_to_independent_review | test | | |
| | | | `tests/test_assignment_failover_e2e.py` | test_context_length_400_is_request_scoped | test | | |
| | | | `tests/test_failover_matrix_e2e.py` | test_pool_exhaustion_fails_closed_without_using_the_controller | test | | |
| | | | `docs/proof/dogfood-bod-225-live-2026-09-29/events.jsonl` | recorded live reassignments after injected quota and real timeout/no-final-answer | evidence | | |
| PM-021 | harness independence | One live Verdict run supervised two worker harnesses (Prime Agent headless and the direct gateway) in the same cockpit, with the harness recorded per attempt. | `verdict/orchestration/executors.py` | MixedExecutor (per-node harness routing; preserves each delegate's harness) | implementation | `verdict run-receipt --runs-dir docs/proof/mixed-harness-live-2026-09-30 run` | verified |
| | | | `tests/test_mixed_executor.py` | offline mixed-executor scenario (18 tests) | test | | |
| | | | `docs/proof/mixed-harness-live-2026-09-30/README.md` | live run 20260930T222758Z: node-1 prime-headless, node-2 direct-gateway, review PASS on cx/gpt-5.5, receipt COMPLETE integrity OK | evidence | | |
