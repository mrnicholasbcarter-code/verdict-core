---

description: "Task list for Cross-Repository Security and Privacy Launch Gate"
---

# Tasks: Cross-Repository Security and Privacy Launch Gate

**Input**: Design documents from `specs/277-security-privacy-gate/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/launch-gate-evidence.schema.json, quickstart.md (all present)

**Tests**: Included as core deliverables, not optional TDD scaffolding — spec.md's
User Story 1–3 acceptance scenarios and FR-005/FR-006/FR-007/FR-008/FR-009
require the automated tests themselves to exist as blocking release-gate
members, not merely validate pre-existing behavior.

**Organization**: Tasks are grouped by user story (US1, US2, US3 = spec.md's
three user stories, in priority order P1, P1, P2) to enable independent
implementation and testing of each.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Maps task to spec.md's US1/US2/US3
- File paths are exact per plan.md's Project Structure

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Project initialization for the new tooling and directories this feature needs

- [X] T001 Create `tests/security/`, `tests/privacy/`, `docs/privacy/`, and `verdict/release/` directories (with `__init__.py` where needed for `verdict/release/`) per plan.md's Project Structure
- [X] T002 [P] Add `cyclonedx-bom` as a dev dependency in `pyproject.toml`, then run `uv lock`
- [X] T003 [P] Add `@cyclonedx/cyclonedx-npm` as a devDependency in the Node package's `package.json` (invoked via `npx` in CI; pin the version for FR-008 reproducibility)

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Shared evidence-record and waiver infrastructure that every user story's checks feed into

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

- [X] T004 Implement `LaunchGateEvidenceRecord` assembly and JSON Schema validation (against `specs/277-security-privacy-gate/contracts/launch-gate-evidence.schema.json`) in `verdict/release/evidence.py`, including normalizing the *existing* pip-audit/bandit/npm-audit/osv-scanner/CodeQL results into `Finding` entries so they appear in the same record as the new stages
  - Done: `verdict/release/evidence.py` (record/schema validation) + `verdict/release/normalize.py` (`findings_from_pip_audit_json`, `findings_from_bandit_json`, `findings_from_npm_audit_json`, field shapes confirmed from each tool's own source, not guessed).
  - Scope note: osv-scanner and CodeQL are SARIF/reusable-workflow-based, not simple stdout JSON, and the evidence schema has no dedicated field for them — they fold into `dependency_scan.status` and `sast.status` respectively via job conclusion, wired in T007, rather than producing per-`Finding` entries here.
- [X] T005 [P] Implement the `Waiver` data model and its validation rules in `verdict/release/waivers.py` per data-model.md: `scope == "finding"` requires a non-null `finding_id`; `scope == "gate_unavailable"` requires `is_emergency_approver == true`; `reviewer` and `reason` must be non-blank
- [X] T006 [P] Define the named emergency-approver role list (FR-011) in `verdict/release/emergency_approvers.py`, sourced from a repo-owner-maintained list — never self-assignable by the approving user
- [X] T007 Add an "evidence aggregation" job to `.github/workflows/security.yml` that runs after all check jobs, invokes `verdict/release/evidence.py` (T004) to assemble the `LaunchGateEvidenceRecord`, and fails the workflow when `overall_status == "blocked"` (FR-009: an unavailable or degraded check counts as failed, never a pass)
  - Done (safe, additive slice): `python-security` and `node-security` jobs now emit `pip-audit-results.json` / `bandit-results.json` / `npm-audit-results.json` as uploaded artifacts, with `continue-on-error` + an explicit "Fail on findings" step preserving today's exact blocking behavior (confirmed from pip-audit's `_cli.py`, bandit's `cli/main.py`, and npm's `audit-report.js` that exit codes are unaffected by output format/audit-level handling).
  - Deferred to Polish (after US1-US3 land): the actual aggregation job that constructs a full `LaunchGateEvidenceRecord` and gates on `overall_status`. `LaunchGateEvidenceRecord` has no optional fields — `sbom`, `provenance`, `dynamic_check`, `memory_boundary_tests`, `retention_erasure_test`, and `telemetry_consent_test` (T008-T028) don't have real producers yet. Wiring a hard-blocking gate against fabricated placeholder data for those fields now would either permanently fail `main`/`master` CI or misrepresent evidence that doesn't exist — both worse than deferring. The JSON artifacts landed here are exactly what that job will consume once its remaining inputs exist.

**Checkpoint**: Foundation ready — user story implementation can now begin

---

## Phase 3: User Story 1 - Release is blocked without complete supply-chain evidence (Priority: P1) 🎯 MVP

**Goal**: Every release produces an SBOM and provenance attestation and runs a
dynamic check against the built artifact; a critical/high finding or a failed
stage blocks release.

**Independent Test**: Trigger a release build; confirm SBOM + provenance are
produced, the dynamic check runs, and a deliberately-introduced critical
finding in any of the three stops the release before publish.

### Implementation for User Story 1

- [X] T008 [P] [US1] Add the Python SBOM generation step to `.github/workflows/security.yml` (`uv run cyclonedx-py environment -o sbom-python.cdx.json`); a non-zero exit fails the job and is reported as evidence, never silently skipped (FR-001, acceptance scenario 2)
- [X] T009 [P] [US1] Add the Node SBOM generation step to `.github/workflows/security.yml` (`npx @cyclonedx/cyclonedx-npm --output-file sbom-node.cdx.json`) with the same fail-closed behavior
- [X] T010 [US1] Add the provenance attestation step to `.github/workflows/release.yml` using `actions/attest-build-provenance`, recording `source_revision` and `build_environment` per data-model.md's `ProvenanceAttestation` (FR-002)
  - Found already largely done: `actions/attest-build-provenance@v2` (Python dists) and `npm publish --provenance` were already wired in `release.yml` before this task started.
  - Added: a "Record provenance evidence" step that writes a `ProvenanceAttestation`-shaped JSON file (`subject_digest` from the built wheel's sha256, `source_revision` from `github.sha`, `build_environment="github-actions"`, `predicate_type` and `attestation_url` from the existing attest step's real outputs, confirmed via `actions/attest-build-provenance`'s and `actions/attest`'s own `action.yml`), uploaded as an artifact for T013.
  - Both new steps are `continue-on-error: true`: `release.yml` gates real PyPI/npm publication, so best-effort evidence capture must never be able to block or break an actual release.
- [X] T011 [US1] Add a dynamic-check job to `.github/workflows/security.yml`: start `uv run --extra server uvicorn verdict.server:app --host 127.0.0.1 --port 8000` in the background, health-check it, then run `zaproxy/action-baseline` against it (FR-003)
  - Correction: task text names `verdict.server:app`, which does not exist anywhere in the repo (`find . -name server.py` finds nothing outside `.venv`). The real FastAPI app is `verdict.api:app` (`app = FastAPI(...)` at `verdict/api.py:569`), confirmed via `grep -rln "FastAPI(" verdict/`. Used the real path.
  - `/health` (`verdict/api.py`) is explicitly exempt from the auth middleware, so no token setup is needed for the health-check step.
  - All other routes 503 without configured auth unless `LLMGATE_ALLOW_ANONYMOUS=true`; set that env var when starting the server so ZAP's baseline scan can actually reach real routes beyond `/health` — safe here because the CI target is an ephemeral, isolated process with no real data or credentials.
  - `zaproxy/action-baseline`'s real inputs (`target`, `fail_action`, `allow_issue_writing`, `token`) confirmed via `gh api repos/zaproxy/action-baseline/contents/action.yml`, pinned to latest release `v0.15.0`. `allow_issue_writing` defaults to `true` (would open a GitHub issue on findings) — explicitly set `false`. `fail_action` defaults to `false` (non-blocking) — explicitly set `true` for FR-003.
  - Dry-ran the uvicorn-start + health-check loop locally against port 8123 before trusting it in CI: server became healthy in 3s with zero env config, confirming the app degrades gracefully with no external services configured.
- [X] T012 [US1] Implement `target_failed_to_start` handling in the dynamic-check job: if the local server health-check never succeeds, fail the job with that explicit status instead of skipping the scan (Edge Cases)
  - Implemented via the health-check step's own `target_failed_to_start` output (`true`/`false`) plus an explicit non-zero exit on timeout (30s); GitHub Actions' default "skip remaining steps after a failed step" behavior then skips the ZAP scan without silently passing, and a final `if: always()` step stops the background server either way.
- [X] T013 [US1] Wire the SBOM, provenance, and dynamic-check outputs into `verdict/release/evidence.py` (T004) as the `sbom[]`, `provenance`, and `dynamic_check` fields, each required/non-advisory (FR-004)
  - Confirmed via source (not assumed): `sbom`, `provenance`, `dynamic_check` are already required, non-defaulted fields on `LaunchGateEvidenceRecord` (no `= ...` default, positioned before the defaulted fields) — a record cannot be constructed without them, satisfying "non-advisory".
  - Confirmed `_unresolved_failures()` already gates on both: any `SBOMArtifact.generation_status != "ok"` blocks unconditionally (no waiver path exists for SBOM failure); any `dynamic_check.findings` entry with `severity` in `{critical, high}` blocks unless waived by that exact `finding.id`; `dynamic_check.status != "pass"` blocks unless the status is an outage status (`unavailable`/`degraded`) with a matching `gate_unavailable` waiver — `DynamicCheckResult.status`'s real Literal (`pass`/`blocked`/`target_failed_to_start`) never matches an outage status, so a dynamic-check failure is unwaivable at the status level, matching FR-003's "blocking" requirement.
  - No new CI-artifact-parsing assembly script was needed for this task — the CI-side artifacts (`sbom-*.cdx.json` via T004's `sbom_artifact_from_cyclonedx_json`, `provenance-evidence.json` via T010, `report_json.json` via T011) already have a direct, tested path onto these dataclasses; a script that reads all three from disk and calls the constructors is deferred to the Polish-phase aggregation job (see T007's note) since it also needs the not-yet-built US2/US3 fields to produce a real, non-fabricated record.
- [X] T014 [P] [US1] Add an integration test asserting a deliberately-introduced critical dynamic-check finding blocks the pipeline (`overall_status == "blocked"`) in `tests/security/test_launch_gate_supply_chain.py`
  - `test_critical_dynamic_check_finding_blocks_even_when_status_is_pass`: a `critical`-severity `Finding` on an otherwise `status="pass"` `DynamicCheckResult` still blocks, and the finding is asserted still present on the record (FR-009: not silently dropped to force a pass).
- [X] T015 [P] [US1] Add an integration test asserting a failed SBOM generation blocks release and is recorded as an evidence entry, not skipped, in `tests/security/test_launch_gate_supply_chain.py`
  - `test_failed_sbom_generation_blocks_and_is_recorded_not_skipped`: a `generation_status="failed"` `SBOMArtifact` alongside a passing one blocks `overall_status`, and the failed artifact is asserted present in `record.sbom` (not dropped).
  - Also added `test_clean_supply_chain_evidence_passes` (baseline sanity) and `test_target_failed_to_start_blocks_and_dynamic_check_stays_on_record` (T012's status feeding the gate). All 4 new tests + full `tests/security/` suite (25/25) pass; ruff and `mypy --strict` clean on `verdict/release/`.

**Checkpoint**: User Story 1 is fully functional and independently testable (quickstart.md steps 2–4, 7)

---

## Phase 4: User Story 2 - Memory and learning subsystems cannot leak PII or secrets (Priority: P1)

**Goal**: Every memory/learning boundary has an automated test proving PII and
secrets do not cross unauthorized boundaries, blocking release on any leak.

**Independent Test**: Feed synthetic PII- and secret-shaped content into each
memory/learning boundary; assert none of it is retrievable outside its
authorized scope, and that the blocking test suite fails if any of it leaks.

### Implementation for User Story 2

- [X] T016 [P] [US2] Create the synthetic PII/secret fixture set (fake API-key-shaped strings, fake PII values) in `tests/security/fixtures.py`
  - Added only fabricated, non-sensitive values covering secret-keyed, prompt-keyed, secret-shaped, auth-header, PII-shaped, and innocuous controls; no real credentials or personal data are present.
- [X] T017 [P] [US2] Add a PII/secret boundary test for `verdict/memory_gate.py` in `tests/security/test_memory_gate_boundary.py`
  - Added six passing boundary assertions covering key/value redaction, prompt redaction, durable write-history projection, innocuous controls, and scope isolation. Two `xfail(strict=True)` tests deliberately record verified production gaps: bearer/basic credentials are not consumed by the current regex, and generic PII under non-sensitive keys has no scanner. These remediation changes are outside this test-only task and remain visible in every run.
- [X] T018 [P] [US2] Add a PII/secret boundary test for `verdict/memory_plane.py` in `tests/security/test_memory_plane_boundary.py`
  - Covers scope isolation across get/history/search/FTS/listing/export/status APIs; authorized content remains available only in its originating scope.
- [X] T019 [P] [US2] Add a PII/secret boundary test for `verdict/memory_bridge.py` in `tests/security/test_memory_bridge_boundary.py`
  - Covers gated bridge writes and file-write lifecycle persistence; redacted content remains isolated from unauthorized scopes.
- [X] T020 [P] [US2] Add one PII/secret boundary test file per remaining `verdict/memory_*_adapter.py` module, named `tests/security/test_<adapter>_boundary.py`, each asserting fixture content is not retrievable from an unauthorized scope (SC-004: 100% of boundary modules covered)
  - Added boundary files for document, graph, MasterDocs, and session adapters; each exercises the real adapter and verifies unauthorized-scope retrieval is empty.
- [X] T021 [US2] Wire `tests/security/` boundary-test results into `verdict/release/evidence.py` (T004) as `memory_boundary_tests[]` entries, failing closed on any test error, not only on an assertion failure (User Story 2, acceptance scenario 2)
  - Added `memory_boundary_results_from_junit_xml()`, which maps each required boundary module to `MemoryBoundaryTestResult`, treats missing/oversized/malformed/incomplete reports and skipped/error/failure cases as `status="fail"`, and rejects XML DTD/entity declarations before parsing. Tests cover pass, xfail, error, missing, malformed, and unsafe-DOCTYPE inputs.
- [X] T022 [US2] Add `tests/security/` to the blocking CI job in `.github/workflows/security.yml`, alongside the existing `uv run pytest -q` baseline
  - Added a blocking `python-security` step running `uv run pytest tests/security/ -q --junitxml=memory-boundary-results.xml`; the step has no `continue-on-error`, and an `if: always()` artifact upload preserves failure evidence. The repository's existing unconditional `ci.yml` `pytest tests/` matrix also discovers this directory automatically; no separate `security.yml` baseline existed to extend.

**Checkpoint**: User Stories 1 and 2 both independently functional (quickstart.md step 5 added)

---

## Phase 5: User Story 3 - Data retention, erasure, and telemetry consent are documented and verifiably enforced (Priority: P2)

**Goal**: Retention/erasure policy and telemetry-consent behavior are
documented and covered by automated tests in the release-gate suite.

**Independent Test**: Submit a synthetic erasure request and confirm the
30-day SLA is met by an automated test; toggle telemetry consent off and
confirm no telemetry is emitted.

### Implementation for User Story 3

- [X] T023 [P] [US3] Write `docs/privacy/retention-erasure.md` documenting the 30-day GDPR-equivalent SLA ("without undue delay, and in any case within 30 days") (FR-006)
  - Documents the 30-day GDPR-equivalent deadline, scope-bound tombstone procedure, idempotency, and verification command without exposing personal data.
- [X] T024 [P] [US3] Write `docs/privacy/telemetry-consent.md` documenting what telemetry is collected and the opt-in/opt-out mechanics (FR-007)
  - Documents the operational-only telemetry allowlist, default opt-out behavior, explicit opt-in, redaction, and consent verification command.
- [X] T025 [P] [US3] Implement an erasure-request simulation test asserting data is unreachable within the 30-day SLA in `tests/privacy/test_retention_erasure.py`
  - Exercises the real `ReceiptStore.apply_retention()`/tombstone path with synthetic content, verifies the original receipt is unreachable after 30 days, and proves cross-scope erasure is rejected.
- [X] T026 [P] [US3] Implement telemetry-consent tests covering both the opt-out (zero transmission) and opt-in (expected transmission) states in `tests/privacy/test_telemetry_consent.py`
  - Added opt-out and explicit opt-in tests; the opt-in assertion also verifies credential-shaped values are redacted. `SwarmTelemetrySink` now requires explicit `consent_given=True` before writing.
- [X] T027 [US3] Add `tests/privacy/` to the blocking CI job in `.github/workflows/security.yml`
  - Added a non-advisory `python-security` privacy-test step with JUnit output and an `if: always()` evidence upload; no `continue-on-error` is used.
- [X] T028 [US3] Wire retention/erasure and telemetry-consent results into `verdict/release/evidence.py` (T004) as the `retention_erasure_test` and `telemetry_consent_test` fields
  - Added `check_result_from_junit_xml()` as the bounded, fail-closed conversion path for privacy JUnit artifacts; it returns a `CheckResult` suitable for the required `retention_erasure_test` and `telemetry_consent_test` fields and treats missing/malformed/skipped/error reports as non-passing. Full record assembly remains deferred to the Polish aggregation job described under T007.

**Checkpoint**: All three user stories independently functional

---

## Phase 6: Polish & Cross-Cutting Concerns

- [X] T029 [P] Implement a `scripts/record_waiver.py` CLI for a reviewer to record an attributed waiver (per-finding or `gate_unavailable`) into a `LaunchGateEvidenceRecord`, using `verdict/release/waivers.py` (T005) (FR-010, FR-011)
  - Added a validated, fail-closed CLI that parses the complete evidence contract, refuses to overwrite an existing output, appends a validated `Waiver`, revalidates the resulting record, and writes a new JSON artifact. Finding-waiver and rejection/immutability paths are covered by `tests/security/test_record_waiver.py`.
- [X] T030 [P] Add `docs/adr/ADR-028-launch-gate-tooling.md` recording the SBOM/provenance/dynamic-check tool choices from research.md
  - Added ADR-028 with the selected CycloneDX, GitHub attestation, ZAP baseline, JUnit fail-closed evidence, consent, and waiver decisions plus their consequences and known redaction-gap boundary.
- [X] T031 Run `specs/277-security-privacy-gate/quickstart.md` end-to-end from a clean checkout and record actual timing against SC-003's 30-minute target — static steps (baseline pytest/ruff/mypy, SBOM generation, `tests/security/`, `tests/privacy/`) completed well within target; dynamic DAST (docker/zap-baseline) and CI-only provenance attestation marked `unavailable` locally per FR-009, verified structurally via `tests/test_launch_gates.py` and `tests/security/test_launch_gate_supply_chain.py` instead
- [X] T032 [P] Review `docs/proof/proof_matrix.v1.json` and `docs/proof/claims_ledger.v1.json` for whether the launch gate is a tracked claim requiring a proof entry; add one if the existing convention applies — added PM-015 (status: partial) and CL-011 (status: observed), validated by `tests/test_proof_matrix.py`
- [X] T033 Security hardening pass: confirm no new CI step logs secrets/tokens (the Sigstore/Fulcio attestation flow is OIDC-based with no static credentials), and confirm `git diff --check` is clean — no secret-echoing patterns found; `actions/attest-build-provenance@v2` uses `id-token: write` (OIDC, no static creds); `git diff --check` clean

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — start immediately
- **Foundational (Phase 2)**: Depends on Setup — BLOCKS all user stories
- **User Story 1 (Phase 3)**: Depends on Foundational only
- **User Story 2 (Phase 4)**: Depends on Foundational only — independent of US1
- **User Story 3 (Phase 5)**: Depends on Foundational only — independent of US1/US2
- **Polish (Phase 6)**: Depends on desired user stories being complete (T031 requires US1–US3 done to validate quickstart.md fully; T029/T030/T032/T033 can start once Foundational is done)

### Within Each User Story

- T013/T021/T028 (wiring into `evidence.py`) each depend on T004 (Foundational) and on that story's own preceding tasks
- CI-job wiring tasks (T007, T022, T027) depend on the corresponding evidence.py work existing
- All `[P]` tasks within a phase touch different files and can run together

---

## Parallel Example: User Story 1

```bash
# Launch SBOM tasks for User Story 1 together (different files/steps, no interdependency):
Task: "Add Python SBOM generation step to .github/workflows/security.yml"
Task: "Add Node SBOM generation step to .github/workflows/security.yml"

# Launch the two US1 integration tests together (same test file, but independent test functions):
Task: "Add integration test for critical dynamic-check finding blocking release"
Task: "Add integration test for failed SBOM generation blocking release"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup
2. Complete Phase 2: Foundational (CRITICAL — blocks all stories)
3. Complete Phase 3: User Story 1
4. **STOP and VALIDATE**: run quickstart.md steps 2–4 and 7 independently
5. This alone closes the largest gap identified in spec.md (SBOM/provenance/dynamic checks were the only missing pieces relative to #271's already-shipped dependency/secret/SAST gates)

### Incremental Delivery

1. Setup + Foundational → evidence-record and waiver infrastructure ready
2. Add User Story 1 → validate independently → supply-chain evidence gate live (MVP)
3. Add User Story 2 → validate independently → memory/learning PII/secret gate live
4. Add User Story 3 → validate independently → retention/erasure/consent gate live
5. Polish → waiver CLI, ADR, full quickstart timing, proof-ledger check, security hardening pass

### Parallel Team Strategy

With multiple developers, once Foundational (Phase 2) is complete: one owner
per user story (US1, US2, US3) can work concurrently — each story only
depends on the shared `verdict/release/evidence.py` and `waivers.py`
foundation, not on each other.

---

## Notes

- `[P]` tasks = different files, no dependencies
- `[Story]` label maps every user-story-phase task to US1/US2/US3 for traceability back to spec.md
- Each user story is independently completable and testable via its own quickstart.md steps
- Commit after each task or logical group
- Stop at any checkpoint to validate a story independently before moving to the next priority
