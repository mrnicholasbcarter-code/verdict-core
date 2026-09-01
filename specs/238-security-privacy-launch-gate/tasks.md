---

description: "Task list for feature implementation"
---

# Tasks: Cross-Repository Security and Privacy Launch Gate

**Input**: Design documents from `specs/238-security-privacy-launch-gate/`

**Prerequisites**: [plan.md](./plan.md), [spec.md](./spec.md), [research.md](./research.md), [data-model.md](./data-model.md), [contracts/](./contracts/)

**Tests**: Included. The specification requires enforcement by failing checks rather than by
prose (FR-012, SC-006), and Constitution IV makes verification part of the change. Every
requirement below lands with something that fails when the property is absent.

**Organization**: Tasks are grouped by user story so each story can be implemented and
tested independently.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (US1, US2, US3)
- Include exact file paths in descriptions

## Path Conventions

Single Python project at repository root: `verdict/`, `scripts/`, `tests/`,
`.github/workflows/`, `contracts/`. The `verdict-node` half is a **separate repository and
a separate pull request** — see Phase 7.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Make the environment reproducible and confirm the baseline before changing it

- [ ] T001 Sync the development environment with `uv sync --extra dev --extra server` and confirm `./.venv/bin/pytest -q` passes, recording the baseline pass count
- [ ] T002 Run the full verification baseline (`uv run pytest -q`, `ruff check .`, `ruff format --check .`, `mypy verdict --strict`, `git diff --check`) and confirm `git status` is clean afterwards, so any later dirty state is attributable
- [ ] T003 [P] Declare `pip-audit` and `bandit` as pinned entries in the `dev` extra of `pyproject.toml`, replacing the unversioned ad-hoc install at `.github/workflows/security.yml:33`
- [ ] T004 [P] Refresh `uv.lock` for the new `dev` entries and confirm the lockfile change is the only side effect

**Checkpoint**: Baseline recorded, security tooling is a declared and locked dependency rather than a floating install

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The severity policy and the exception mechanism. Every check in every story
reads these, so nothing else can start until they exist.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

- [ ] T005 Add the severity vocabulary and ordering (`low` < `medium` < `high` < `critical`) to `verdict/security.py`, with an `unmappable severity` path that resolves toward blocking, never below the threshold
- [ ] T006 [P] Add per-tool severity normalisation to `verdict/security.py` mapping `pip-audit`, `bandit`, `npm audit`, and OSV vocabularies into the shared enum, per [data-model.md](./data-model.md#finding)
- [ ] T007 [P] Write `contracts/security-exceptions.schema.json` in the repository root `contracts/` directory, copied from [contracts/security-exceptions.schema.json](./contracts/security-exceptions.schema.json)
- [ ] T008 Implement the exception evaluator in `verdict/security.py`: load the tracked exception file, validate against the schema with `jsonschema`, and resolve absent, schema-invalid, and expired entries all to "no exception" (FR-005d, FR-005e, FR-005f)
- [ ] T009 [P] Add tests in `tests/test_security.py` for severity normalisation, including an unmappable severity that must be treated as at or above the threshold
- [ ] T010 [P] Add tests in `tests/test_security.py` for the three exception failure modes — file absent, file schema-invalid, entry expired — each asserting the finding still blocks and that an invalid file is itself reported
- [ ] T011 Extend `verdict/compatibility_manifest.py` to schema version `2`, adding `security_policy` with `blocking_severity` and `policy_version` **inside the hashed region**, per [contracts/compatibility-manifest-v2.md](./contracts/compatibility-manifest-v2.md)
- [ ] T012 Add the version-2 rejection cases to `verdict/compatibility_manifest.py`: a version-1 manifest, an absent `security_policy`, and an unrecognised `blocking_severity` are each rejected rather than defaulted
- [ ] T013 [P] Extend `tests/test_compatibility_manifest.py` with round-trip coverage for version 2 and each rejection case, asserting the hash covers the policy so a manifest cannot be re-emitted at a weaker threshold
- [ ] T014 Assert in `tests/test_compatibility_manifest.py` that **no** new rejection branch was added for the "version-1 reader, version-2 manifest" case — `__post_init__` already raises on any `schema_version != "1"`, and a second path around a working guard weakens it
- [ ] T015 Wire `verdict compat manifest` and `verdict compat check` into `.github/workflows/ci.yml` as non-advisory steps, completing the CI half ADR-024 left open (FR-024)
- [ ] T016 [P] Add a test in `tests/test_runtime_compatibility.py` asserting the compat step exists in a workflow and carries neither `continue-on-error` nor `|| true`

**Checkpoint**: One threshold exists, is carried across the repository boundary, and cannot be waived except through a schema-validated, expiring entry

---

## Phase 3: User Story 1 — A release cannot ship an unresolved critical finding (Priority: P1) 🎯 MVP

**Goal**: A finding at or above the declared severity stops the release. It does not warn.

**Independent Test**: Introduce a finding at the declared severity and confirm the release
workflow fails; lower it below the threshold and confirm the release proceeds. Confirm no
security step carries `continue-on-error` or `|| true`.

### Tests for User Story 1

- [ ] T017 [P] [US1] Add a test in `tests/test_generate_gates_report.py` asserting `_is_advisory` classifies each new security step as enforcing, so an advisory step cannot masquerade as a gate
- [ ] T018 [P] [US1] Add a test in `tests/test_release_workflow.py` asserting every third-party `uses:` reference across `.github/workflows/` resolves to an immutable revision and none to a moving branch
- [ ] T019 [P] [US1] Add a test in `tests/test_release_workflow.py` asserting a bill of materials is generated for every published artifact and that each carries a provenance attestation

### Implementation for User Story 1

- [ ] T020 [US1] Replace the three divergent thresholds in `.github/workflows/security.yml` — `pip-audit --local` (blocks on any advisory), `bandit -ll` (medium), `npm audit --audit-level=high` (high) — with the single declared severity read from the policy (FR-003)
- [ ] T021 [US1] Align `osv-scanner.toml` with the declared severity so the fourth check does not carry a fourth threshold
- [ ] T022 [P] [US1] Pin every third-party action reference to an immutable commit revision with the human-readable version in a trailing comment, across all ten files in `.github/workflows/` (FR-005a)
- [ ] T023 [US1] Replace `pypa/gh-action-pypi-publish@release/v1` in `.github/workflows/release.yml` with an immutable revision — it is the one reference on a moving branch, on the publishing step
- [ ] T024 [US1] Update `tests/test_release_workflow.py::test_release_workflow_publishes_the_attested_python_artifacts_once`, which currently asserts the literal `uses: pypa/gh-action-pypi-publish@release/v1` and will fail once T023 lands
- [ ] T025 [US1] Add a pinning check step to `.github/workflows/security.yml` that fails when any third-party reference is not immutable (FR-005b, FR-005c)
- [ ] T026 [P] [US1] Add CycloneDX bill-of-materials generation for the Python distribution to `.github/workflows/release.yml`, written into the evidence directory (FR-006)
- [ ] T027 [P] [US1] Add CycloneDX bill-of-materials generation for each published npm package to `.github/workflows/release.yml` (FR-006)
- [ ] T028 [US1] Extend the existing `actions/attest-build-provenance` usage in `.github/workflows/release.yml` to attest the bills of materials alongside the distributions (FR-007, FR-008)
- [ ] T029 [P] [US1] Add history secret scanning to `.github/workflows/security.yml` as a pinned scanner, distinct from the existing committed-file credential check
- [ ] T030 [US1] Add dynamic verification of the optional server surface in `tests/test_server_dynamic.py`, starting the app in-process and asserting it refuses malformed and hostile input rather than crashing or leaking (FR-009, FR-010)
- [ ] T031 [US1] Extend the `_check_supply_chain_scans` needle table in `scripts/generate_gates_report.py` with rows for the bill of materials, secret scanning, the pinned-revision check, and dynamic verification, per [contracts/gate-report.md](./contracts/gate-report.md)
- [ ] T032 [US1] Update `tests/test_generate_gates_report.py` for the extended needle table, covering the missing-needle (`BLOCKED`) and all-advisory (`FAIL`) branches for each new row

**Checkpoint**: User Story 1 is fully functional — a blocking finding stops the release, one threshold governs four tools, and the tooling that decides is itself pinned

---

## Phase 4: User Story 2 — A reviewer reproduces the security and privacy evidence (Priority: P1)

**Goal**: A reviewer can read what was checked, what was found, and what was accepted,
without re-running the pipeline.

**Independent Test**: Run the gates report and confirm G5.1 and G5.2 report `PASS` rather
than `BLOCKED`, with real files present in the evidence directory.

### Tests for User Story 2

- [ ] T033 [P] [US2] Add a test in `tests/test_launch_gates.py` asserting G5.1 and G5.2 report `PASS` when the documents are present in the evidence directory and `BLOCKED` when they are not
- [ ] T034 [P] [US2] Add a test in `tests/test_generate_gates_report.py` asserting a symlinked artifact is rejected, pinning the existing `is_symlink` guard so a future copy step cannot be "optimised" into a link

### Implementation for User Story 2

- [ ] T035 [P] [US2] Write `THREAT_MODEL.md` at the repository root covering the trust boundaries, assets, adversaries, and mitigations of the control plane and its optional server surface (FR-016)
- [ ] T036 [P] [US2] Write `PRIVACY_POLICY.md` at the repository root stating what is collected, what is stored, the retention rules and their dispositions, and the no-egress guarantee (FR-017)
- [ ] T037 [US2] Add an evidence step to `.github/workflows/acceptance-gates.yml` that copies `THREAT_MODEL.md` and `PRIVACY_POLICY.md` into `evidence/` as **real files**, placed with the other evidence-generation steps and before the non-advisory generator step
- [ ] T038 [US2] Verify the copy actually moves G5.1 and G5.2 off `BLOCKED` by running `uv run python scripts/generate_gates_report.py --evidence-dir evidence` and reading `evidence/gates_status.json` — artifacts resolve against the evidence directory, not the repository root, so authoring the documents alone is not sufficient
- [ ] T039 [P] [US2] Record the review and sign-off expectations for security and privacy changes in `SECURITY.md` (FR-018, FR-019)
- [ ] T040 [US2] Confirm no gate reports `PASS` on absent evidence anywhere in `evidence/gates_status.json`, per constitution quality gate 6

**Checkpoint**: G5.1 and G5.2 are unblocked and a reviewer can reconstruct the evidence from the bundle alone

---

## Phase 5: User Story 3 — Private data cannot cross a boundary it was never meant to cross (Priority: P2)

**Goal**: Telemetry stays local, retention is enforced, and erasure and the append-only
evidence chain coexist.

**Independent Test**: Run the privacy tests offline; confirm no test needs a live endpoint,
and that the evidence chain still verifies after an erasure.

### Tests for User Story 3

- [ ] T041 [P] [US3] Add a test in `tests/test_security.py` that fails if telemetry egress is attempted, enforcing the no-egress guarantee by failing check rather than by prose (FR-012, SC-006)
- [ ] T042 [P] [US3] Add a test in `tests/test_security.py` asserting the chain still verifies after an erasure record is appended, and that what remains is non-reversible (FR-015a, FR-015b, SC-010)
- [ ] T043 [P] [US3] Add a test asserting no erasure path deletes or rewrites a prior record — a verification failure here means history was rewritten instead of appended to

### Implementation for User Story 3

- [ ] T044 [US3] Implement the governed erasure entry point composing the existing primitives — `MemoryPlane.tombstone`, `ReceiptStore.tombstone`, `ReceiptStore.apply_retention`, and the redaction helpers in `verdict/security.py`. Do **not** add a deleter; it would contradict ADR-017's ledger design
- [ ] T045 [P] [US3] Enumerate the retention rules — category, lifetime, disposition, store — in `PRIVACY_POLICY.md`, including the `retained-as-reference` disposition for non-reversible references (FR-011, FR-013, FR-014)
- [ ] T046 [US3] Confirm `ReceiptStore.apply_retention` and `adaptive_state._enforce_retention` actually implement the lifetimes the policy states, and reconcile any divergence in favour of the enforced behaviour rather than the prose
- [ ] T047 [US3] Validate erasure inputs at the boundary so a malformed erasure request is refused rather than partially applied

**Checkpoint**: All three user stories are independently functional in `verdict-core`

---

## Phase 6: Cross-Repository Parity — `verdict-node` (SEPARATE PULL REQUEST)

**Purpose**: Bring the second repository onto the same policy

**⚠️ Constitution III**: These tasks belong to a **different repository** and must not share
a commit with anything above. `verdict-core` lands and publishes the policy first.

- [ ] T048 Confirm the `verdict-core` pull request is merged and the version-2 manifest is published before starting any task in this phase
- [ ] T049 [P] Add a TypeScript reader for the manifest `security_policy` field in `verdict-node` — no mirror of `CompatibilityManifest` exists in `contracts/` or `verdict/client-sdk/` today, so this surface is new (FR-023)
- [ ] T050 Create `.github/workflows/security.yml` in `verdict-node` — it currently has `ci`, `codeql`, `lint`, and `npm-publish` and **no security workflow at all** (FR-020, FR-021)
- [ ] T051 [P] Add `verdict-node`'s own exception file and schema validation, matching [contracts/security-exceptions.schema.json](./contracts/security-exceptions.schema.json)
- [ ] T052 Pin every third-party action reference in `verdict-node/.github/workflows/` to an immutable revision (FR-022)
- [ ] T053 Assert `verdict-node` rejects a manifest whose policy it cannot parse rather than proceeding without one (FR-025)
- [ ] T054 Run `verdict-node`'s declared `package.json` scripts — build, test, lint, typecheck, package verification. Read the script names from the manifest; do not invent them, and report anything missing or skipped honestly

**Checkpoint**: Both repositories enforce one policy, landed in the correct order

---

## Phase 7: Polish & Cross-Cutting Concerns

**Purpose**: Record the decision and prove the whole thing end to end

- [ ] T055 [P] Write `docs/adr/ADR-028-*.md` recording the security and privacy launch gate decision — the latest existing ADR is 027
- [ ] T056 [P] Update ADR-024's status, which currently reads "Partially Implemented — verdict-core side complete …; downstream repo declarations and CI wiring still open", now that T015 and Phase 6 close it
- [ ] T057 Run all eight scenarios in [quickstart.md](./quickstart.md) and record the outcome of each, naming anything skipped or unavailable rather than inferring a pass
- [ ] T058 Run the full verification baseline again (`uv run pytest -q`, `ruff check .`, `ruff format --check .`, `mypy verdict --strict`, `uv run python -m build`, `git diff --check`) and confirm `git status` is clean
- [ ] T059 Confirm the test suite wrote no evidence into a tracked path — a dirty tree after the suite invalidates every gate result above it
- [ ] T060 Verify the release evidence bundle contains the bills of materials, the two documents, and `gates_status.json`, and that each gate status is bound to the exact head revision

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — start immediately
- **Foundational (Phase 2)**: Depends on Setup — **BLOCKS all user stories**, because every check reads the severity policy and the exception evaluator
- **User Stories (Phases 3–5)**: All depend on Foundational
- **Cross-repository (Phase 6)**: Depends on the `verdict-core` pull request being **merged**, not merely written
- **Polish (Phase 7)**: Depends on the desired user stories being complete

### User Story Dependencies

- **US1 (P1)**: Can start after Phase 2. No dependency on other stories.
- **US2 (P1)**: Can start after Phase 2. Independent of US1 — the documents and the evidence copy do not need the threshold work.
- **US3 (P2)**: Can start after Phase 2. Independent of US1 and US2.

### Within Each User Story

- Tests are written first and must fail before the implementation lands
- Policy and normalisation before the checks that read them
- Checks before the gate-report rows that assert them
- Story complete before moving to the next priority

### Critical Ordering Constraints

Three orderings are not negotiable and will produce silent failures if reversed:

1. **T023 before T024.** Changing the publish reference breaks an existing assertion in `tests/test_release_workflow.py` that pins the literal `@release/v1`. Landing them apart leaves a red suite.
2. **T037 before T038.** The copy step must exist before the verification that it worked. Skipping T038 is the specific way this feature ships looking complete while G5.1 and G5.2 stay `BLOCKED`.
3. **T048 gates all of Phase 6.** One commit must never span both repositories.

### Parallel Opportunities

- T003 and T004 in Setup
- T006, T007, T009, T010, T013, T016 within Foundational, once T005 lands
- T017, T018, T019 (US1 tests) together; then T022, T026, T027, T029 across different files
- T033, T034 (US2 tests) together; T035, T036, T039 are three different documents
- T041, T042, T043 (US3 tests) together
- Once Phase 2 completes, US1, US2, and US3 can proceed in parallel by three people

---

## Parallel Example: User Story 1

```bash
# Launch the User Story 1 tests together:
Task: "Assert _is_advisory classifies each new security step as enforcing in tests/test_generate_gates_report.py"
Task: "Assert every third-party uses: reference is immutable in tests/test_release_workflow.py"
Task: "Assert a bill of materials and attestation exist per artifact in tests/test_release_workflow.py"

# Then launch the independent implementation tasks together:
Task: "Pin every third-party action reference across .github/workflows/"
Task: "Add CycloneDX generation for the Python distribution in .github/workflows/release.yml"
Task: "Add CycloneDX generation for each npm package in .github/workflows/release.yml"
Task: "Add pinned history secret scanning to .github/workflows/security.yml"
```

---

## Implementation Strategy

### MVP First (User Story 1 only)

1. Complete Phase 1: Setup
2. Complete Phase 2: Foundational — **critical, blocks all stories**
3. Complete Phase 3: User Story 1
4. **STOP and VALIDATE**: quickstart Scenarios 1, 2, 3, and 5
5. At this point a blocking finding stops a release, four tools share one threshold, and the tooling is pinned — the largest share of the risk is closed

### Incremental Delivery

1. Setup + Foundational → the policy exists and crosses the repository boundary
2. Add US1 → releases are gated (MVP)
3. Add US2 → G5.1 and G5.2 unblock, evidence is reproducible
4. Add US3 → privacy and erasure guarantees are enforced by tests
5. Phase 6 in a separate pull request → `verdict-node` reaches parity
6. Phase 7 → decision recorded, whole path validated end to end

### Parallel Team Strategy

1. The team completes Setup and Foundational together — everything depends on them
2. Then: one person on US1 (the largest), one on US2, one on US3
3. Phase 6 waits for the `verdict-core` merge regardless of staffing

---

## Notes

- [P] tasks touch different files and have no incomplete dependencies
- [Story] labels map each task to a user story for traceability
- Verify tests fail before implementing
- Commit after each task or logical group; one repository per commit
- Every failure mode in this feature resolves toward blocking: absent, malformed, expired, and unparseable all mean "no exception"
- Report skipped or unavailable evidence as itself — never as a pass
