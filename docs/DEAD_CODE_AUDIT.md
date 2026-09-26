# Dead-Code Audit

Generated: 2025-07-11 · Method: static import-reference scan across `verdict/`, `tests/`, `scripts/`, `pyproject.toml`

## Scope

- **218** verdict modules scanned
- **227** test files
- **27** script files
- Import patterns checked: `import verdict.X`, `from verdict.X import`, `from verdict import X`, `from verdict.pkg import X`, relative `from .X import`

---

## 1. Modules with Zero Importers

*None found — all modules have at least one importer.*

## 2. Modules Imported Only by Tests

These modules have no importer in `verdict/` itself or `scripts/`. They are exercised by tests, which confirms they are functional entry points (demo scripts, standalone adapters, or experimental modules) rather than dead code. They are preserved for now; the controller should decide whether to promote, document, or retire each one.

| Module | File | Tests | Verdict | Evidence |
|--------|------|-------|---------|----------|
| `verdict.anthropic_messages_adapter` | `verdict/anthropic_messages_adapter.py` | 1 | **KEEP** | test-exercised module (1 tests) |
| `verdict.capacity_adapters` | `verdict/capacity_adapters.py` | 2 | **KEEP** | test-exercised module (2 tests) |
| `verdict.capacity_direct` | `verdict/capacity_direct.py` | 1 | **KEEP** | test-exercised module (1 tests) |
| `verdict.capacity_gateway` | `verdict/capacity_gateway.py` | 1 | **KEEP** | test-exercised module (1 tests) |
| `verdict.capacity_project` | `verdict/capacity_project.py` | 1 | **KEEP** | test-exercised module (1 tests) |
| `verdict.compaction` | `verdict/compaction.py` | 1 | **KEEP** | test-exercised module (1 tests) |
| `verdict.context_lift` | `verdict/context_lift.py` | 3 | **KEEP** | test-exercised module (3 tests) |
| `verdict.controller_selection` | `verdict/controller_selection.py` | 1 | **KEEP** | test-exercised module (1 tests) |
| `verdict.decision_kernel_demo` | `verdict/decision_kernel_demo.py` | 3 | **KEEP** | demo module (test-imported) |
| `verdict.delivery_ports` | `verdict/delivery_ports.py` | 1 | **KEEP** | test-exercised module (1 tests) |
| `verdict.environment_discovery` | `verdict/environment_discovery.py` | 1 | **KEEP** | test-exercised module (1 tests) |
| `verdict.execution_hosts` | `verdict/execution_hosts.py` | 1 | **KEEP** | test-exercised module (1 tests) |
| `verdict.intelligence_adapter` | `verdict/intelligence_adapter.py` | 1 | **KEEP** | test-exercised module (1 tests) |
| `verdict.live_routing_run` | `verdict/live_routing_run.py` | 2 | **KEEP** | test-exercised module (2 tests) |
| `verdict.memory_migration` | `verdict/memory_migration.py` | 1 | **KEEP** | test-exercised module (1 tests) |
| `verdict.memory_mirror` | `verdict/memory_mirror.py` | 1 | **KEEP** | test-exercised module (1 tests) |
| `verdict.optimized_dispatch` | `verdict/optimized_dispatch.py` | 1 | **KEEP** | test-exercised module (1 tests) |
| `verdict.passport_eligibility` | `verdict/passport_eligibility.py` | 1 | **KEEP** | test-exercised module (1 tests) |
| `verdict.release.normalize` | `verdict/release/normalize.py` | 1 | **KEEP** | test-exercised module (1 tests) |
| `verdict.routing_demo` | `verdict/routing_demo.py` | 2 | **KEEP** | demo module (test-imported) |

## 3. Unused Scripts

| Script | Referenced By | Verdict | Evidence |
|--------|--------------|---------|----------|
| `scripts/benchmark.py` | benchmark.yml, acceptance-gates.yml | **KEEP** | referenced by benchmark.yml, acceptance-gates.yml |
| `scripts/check_doc_links.py` | lint.yml, test_check_doc_links.py | **KEEP** | referenced by lint.yml, test_check_doc_links.py |
| `scripts/check_no_obsolete_architecture.py` | test_obsolete_architecture_absent.py | **KEEP** | referenced by test_obsolete_architecture_absent.py |
| `scripts/check_structural_risk.py` | ADR-029-portfolio-repositioning-plan.md, test_structural_risk.py | **KEEP** | referenced by ADR-029-portfolio-repositioning-plan.md, test_structural_risk.py |
| `scripts/evidence_bundle.py` | test_evidence_bundle.py, acceptance-gates.yml | **KEEP** | referenced by test_evidence_bundle.py, acceptance-gates.yml |
| `scripts/flagship_demo.py` | README.md, DEMO.md | **KEEP** | referenced by README.md, DEMO.md |
| `scripts/generate_gates_report.py` | test_generate_gates_report.py, acceptance-gates.yml | **KEEP** | referenced by test_generate_gates_report.py, acceptance-gates.yml |
| `scripts/ingest_dependency_docs.py` | — | **REMOVE-CANDIDATE** | no references in CI, docs, pyproject, or tests |
| `scripts/memory_offline_smoke.py` | ADR-009-durable-memory-write-gate.md, memory-plane-offline-verification.md | **KEEP** | referenced by ADR-009-durable-memory-write-gate.md, memory-plane-offline-verification.md |
| `scripts/prime_state.py` | test_prime_state.py, test_prime_supervisor.py | **KEEP** | referenced by test_prime_state.py, test_prime_supervisor.py |
| `scripts/prime_supervisor.py` | controller-routing.md, test_prime_supervisor.py | **KEEP** | referenced by controller-routing.md, test_prime_supervisor.py |
| `scripts/prime_workflow.py` | 2026-09-13-prime-workflow.md, prime-workflow.md | **KEEP** | referenced by 2026-09-13-prime-workflow.md, prime-workflow.md |
| `scripts/proof/acceptance_smoke.py` | — | **REMOVE-CANDIDATE** | no references in CI, docs, pyproject, or tests |
| `scripts/proof/contract.py` | — | **KEEP** | imported by scripts/proof/run.py (referenced in .github/workflows/proof.yml) |
| `scripts/proof/run.py` | proof.yml | **KEEP** | referenced by proof.yml |
| `scripts/proof/runner.py` | — | **KEEP** | imported by scripts/proof/run.py (referenced in .github/workflows/proof.yml) |
| `scripts/proof/secrets_scan.py` | — | **REMOVE-CANDIDATE** | no references in CI, docs, pyproject, or tests |
| `scripts/quickstart.py` | acceptance-gates.yml | **KEEP** | referenced by acceptance-gates.yml |
| `scripts/record_waiver.py` | test_record_waiver.py | **KEEP** | referenced by test_record_waiver.py |
| `scripts/terminal_preview.py` | BRANCH_RECONCILIATION.md | **KEEP** | owner decision: KEEP |
| `scripts/validate_contract_schema.py` | architecture.md, ci.yml | **KEEP** | referenced by architecture.md, ci.yml |
| `scripts/verify_gates.py` | test_verify_gates.py, test_generate_gates_report.py | **KEEP** | referenced by test_verify_gates.py, test_generate_gates_report.py |
| `scripts/verify_proof_matrix.py` | PORTFOLIO_PROOF_MATRIX.md, CLAIMS_AUDIT_2026-09-06.md | **KEEP** | referenced by PORTFOLIO_PROOF_MATRIX.md, CLAIMS_AUDIT_2026-09-06.md |
| `scripts/verify_release_artifacts.py` | release.yml, test_verify_release_artifacts.py | **KEEP** | referenced by release.yml, test_verify_release_artifacts.py |
| `scripts/verify_release_versions.py` | release.yml, test_verify_release_versions.py | **KEEP** | referenced by release.yml, test_verify_release_versions.py |

## 4. Declared Dependencies

All declared dependencies in `pyproject.toml` are imported somewhere in `verdict/`.

| Dependency | Type | verdict/ files | Verdict | Evidence |
|------------|------|---------------|---------|----------|
| `httpx` | core | 12 | **KEEP** | HTTP client used in proxy, live routing, omniroute, probes |
| `PyYAML` | core | 4 | **KEEP** | YAML parsing in contracts, policy, qualification |
| `rich` | core | 6 | **KEEP** | console output in CLI, dashboard, terminal UI |
| `defusedxml` | core | 1 | **KEEP** | safe XML parsing in security module |
| `fastapi` | server optional | 1 | **KEEP** | api.py FastAPI app |
| `uvicorn` | server optional | 1 | **KEEP** | server startup in cli.py |
| `streamlit` | dashboard optional | 1 | **KEEP** | verdict/dashboard.py |
| `plotly` | dashboard optional | 1 | **KEEP** | verdict/dashboard.py charts |
| `pandas` | dashboard optional | 1 | **KEEP** | verdict/dashboard.py data frames |

## 5. Untracked Generated Artifacts (`.gitignore` additions)

The following directories were found untracked in the repo and are not covered by the existing `.gitignore`. Entries have been appended.

| Path | Reason |
|------|--------|
| `.playwright-mcp/` | Playwright MCP server state (generated at runtime) |
| `.serena/` | Serena MCP memory/cache dir (generated at runtime) |
| `benchmarks/fixtures/legit_workspace/.verdict/` | Runtime DB state inside benchmark fixture workspace |

## 6. Full Module Importer Table

<details>
<summary>All 218 verdict modules (expand)</summary>

| Module | Verdict importers | Test importers | Script importers | Verdict |
|--------|------------------|---------------|-----------------|---------|
| `verdict.__init__` | 0 | 2 | 0 | KEEP |
| `verdict.__main__` | 1 | 0 | 1 | KEEP |
| `verdict.adaptive_ranker` | 2 | 3 | 0 | KEEP |
| `verdict.admit_prove_confirm` | 1 | 2 | 0 | KEEP |
| `verdict.anthropic_messages_adapter` | 0 | 1 | 0 | KEEP |
| `verdict.api` | 2 | 12 | 1 | KEEP |
| `verdict.autodev_routing` | 2 | 4 | 0 | KEEP |
| `verdict.autodev_run` | 1 | 7 | 0 | KEEP |
| `verdict.availability` | 21 | 16 | 0 | KEEP |
| `verdict.availability_cache` | 3 | 4 | 0 | KEEP |
| `verdict.benchmarking` | 2 | 2 | 0 | KEEP |
| `verdict.bootstrap_install` | 1 | 0 | 0 | KEEP |
| `verdict.bounded_recovery` | 1 | 2 | 0 | KEEP |
| `verdict.candidate_pool` | 2 | 3 | 0 | KEEP |
| `verdict.capability_bootstrap` | 3 | 5 | 1 | KEEP |
| `verdict.capability_gate` | 5 | 2 | 0 | KEEP |
| `verdict.capability_passports` | 17 | 12 | 0 | KEEP |
| `verdict.capability_registry` | 3 | 2 | 0 | KEEP |
| `verdict.capacity_adapters` | 0 | 2 | 0 | KEEP |
| `verdict.capacity_aggregator` | 1 | 1 | 0 | KEEP |
| `verdict.capacity_direct` | 0 | 1 | 0 | KEEP |
| `verdict.capacity_gateway` | 0 | 1 | 0 | KEEP |
| `verdict.capacity_live` | 1 | 1 | 0 | KEEP |
| `verdict.capacity_models` | 7 | 2 | 0 | KEEP |
| `verdict.capacity_project` | 0 | 1 | 0 | KEEP |
| `verdict.capacity_resolve` | 1 | 1 | 0 | KEEP |
| `verdict.catalog` | 3 | 4 | 0 | KEEP |
| `verdict.chooser` | 2 | 4 | 0 | KEEP |
| `verdict.claims_ledger` | 1 | 1 | 0 | KEEP |
| `verdict.claims_models` | 1 | 0 | 0 | KEEP |
| `verdict.classifier` | 8 | 2 | 0 | KEEP |
| `verdict.cli` | 2 | 27 | 0 | KEEP |
| `verdict.code_graph` | 3 | 1 | 0 | KEEP |
| `verdict.commands.__init__` | 0 | 0 | 0 | KEEP |
| `verdict.commands.dispatch` | 1 | 0 | 0 | KEEP |
| `verdict.commands.parsers_autodev` | 1 | 0 | 0 | KEEP |
| `verdict.commands.parsers_harness` | 1 | 0 | 0 | KEEP |
| `verdict.commands.parsers_models` | 1 | 0 | 0 | KEEP |
| `verdict.commands.parsers_routing` | 1 | 0 | 0 | KEEP |
| `verdict.commands.parsers_runtime` | 1 | 0 | 0 | KEEP |
| `verdict.commands.parsers_setup` | 1 | 0 | 0 | KEEP |
| `verdict.compaction` | 0 | 1 | 0 | KEEP |
| `verdict.comparison` | 2 | 3 | 0 | KEEP |
| `verdict.compatibility_manifest` | 1 | 2 | 0 | KEEP |
| `verdict.context_budget` | 3 | 3 | 0 | KEEP |
| `verdict.context_envelope` | 1 | 2 | 0 | KEEP |
| `verdict.context_hydrate` | 3 | 4 | 0 | KEEP |
| `verdict.context_inject` | 1 | 1 | 0 | KEEP |
| `verdict.context_intelligence` | 2 | 5 | 0 | KEEP |
| `verdict.context_lift` | 0 | 3 | 0 | KEEP |
| `verdict.context_pack` | 18 | 12 | 1 | KEEP |
| `verdict.context_sources` | 1 | 1 | 0 | KEEP |
| `verdict.context_trust` | 1 | 2 | 0 | KEEP |
| `verdict.contracts` | 15 | 16 | 0 | KEEP |
| `verdict.controller_launch` | 1 | 1 | 1 | KEEP |
| `verdict.controller_selection` | 0 | 1 | 0 | KEEP |
| `verdict.cost_ledger` | 9 | 13 | 0 | KEEP |
| `verdict.daemon` | 2 | 1 | 0 | KEEP |
| `verdict.dashboard` | 1 | 0 | 0 | KEEP |
| `verdict.decision_kernel` | 2 | 3 | 0 | KEEP |
| `verdict.decision_kernel_demo` | 0 | 3 | 0 | KEEP |
| `verdict.decomposer` | 2 | 3 | 0 | KEEP |
| `verdict.delivery` | 1 | 2 | 0 | KEEP |
| `verdict.delivery_ports` | 0 | 1 | 0 | KEEP |
| `verdict.dependency_ingest` | 0 | 1 | 1 | KEEP |
| `verdict.discovery` | 2 | 4 | 0 | KEEP |
| `verdict.dispatcher` | 3 | 4 | 0 | KEEP |
| `verdict.documentation_preflight` | 6 | 3 | 0 | KEEP |
| `verdict.effective_capability` | 7 | 11 | 0 | KEEP |
| `verdict.eligibility` | 11 | 8 | 0 | KEEP |
| `verdict.enforcement` | 1 | 2 | 0 | KEEP |
| `verdict.environment_discovery` | 0 | 1 | 0 | KEEP |
| `verdict.escalation` | 3 | 1 | 0 | KEEP |
| `verdict.evaluation` | 1 | 1 | 0 | KEEP |
| `verdict.evidence` | 4 | 5 | 0 | KEEP |
| `verdict.evidence_receipts` | 1 | 1 | 0 | KEEP |
| `verdict.execution_hosts` | 0 | 1 | 0 | KEEP |
| `verdict.execution_packet` | 2 | 5 | 0 | KEEP |
| `verdict.execution_path` | 11 | 13 | 0 | KEEP |
| `verdict.execution_session` | 4 | 5 | 0 | KEEP |
| `verdict.expected_cost` | 6 | 10 | 1 | KEEP |
| `verdict.failover_engine` | 1 | 3 | 0 | KEEP |
| `verdict.failover_replay_proof` | 2 | 1 | 0 | KEEP |
| `verdict.fixture_paths` | 2 | 1 | 0 | KEEP |
| `verdict.flagship_demo` | 1 | 1 | 1 | KEEP |
| `verdict.free_route_harvest` | 3 | 2 | 0 | KEEP |
| `verdict.free_tier_admit` | 10 | 16 | 0 | KEEP |
| `verdict.gate` | 6 | 5 | 0 | KEEP |
| `verdict.gateway_adapter_runtime` | 3 | 2 | 0 | KEEP |
| `verdict.gateway_adapters` | 5 | 5 | 0 | KEEP |
| `verdict.gateway_conformance` | 1 | 1 | 0 | KEEP |
| `verdict.golden_path` | 1 | 1 | 0 | KEEP |
| `verdict.guidance` | 3 | 1 | 0 | KEEP |
| `verdict.handoff` | 4 | 2 | 0 | KEEP |
| `verdict.harness_claude` | 3 | 1 | 0 | KEEP |
| `verdict.harness_cline` | 3 | 1 | 0 | KEEP |
| `verdict.harness_codex` | 9 | 1 | 0 | KEEP |
| `verdict.harness_cursor` | 3 | 1 | 0 | KEEP |
| `verdict.harness_hermes` | 3 | 1 | 0 | KEEP |
| `verdict.harness_opencode` | 3 | 1 | 0 | KEEP |
| `verdict.harness_prime` | 3 | 1 | 0 | KEEP |
| `verdict.headroom` | 1 | 3 | 0 | KEEP |
| `verdict.home` | 2 | 12 | 0 | KEEP |
| `verdict.intelligence` | 6 | 18 | 1 | KEEP |
| `verdict.intelligence_adapter` | 0 | 1 | 0 | KEEP |
| `verdict.live_routing` | 6 | 5 | 0 | KEEP |
| `verdict.live_routing_gateway` | 3 | 3 | 0 | KEEP |
| `verdict.live_routing_run` | 0 | 2 | 0 | KEEP |
| `verdict.live_routing_usage` | 1 | 0 | 0 | KEEP |
| `verdict.logger` | 1 | 1 | 0 | KEEP |
| `verdict.mcp_config_discovery` | 1 | 1 | 0 | KEEP |
| `verdict.mcp_server` | 1 | 1 | 0 | KEEP |
| `verdict.memory_adapters` | 3 | 2 | 0 | KEEP |
| `verdict.memory_bridge` | 3 | 4 | 0 | KEEP |
| `verdict.memory_capture_policy` | 1 | 1 | 0 | KEEP |
| `verdict.memory_document_adapter` | 3 | 2 | 0 | KEEP |
| `verdict.memory_gate` | 5 | 6 | 1 | KEEP |
| `verdict.memory_graph_adapter` | 1 | 2 | 0 | KEEP |
| `verdict.memory_masterdocs_adapter` | 1 | 3 | 0 | KEEP |
| `verdict.memory_masterdocs_contracts` | 2 | 0 | 0 | KEEP |
| `verdict.memory_masterdocs_support` | 1 | 0 | 0 | KEEP |
| `verdict.memory_migration` | 0 | 1 | 0 | KEEP |
| `verdict.memory_mirror` | 0 | 1 | 0 | KEEP |
| `verdict.memory_outbox` | 2 | 1 | 0 | KEEP |
| `verdict.memory_plane` | 28 | 33 | 2 | KEEP |
| `verdict.memory_session_adapter` | 3 | 2 | 0 | KEEP |
| `verdict.metadata.__init__` | 0 | 0 | 0 | KEEP |
| `verdict.metadata.mapping` | 6 | 1 | 0 | KEEP |
| `verdict.metadata.records` | 9 | 5 | 0 | KEEP |
| `verdict.metadata.sources` | 3 | 2 | 0 | KEEP |
| `verdict.metadata.store` | 6 | 5 | 0 | KEEP |
| `verdict.model_passports` | 11 | 14 | 0 | KEEP |
| `verdict.models` | 28 | 40 | 1 | KEEP |
| `verdict.omniroute` | 4 | 5 | 0 | KEEP |
| `verdict.omniroute_catalog` | 3 | 1 | 0 | KEEP |
| `verdict.omniroute_catalog_stats` | 1 | 0 | 0 | KEEP |
| `verdict.optimized_dispatch` | 0 | 1 | 0 | KEEP |
| `verdict.orchestration.__init__` | 0 | 0 | 0 | KEEP |
| `verdict.orchestration.cli` | 1 | 0 | 0 | KEEP |
| `verdict.orchestration.contracts` | 11 | 9 | 0 | KEEP |
| `verdict.orchestration.eligibility` | 1 | 1 | 0 | KEEP |
| `verdict.orchestration.executors` | 1 | 1 | 0 | KEEP |
| `verdict.orchestration.planner` | 1 | 1 | 0 | KEEP |
| `verdict.orchestration.receipt` | 2 | 2 | 0 | KEEP |
| `verdict.orchestration.recovery` | 1 | 1 | 0 | KEEP |
| `verdict.orchestration.review` | 1 | 1 | 0 | KEEP |
| `verdict.orchestration.run` | 1 | 2 | 0 | KEEP |
| `verdict.orchestration.runtime` | 2 | 1 | 0 | KEEP |
| `verdict.orchestration.supervisor` | 1 | 1 | 0 | KEEP |
| `verdict.orchestration.tui` | 1 | 1 | 0 | KEEP |
| `verdict.outcome_log` | 2 | 3 | 0 | KEEP |
| `verdict.pack_state` | 2 | 2 | 0 | KEEP |
| `verdict.passport_eligibility` | 0 | 1 | 0 | KEEP |
| `verdict.patch_executor` | 3 | 5 | 0 | KEEP |
| `verdict.planner` | 3 | 3 | 0 | KEEP |
| `verdict.policy` | 5 | 2 | 0 | KEEP |
| `verdict.policy_artifacts` | 1 | 1 | 0 | KEEP |
| `verdict.present` | 2 | 2 | 0 | KEEP |
| `verdict.probes` | 14 | 5 | 0 | KEEP |
| `verdict.proof_receipts` | 4 | 3 | 0 | KEEP |
| `verdict.protocol_probes` | 3 | 3 | 0 | KEEP |
| `verdict.prove_at_rest` | 3 | 2 | 0 | KEEP |
| `verdict.provider_detection` | 2 | 3 | 0 | KEEP |
| `verdict.provider_receipts` | 3 | 4 | 0 | KEEP |
| `verdict.proxy` | 1 | 3 | 0 | KEEP |
| `verdict.qualification_report` | 1 | 1 | 0 | KEEP |
| `verdict.receipt_store` | 8 | 14 | 0 | KEEP |
| `verdict.receipt_verifier` | 2 | 1 | 0 | KEEP |
| `verdict.relay` | 4 | 1 | 0 | KEEP |
| `verdict.release.__init__` | 0 | 0 | 0 | KEEP |
| `verdict.release.emergency_approvers` | 1 | 1 | 0 | KEEP |
| `verdict.release.evidence` | 1 | 4 | 1 | KEEP |
| `verdict.release.normalize` | 0 | 1 | 0 | KEEP |
| `verdict.release.waivers` | 1 | 1 | 1 | KEEP |
| `verdict.repository_files` | 2 | 1 | 0 | KEEP |
| `verdict.responses_compatibility` | 2 | 1 | 0 | KEEP |
| `verdict.resume` | 2 | 2 | 0 | KEEP |
| `verdict.router` | 2 | 6 | 0 | KEEP |
| `verdict.routing_demo` | 0 | 2 | 0 | KEEP |
| `verdict.routing_demo_capture` | 1 | 0 | 0 | KEEP |
| `verdict.routing_demo_mock` | 1 | 0 | 0 | KEEP |
| `verdict.routing_receipt` | 3 | 1 | 1 | KEEP |
| `verdict.runtime_certification` | 7 | 10 | 0 | KEEP |
| `verdict.runtime_compatibility` | 1 | 1 | 0 | KEEP |
| `verdict.runtime_contract` | 2 | 0 | 0 | KEEP |
| `verdict.runtime_daemons` | 2 | 2 | 0 | KEEP |
| `verdict.runtime_health` | 2 | 1 | 0 | KEEP |
| `verdict.runtime_passports` | 3 | 3 | 0 | KEEP |
| `verdict.savings_bench` | 1 | 1 | 0 | KEEP |
| `verdict.savings_execution` | 2 | 1 | 0 | KEEP |
| `verdict.savings_live` | 1 | 1 | 0 | KEEP |
| `verdict.security` | 14 | 2 | 0 | KEEP |
| `verdict.semantic_capabilities` | 1 | 2 | 0 | KEEP |
| `verdict.serve_path` | 10 | 2 | 0 | KEEP |
| `verdict.session_economics` | 7 | 12 | 1 | KEEP |
| `verdict.setup_plan` | 2 | 4 | 0 | KEEP |
| `verdict.setup_presentation` | 1 | 0 | 0 | KEEP |
| `verdict.shared_memory` | 6 | 3 | 0 | KEEP |
| `verdict.shared_memory_mcp` | 1 | 1 | 0 | KEEP |
| `verdict.simulator` | 1 | 1 | 0 | KEEP |
| `verdict.strength_profiles` | 1 | 1 | 0 | KEEP |
| `verdict.structured_qualification` | 2 | 1 | 0 | KEEP |
| `verdict.subagent_models` | 1 | 1 | 0 | KEEP |
| `verdict.subagent_resolver` | 2 | 3 | 0 | KEEP |
| `verdict.subagent_selection` | 3 | 3 | 0 | KEEP |
| `verdict.suggestions` | 1 | 1 | 0 | KEEP |
| `verdict.task_profile` | 3 | 3 | 0 | KEEP |
| `verdict.terminal_ui` | 5 | 4 | 1 | KEEP |
| `verdict.tool_qualification` | 1 | 1 | 0 | KEEP |
| `verdict.transitions` | 3 | 3 | 0 | KEEP |
| `verdict.trusted_change_report` | 1 | 2 | 0 | KEEP |
| `verdict.work_unit` | 3 | 3 | 0 | KEEP |
| `verdict.worker_runtime` | 1 | 1 | 0 | KEEP |
| `verdict.workers` | 1 | 1 | 0 | KEEP |
| `verdict.workflows.__init__` | 0 | 1 | 0 | KEEP |
| `verdict.workflows.autodev` | 1 | 1 | 0 | KEEP |
| `verdict.worktree_registry` | 2 | 1 | 0 | KEEP |
| `verdict.worthiness` | 1 | 1 | 0 | KEEP |

</details>


## Controller review corrections (2026-09-24)

The audit is advisory. Nothing was deleted. The controller re-checked the script candidates:

| Candidate | Correction |
|---|---|
| `scripts/proof/acceptance_smoke.py` | **KEEP.** It is invoked by `proof/contract.yaml` (`command: ["python", "scripts/proof/acceptance_smoke.py"]`) through `scripts/proof/run.py`. |
| `scripts/ingest_dependency_docs.py` | **KEEP (entry point).** It is a thin CLI wrapper over `verdict.dependency_ingest.ingest_dependency_docs`, which is library code with tests. Candidate for a `verdict` subcommand later. |
| `scripts/proof/secrets_scan.py` | **REMOVE-CANDIDATE, confirmed.** CI writes `secrets_scan_results.txt` with an inline command and never calls this script. It is left in place until the owner decides. |

`.serena/` was dropped from the proposed `.gitignore` additions because `.serena/project.yml` and
`.serena/.gitignore` are tracked.
