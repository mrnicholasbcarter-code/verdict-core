# Phase 1 Data Model: Cross-Repository Security and Privacy Launch Gate

**Feature**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md) | **Research**: [research.md](./research.md)

These are CI-run-scoped evidence records (files/artifacts produced per
release run), not new application persistence. Field lists are derived from
spec.md's Key Entities section plus the Functional Requirements that name
specific fields.

## LaunchGateEvidenceRecord

Per-release aggregate; the top-level artifact FR-008 requires reviewers be
able to reproduce from a clean checkout.

| Field | Type | Notes |
|---|---|---|
| `release_ref` | string | Git ref/tag or commit SHA the record is bound to |
| `generated_at` | ISO 8601 timestamp | |
| `sbom` | SBOMArtifact | one per ecosystem (python, node) — see below |
| `provenance` | ProvenanceAttestation | |
| `dynamic_check` | DynamicCheckResult | |
| `dependency_scan` | CheckResult | existing pip-audit/npm-audit/osv-scanner results, referenced not duplicated |
| `sast` | CheckResult | existing CodeQL/bandit result, referenced not duplicated |
| `memory_boundary_tests` | MemoryBoundaryTestResult[] | one per boundary module (FR-005) |
| `retention_erasure_test` | CheckResult | pass/fail + evidence link (FR-006) |
| `telemetry_consent_test` | CheckResult | pass/fail per opt-in/opt-out state (FR-007) |
| `overall_status` | enum: `pass` \| `blocked` | derived: `blocked` if any critical/high finding lacks a recorded waiver (FR-009) |
| `waivers` | Waiver[] | zero or more; see below |

**Validation rule (FR-009)**: `overall_status` MUST NOT be `pass` if any
sub-result is `unavailable`, `degraded`, or `failed` and has no matching
`Waiver`. An unavailable/degraded check is treated identically to `failed`.

## SBOMArtifact

| Field | Type | Notes |
|---|---|---|
| `ecosystem` | enum: `python` \| `node` | |
| `format` | const: `CycloneDX` | see research.md §1 |
| `format_version` | string | e.g. `1.6` |
| `file_path` | string | artifact path/URL |
| `component_count` | integer | |
| `generation_status` | enum: `ok` \| `failed` | FR-002 acceptance scenario 2: a failed generation blocks release, is not silently skipped |

## ProvenanceAttestation

| Field | Type | Notes |
|---|---|---|
| `subject_digest` | string (sha256) | digest of the release artifact the attestation covers |
| `source_revision` | string | git commit SHA (FR-002) |
| `build_environment` | string | runner OS/arch, workflow name+run id (FR-002) |
| `predicate_type` | string | in-toto/SLSA predicate type URI |
| `attestation_url` | string | GitHub attestation API reference |

## DynamicCheckResult

| Field | Type | Notes |
|---|---|---|
| `target` | string | local address the scan ran against (e.g. `http://127.0.0.1:8000`) — never a staging URL, per FR-003 |
| `scan_type` | const: `zap-baseline` | see research.md §3 |
| `findings` | Finding[] | |
| `status` | enum: `pass` \| `blocked` \| `target_failed_to_start` | FR-003/Edge Cases: a target that fails to start is a blocking failure, not a skip |

## Finding

Shared shape used by `dependency_scan`, `sast`, and `dynamic_check`.

| Field | Type | Notes |
|---|---|---|
| `id` | string | tool-assigned finding id |
| `severity` | enum: `critical` \| `high` \| `medium` \| `low` \| `info` | only `critical`/`high` are release-blocking (FR-009) |
| `title` | string | |
| `source_check` | string | which check produced it |
| `waived` | boolean | true only if a matching `Waiver` exists |

## MemoryBoundaryTestResult

| Field | Type | Notes |
|---|---|---|
| `boundary_module` | string | e.g. `verdict/memory_bridge.py` |
| `pii_leak_detected` | boolean | MUST be `false` to pass (FR-005, acceptance scenario 1) |
| `secret_leak_detected` | boolean | MUST be `false` to pass |
| `status` | enum: `pass` \| `fail` | fails closed — a run that can't complete counts as `fail`, not skipped (User Story 2, scenario 2) |

## Waiver

The single accountability mechanism for both per-finding bypass (FR-010) and
gate-infrastructure-outage bypass (FR-011).

| Field | Type | Notes |
|---|---|---|
| `scope` | enum: `finding` \| `gate_unavailable` | FR-011 reuses this same record shape for a full outage |
| `finding_id` | string, nullable | required when `scope == finding`; null when `scope == gate_unavailable` |
| `reviewer` | string | attributed identity — MUST NOT be blank (FR-010) |
| `reason` | string | free text, MUST NOT be blank |
| `recorded_at` | ISO 8601 timestamp | |
| `is_emergency_approver` | boolean | MUST be `true` when `scope == gate_unavailable`, per FR-011's named-role requirement |

**Validation rule**: A `Waiver` with `scope == gate_unavailable` and
`is_emergency_approver == false` is invalid — reject it rather than accept the
release.

## RetentionErasurePolicyRecord

| Field | Type | Notes |
|---|---|---|
| `retention_window_days` | const: `30` | GDPR-equivalent, per Clarifications session |
| `erasure_sla` | string | `"without undue delay, and in any case within 30 days"` |
| `policy_doc_ref` | string | `docs/privacy/retention-erasure.md` |
| `test_ref` | string | path to the automated test proving the SLA is honored |

## TelemetryConsentRecord

Not separately named in spec.md's Key Entities, but required by FR-007's
"document ... and MUST have an automated test verifying" — modeled here for
completeness since it's tested exactly like `RetentionErasurePolicyRecord`.

| Field | Type | Notes |
|---|---|---|
| `consent_states_tested` | enum[]: `opt_in`, `opt_out` | both MUST be covered (FR-007) |
| `policy_doc_ref` | string | `docs/privacy/telemetry-consent.md` |
| `test_ref` | string | path to the automated test |

## State transitions

`LaunchGateEvidenceRecord.overall_status` is derived, not directly settable:

```text
all sub-results pass, no unwaived critical/high findings  → pass
any sub-result failed/unavailable/degraded AND no matching Waiver → blocked
any sub-result failed/unavailable/degraded AND a matching Waiver exists → pass
                                                                          (waiver itself is always recorded, visible in .waivers[])
```

There is no transition that produces `pass` silently — every `blocked → pass`
transition requires a `Waiver` entry to exist in the same record.
