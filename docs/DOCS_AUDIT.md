# Documentation Audit — Disposition Record

**Branch line:** orchestration golden path (the orchestration golden-path documentation pass documentation pass)
**Recorded:** 2026-09-24
**Test count on this branch:** 2907 (from `pytest --collect-only -q`)
**Repository:** https://github.com/mrnicholasbcarter-code/verdict-core

Status values:

- **KEEP** — accurate on this branch; no rewrite needed.
- **REWRITTEN** — rewritten during this documentation pass to remove false or
  stale claims. Some rewrites land through parallel branches of the same pass;
  verify at merge.
- **ARCHIVED** — moved under `docs/archive/` (historical record only, not
  current product behavior).

Context for archive decisions: Ruflo, RuVector, SONA, Hivemind, and swarm
dispatch/supervision are obsolete in Core . See
[ADR-023](adr/ADR-023-governed-swarm-supervision.md) (superseded) and
[ADR-036](adr/ADR-036-goal-to-receipt-orchestration.md) (current
orchestration).

## Root files

| File | Status | Reason |
|------|--------|--------|
| README.md | REWRITTEN | ADR count and CLI table updated for ADR-036 commands (orchestrate/supervise/watch/run-receipt/eligibility) in this pass. |
| AGENTS.md | REWRITTEN | Test count corrected (321 -> 2907) in this pass. |
| CHANGELOG.md | KEEP | Version matches pyproject.toml; no stale claims. |
| CONTRIBUTING.md | KEEP | Generic contribution guidance; no stale technical claims. |
| CODE_OF_CONDUCT.md | KEEP | Standard CoC; no technical claims. |
| SECURITY.md | REWRITTEN | Broken link to nonexistent docs/specs/RELEASE_ACCEPTANCE.md replaced with ACCEPTANCE_GATES.md and docs/proof/RELEASE_BOUNDARY_0.3.0.md. |
| RELEASE_PACKAGING.md | REWRITTEN | Fixed `../SUPPORT.md` relative path; corrected fictional `release-support/` directory listing and issue-template list to match `.github/ISSUE_TEMPLATE/`. |
| RELEASE_CHECKLIST.md | KEEP | No stale claims. |
| SUPPORT.md | KEEP | Support channels; accurate. |
| VERSIONING.md | KEEP | Semver policy; no stale claims. |
| ACCEPTANCE_GATES.md | KEEP | Gate definitions consistent with CI. |
| BRANCH_REGISTRY.md | KEEP | Factual branch-tracking record. |
| CONTRACT_PARITY.md | KEEP | TypeScript parity tracking consistent with contracts/index.ts. |

## docs/ (top level)

| File | Status | Reason |
|------|--------|--------|
| docs/CLI_REFERENCE.md | REWRITTEN | Adding orchestrate/eligibility/supervise/watch/run-receipt command sections in this pass; existing commands were accurate. |
| docs/GETTING_STARTED.md | KEEP | Earlier findings (wrong GitHub URL, broken production-deployment link) already fixed on this branch. |
| docs/CONFIGURATION.md | KEEP | Config keys and env vars match the codebase. |
| docs/CAPABILITY_PASSPORTS.md | KEEP | Consistent with the eligibility passport model. |
| docs/QUALIFICATION_REPORTS.md | KEEP | Short format reference; accurate. |
| docs/THREAT_MODEL_RECEIPTS.md | KEEP | Consistent with the privacy gate. |
| docs/USER_JOURNEY.md | KEEP | Accurate maturity matrix; credential-free and simulate paths correct. |
| docs/architecture.md | REWRITTEN | Broken sub-links already removed on this branch; ADR-036 orchestration section added in this pass; SwarmDispatcher correctly labeled a historical class name. |
| docs/release-recovery.md | KEEP | Accurate partial-release recovery procedure. |
| docs/typescript-contract-parity.md | KEEP | Current TypeScript parity guide. |
| docs/BRANCH_RECONCILIATION.md | KEEP | Factual the branch/worktree/PR reconciliation pass reconciliation record. |
| docs/DOCS_AUDIT.md | KEEP | This disposition record. |

## docs/adr/

| File | Status | Reason |
|------|--------|--------|
| docs/adr/README.md | KEEP | Canonical ADR index; ADR-036 listed, ADR-023 marked SUPERSEDED. |
| docs/adr/0001-verdict-control-plane-invariants.md | KEEP | Pre-scheme invariants; short and stable. |
| docs/adr/ADR-001 … ADR-022 | KEEP | Correct Status fields; swarm/Ruflo appear only as superseded history. |
| docs/adr/ADR-023-governed-swarm-supervision.md | KEEP | Self-marked SUPERSEDED ; retained as the historical swarm record. |
| docs/adr/ADR-024 … ADR-035 | KEEP | Accurate decision records. |
| docs/adr/ADR-036-goal-to-receipt-orchestration.md | KEEP | Authoritative record for current orchestration. |
| docs/adr/ADR-ORCHESTRATOR-ROUTING.md | KEEP | Unnumbered legacy record kept for the supersession trail; index flags it for renumbering. |

## docs/architecture/

| File | Status | Reason |
|------|--------|--------|
| docs/architecture/AUTONOMOUS_DEV_LOOP.md | REWRITTEN | Queen/worker swarm step replaced with the 12-stage autodev workflow and ADR-036 orchestration; historical note points to ADR-023/ADR-036. |
| docs/architecture/FEATURE_LIFECYCLE_GATE.md | REWRITTEN | Ruflo/RuVector/OpenViking research steps removed; release-gate references repointed to RELEASE_CHECKLIST.md; historical note added. |
| docs/architecture/LIFECYCLE_HOOKS_SPECIFICATION.md | REWRITTEN | Ruflo hook target removed; hook actions and guarantees corrected to match `MemoryHookController` (`verdict/memory_bridge.py`) and `verdict hook` CLI. |
| docs/architecture/PLATFORM_AGNOSTIC_GUIDANCE_SPECIFICATION.md | REWRITTEN | Ruflo platform entry removed; aligned with ADR-003 boundary (optional, disabled by default) and `verdict/guidance.py` behavior. |
| docs/architecture/DOCTOR_AND_UNINSTALL_SPECIFICATION.md | KEEP | doctor/uninstall commands exist and match the spec. |
| docs/architecture/ADR-EVIDENCE-LEDGER.md | ARCHIVED | Duplicate of docs/adr/ADR-001-evidence-ledger.md; moved to docs/archive/architecture/. |
| docs/architecture/ADR-ORCHESTRATOR-ROUTING.md | ARCHIVED | Duplicate of docs/adr/ADR-ORCHESTRATOR-ROUTING.md with obsolete Ruflo/SONA framing; moved to docs/archive/architecture/. |
| docs/architecture/EXPANDED_TOOL_AUTOPILOT_SPECIFICATION.md | ARCHIVED | Describes Ruflo/swarm tool matrix removed by the obsolete Ruflo/swarm/hivemind architecture removal (ADR-023, superseded by ADR-035); moved to docs/archive/architecture/. |
| docs/architecture/CODE_INTELLIGENCE_GRAPH_SPECIFICATION.md | ARCHIVED | Early ADR-005 spec with RuVector claims; partially implemented; moved to docs/archive/architecture/. |
| docs/architecture/LEGACY_MEMORY_ARCHIVAL_AND_CLEANUP_SPECIFICATION.md | ARCHIVED | Completed cleanup spec; malformed regex-as-link fixed; moved to docs/archive/architecture/. |

## docs/specs/

| File | Status | Reason |
|------|--------|--------|
| docs/specs/ROUTING_POLICY.md | REWRITTEN | Swarm/Ruflo dispatch and learning-loop references replaced with ADR-036 orchestration in this pass; core gate policy was accurate. |
| docs/specs/ENFORCEMENT_AND_LEARNING.md | ARCHIVED | Describes the Ruflo/SONA learning loop removed by the obsolete Ruflo/swarm/hivemind architecture removal (ADR-023, superseded by ADR-035); moved to docs/archive/specs/ with an archival banner. |
| docs/specs/268/ (5 files) | ARCHIVED | Completed story spec artifacts; moved to docs/archive/specs/268/. |
| docs/specs/story-269/ (5 files) | ARCHIVED | Completed story spec with swarm references; moved to docs/archive/specs/story-269/. |

## docs/superpowers/ (moved to docs/archive/superpowers/)

| File | Status | Reason |
|------|--------|--------|
| plans/2026-07-22-execution-evidence.md | ARCHIVED | Historical execution-evidence plan with swarm references. |
| plans/2026-09-13-prime-workflow.md | ARCHIVED | Completed plan. |
| plans/2026-09-19-terminal-ui.md | ARCHIVED | Historical terminal UI plan. |
| specs/2026-07-22-execution-evidence-design.md | ARCHIVED | Completed design spec. |
| specs/2026-09-19-anthropic-messages-design.md | ARCHIVED | Completed design spec. |
| specs/2026-09-19-bod-9-reconciliation.md | ARCHIVED | Historical reconciliation spec. |

## docs/benchmarks/

| File | Status | Reason |
|------|--------|--------|
| docs/benchmarks/context-lift.md | KEEP | Fixture-backed benchmark; labeled as estimates. |
| docs/benchmarks/paired-savings.md | KEEP | Fixture-backed paired-savings benchmark. |
| docs/benchmarks/routing-demo.md | KEEP | Clearly labeled mock/deterministic demo. |

## docs/guides/

| File | Status | Reason |
|------|--------|--------|
| docs/guides/orchestration-golden-path.md | KEEP | Authoritative end-to-end orchestration proof; all five commands verified against `--help`. |
| docs/guides/golden-path.md | KEEP | Accurate dated live-probe evidence; orchestration-golden-path.md is canonical for orchestration. |
| docs/guides/admit-prove-confirm-smoke.md | KEEP | Commands match the CLI. |
| docs/guides/autonomous-development.md | KEEP | the obsolete Ruflo/swarm/hivemind architecture removal (ADR-023, superseded by ADR-035) note present; swarm mentions are labeled historical, not product claims. |
| docs/guides/claude-harness.md | KEEP | Accurate harness setup. |
| docs/guides/cline-harness.md | KEEP | Accurate harness setup. |
| docs/guides/codex-harness.md | KEEP | Accurate harness setup. |
| docs/guides/coding-agent-gate.md | KEEP | Accurate agent-gate setup. |
| docs/guides/comparison.md | KEEP | Accurate competitive framing. |
| docs/guides/controller-routing.md | KEEP | Accurate controller routing guide. |
| docs/guides/cursor-harness.md | KEEP | Accurate harness setup. |
| docs/guides/execution-host-contract.md | KEEP | Accurate contract description. |
| docs/guides/free-tier-admit-smoke.md | KEEP | Commands match the CLI. |
| docs/guides/governed-swarm-implementation.md | KEEP | Carries a HISTORICAL / NON-NORMATIVE banner ; retained as history. |
| docs/guides/local-development.md | KEEP | Accurate dev environment setup. |
| docs/guides/memory-outbox-mirror.md | KEEP | Consistent with ADR-034. |
| docs/guides/memory-plane-offline-verification.md | KEEP | Accurate verification guide. |
| docs/guides/model-metadata-store.md | KEEP | Accurate per ADR-032. |
| docs/guides/omniroute-workers.md | KEEP | the obsolete Ruflo/swarm/hivemind architecture removal (ADR-023, superseded by ADR-035) note present; no live swarm claims remain. |
| docs/guides/opencode-harness.md | KEEP | Accurate harness setup. |
| docs/guides/prime-harness.md | KEEP | Accurate harness setup. |
| docs/guides/prime-workflow.md | KEEP | Accurate per ADR-031. |
| docs/guides/prove-at-rest-smoke.md | KEEP | Commands match the CLI. |
| docs/guides/runtime-ownership.md | KEEP | Mentions Ruflo/claude-flow only as observed ADR-008 ownership records, which is factual. |
| docs/guides/ruvector-advisory-readiness.md | KEEP | Carries a SUPERSEDED banner; retained as history. |
| docs/guides/setup-preview.md | KEEP | Short, accurate redirect. |
| docs/guides/shared-memory-provider.md | KEEP | Accurate per ADR-033. |
| docs/guides/unknown-not-healthy.md | KEEP | Accurate fail-closed principle. |

## docs/pages/

| File | Status | Reason |
|------|--------|--------|
| docs/pages/index.mdx | REWRITTEN | Hivemind/Neural Learning feature claims removed in this pass . |
| docs/pages/hivemind.mdx | KEEP | Carries a SUPERSEDED banner; retained as history. |
| docs/pages/_meta.json | KEEP | hivemind nav entry already labeled "historical / superseded". |

## docs/patterns/, docs/portfolio/, docs/privacy/, docs/proof/

| File | Status | Reason |
|------|--------|--------|
| docs/patterns/privacy-safe-execution-evidence.md | KEEP | No live swarm claims; its ADR-001 link must be repointed to docs/archive/architecture/ (or docs/adr/ADR-001) by its owner after the archive move. |
| docs/portfolio/[REMOVED: STAR-format career narrative collection] | DELETED | Career-collateral file removed in the public-docs remediation pass. |
| docs/portfolio/AI_GATEWAY_ASSURANCE_AUDIT.md | KEEP | No stale technical claims. |
| docs/portfolio/KALSHI_TRADING_BOTS_CASE_STUDY.md | KEEP | External case study; no Verdict claims to verify. |
| docs/portfolio/[REMOVED: professional-network profile draft] | DELETED | Career-collateral file removed in the public-docs remediation pass. |
| docs/portfolio/PORTFOLIO_PROOF_MATRIX.md | KEEP | Verified claims with limitations noted. |
| docs/portfolio/PRESS_RELEASE_AND_SHOWCASE_PACKAGE.md | DELETED | Career-collateral file removed in the public-docs remediation pass. |
| docs/portfolio/RESUME_SUITE.md | DELETED | Career-collateral file removed in the public-docs remediation pass. |
| docs/portfolio/VERDICT_PROOF_CASE_STUDY.md | KEEP | Bounded, hedged proof case study. |
| docs/privacy/retention-erasure.md | KEEP | Consistent with ADR-017. |
| docs/privacy/telemetry-consent.md | KEEP | Swarm telemetry described only as deleted; boundary note present. |
| docs/proof/ADVERSARIAL_REVIEW_CHECKLIST.md | KEEP | Accurate review checklist. |
| docs/proof/CLAIMS_AUDIT_2026-09-06.md | KEEP | Self-aware claim classification audit. |
| docs/proof/EVIDENCE_INDEX.md | KEEP | Accurately cites limitations. |
| docs/proof/GOLDEN_PATH_CERTIFICATION.md | KEEP | Fresh-clone gates: 2907 tests, ruff, mypy; certified 2026-09-24. Renamed to drop career framing in the public-docs remediation pass. |
| docs/proof/[REMOVED: self-marked-superseded hardening record] | DELETED | Self-marked superseded record removed in the public-docs remediation pass. |
| docs/proof/ISSUE_455_README_EVIDENCE.md | KEEP | Swarm mentioned only as accurately framed history. |
| docs/proof/PRIVATE_LEDGER_TEMPLATE.md | KEEP | Template; no claims. |
| docs/proof/REDACTION_POLICY.md | KEEP | Accurate redaction policy. |
| docs/proof/RELEASE_BOUNDARY_0.3.0.md | KEEP | Accurate planned release boundary. |

## docs/archive/ (pre-existing entries)

All files already under `docs/archive/` before this pass remain **ARCHIVED**:
historical records retained for provenance, not current product behavior
(architecture.md, audit-2026-07-13.md, BENCHMARKS.md, continuation-runbook.md,
contracts-migration.md, DEMO.md, handoff-july-2026.md, integration-guide.md,
intelligence-adapter-protocol.md, memory-source-index.md, NAMING.md,
plan-flagship-completion.md, PROJECT_MEMORY.md, REBRANDING_PROPOSAL.md,
release-checklists.md, SECURITY_ASSURANCE.md, TOOLING_STACK_DIGEST.md). Some
archived files contain broken links and obsolete Ruflo/SONA/swarm references;
they are preserved as-is because they are historical snapshots.
