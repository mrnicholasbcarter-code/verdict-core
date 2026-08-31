# Verdict 20K+ Flagship Acceptance Gates

This document defines measurable, auditable acceptance criteria for the Verdict flagship release.
Each gate MUST have evidence linked in the evidence bundle.

## Gate Categories

### G1: Core Routing Correctness
| ID | Criterion | Evidence Required |
|----|-----------|-------------------|
| G1.1 | Eligibility gate runs FIRST (before any ranking) | Test: `test_eligibility_runs_first` |
| G1.2 | Hard safety floors enforced: capability, budget, privacy, capacity | Tests: `test_capability_floor`, `test_budget_floor`, `test_privacy_floor`, `test_capacity_floor` |
| G1.3 | Intelligence is ADVISORY ONLY - cannot override gate exclusions | Test: `test_intelligence_cannot_bypass_gate` |
| G1.4 | Deterministic selection for identical inputs | Test: `test_deterministic_selection` |
| G1.5 | Explainability exposes: observed_at, expires_at, age, source, confidence, candidate/eligible counts, per-candidate exclusion reasons, cache refresh/error state | Test: `test_explain_output_schema` |

### G2: Availability & Freshness
| ID | Criterion | Evidence Required |
|----|-----------|-------------------|
| G2.1 | Bounded cache with TTL + stale-while-revalidate | Test: `test_cache_ttl_swr` |
| G2.2 | Explicit `unknown`/`error` states (not silent fallback) | Test: `test_unknown_error_states` |
| G2.3 | Concurrent refresh deduplication | Test: `test_refresh_deduplication` |
| G2.4 | Isolation by provider/model/policy-version | Test: `test_isolation_keys` |

### G3: Cost & Latency Optimization
| ID | Criterion | Evidence Required |
|----|-----------|-------------------|
| G3.1 | Least-cost eligible model selected (not cheapest overall) | Test: `test_least_cost_eligible` |
| G3.2 | Escalation only on capability/verification failure | Test: `test_escalation_policy` |
| G3.3 | Per-task token/USD budgets enforced | Test: `test_budget_enforcement` |
| G3.4 | Concurrency caps and timeout enforced | Test: `test_concurrency_timeout` |

### G4: TypeScript Parity
| ID | Criterion | Evidence Required |
|----|-----------|-------------------|
| G4.1 | Shared contracts package (@bodanglin/verdict-contracts) used by both Python and TypeScript | Evidence: `package.json` dependencies, import statements |
| G4.2 | TaskSpec, RoutingDecision, AvailabilitySnapshot, RuntimeCandidate identical | Evidence: `contract_parity_matrix.md` |
| G4.3 | Python/TypeScript fixtures produce semantically equivalent decisions | Evidence: `parity_fixture_results.json` |
| G4.4 | OpenAI-compatible forwarding middleware with SSE parity | Evidence: `forwarder.test.ts` test results |

### G5: Security & Supply Chain
| ID | Criterion | Evidence Required |
|----|-----------|-------------------|
| G5.1 | Threat model documented (STRIDE) | Evidence: `THREAT_MODEL.md` |
| G5.2 | Privacy policy: no PII in logs, data retention documented | Evidence: `PRIVACY_POLICY.md` |
| G5.3 | Supply chain scanning in CI (pip-audit, npm audit, osv-scanner) | Evidence: CI workflow with scan steps |
| G5.4 | No secrets in code or examples | Evidence: `secrets_scan_results.txt` |

### G6: Observability & Evidence
| ID | Criterion | Evidence Required |
|----|-----------|-------------------|
| G6.1 | Per-assignment logging: model, provider, availability snapshot, estimated/actual cost, reason, fallback, verification result | Evidence: `assignment_log_schema.json` + sample logs |
| G6.2 | Reproducible benchmark harness outputs JSON | Evidence: `benchmark_results.json` |
| G6.3 | Evidence bundle script collects all artifacts | Evidence: `evidence_bundle.tar.gz` |
| G6.4 | CI gate fails if any criterion lacks evidence | Evidence: CI workflow with evidence check |

### G7: Release Quality
| ID | Criterion | Evidence Required |
|----|-----------|-------------------|
| G7.1 | Clean install from PyPI/npm succeeds | Evidence: `quickstart_test.log` |
| G7.2 | Flagship demo runs without credentials | Evidence: `flagship_demo_output.json` |
| G7.3 | README claims match observed behavior | Evidence: `readme_verification.log` |
| G7.4 | Issue templates for bug/feature/security | Evidence: `.github/ISSUE_TEMPLATE/` |

## Evidence Bundle Structure

```
evidence_bundle/
├── gates_status.json          # Gate ID → PASS/FAIL + evidence path
├── test_results.xml           # Full pytest/JUnit output
├── benchmark_results.json     # Cost/latency/quality/availability
├── contract_parity_matrix.md  # Python/TS field comparison
├── security_scans/            # pip-audit, npm audit, osv-scanner
├── assignment_logs/           # Sample assignment logs
├── benchmark_results.json     # Benchmark harness output
├── evidence_bundle.py         # Generation script
└── README.md                  # Bundle explanation
```

## CI Integration

`.github/workflows/acceptance-gates.yml` runs these gates against evidence it
generates. It is not a pull-request check: launch readiness is expected to be
incomplete before launch, and a non-passing gate must not block ordinary
development. It runs on `workflow_dispatch`, weekly, and on every published
release, where a non-passing gate is a real blocker.

The pipeline is producers, then report, then verify:

| Step | Produces | Gates it feeds |
| --- | --- | --- |
| `pytest tests/ --junitxml=evidence/pytest_results.xml` | `pytest_results.xml` | G1.1-G3.4 |
| `scripts/benchmark.py --output evidence/benchmark_results.json` | `benchmark_results.json` | G6.2 |
| `scripts/flagship_demo.py > evidence/flagship_demo_output.json` | `flagship_demo_output.json` | G7.2 |
| `scripts/quickstart.py \| tee evidence/quickstart_test.log` | `quickstart_test.log` | G7.1 |
| committed-credential scan `\| tee evidence/secrets_scan_results.txt` | `secrets_scan_results.txt` | G5.4 |
| `scripts/evidence_bundle.py --evidence-dir evidence --output-dir evidence_bundle` | `evidence_manifest.json` | G6.3 |
| `scripts/generate_gates_report.py --evidence-dir evidence` | `gates_status.json`, `notes/`, `derived/` | all |
| `scripts/verify_gates.py --evidence-dir evidence --json` | the pass/fail decision | all |

Every producer step is `continue-on-error: true` on purpose. A missing artifact
is exactly what the verifier exists to report; letting a crashed producer abort
the job would turn "no evidence" into "no answer". The report and verify steps
are not advisory, so a non-passing gate fails the run.

`scripts/generate_gates_report.py` decides each gate from evidence alone. A
test-backed gate passes only when the named test is present in the JUnit report
and every matching case passed; an artifact-backed gate passes only when its
named file is a real regular file in the evidence directory; a derived gate
(G4.1, G5.3, G6.4, G7.4) passes only when the repository content it inspects
satisfies the documented condition. Everything else is `BLOCKED` (no evidence)
or `FAIL` (evidence present and negative). It never upgrades a missing
criterion, and it writes a `notes/<gate>.md` record of what it inspected so a
blocked gate still cites real evidence instead of producing an unverifiable
report.

Reproduce the whole check locally:

```bash
mkdir -p evidence
pytest tests/ -q --junitxml=evidence/pytest_results.xml
python scripts/generate_gates_report.py --evidence-dir evidence
python scripts/verify_gates.py --evidence-dir evidence --json
```

## Verification

The checked-in validator enforces the machine-readable report contract before
any release decision is made:

```bash
python scripts/verify_gates.py --evidence-dir evidence --json
```

`evidence/gates_status.json` must contain schema version `1`, the
audited repository and commit, and exactly one entry for every `G1.1` through
`G7.4` criterion. Each entry has a `PASS`, `FAIL`, or `BLOCKED` status and at
least one evidence-root-relative regular-file evidence path. Missing, unknown,
duplicate, absolute, traversal, symlinked, or malformed report/evidence causes
verification to fail. A report containing any `FAIL` or `BLOCKED` gate also
exits nonzero. The validator is intentionally evidence-neutral: it checks the
report and referenced artifacts, but never invents evidence or upgrades a
missing criterion.

See `scripts/verify_gates.py` and `tests/test_verify_gates.py` for the
versioned contract and fail-closed cases.
