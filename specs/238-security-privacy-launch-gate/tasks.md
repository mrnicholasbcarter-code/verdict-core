---

description: "Task list for feature implementation"
---

# Tasks: Cross-Repository Security and Privacy Launch Gate

**Input**: Design documents from `specs/238-security-privacy-launch-gate/`

**Prerequisites**: [plan.md](./plan.md), [spec.md](./spec.md), [research.md](./research.md), [data-model.md](./data-model.md), [contracts/](./contracts/)

**Tests**: Included. The specification requires enforcement by failing checks rather than by
prose (FR-012, SC-006), and Constitution IV makes verification part of the change. Every
requirement below lands with something that fails when the property is absent.

**Organization**: Tasks are grouped by their primary user-story owner. Independent validation
is identified below; cross-story and shared-file dependencies are explicit so implementation
work does not imply concurrent writers.

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

- [ ] T001 Sync the development environment with `uv sync --extra dev --extra dashboard --extra server` and confirm `./.venv/bin/pytest -q` passes, recording the baseline pass count (SC-003)
- [ ] T002 Run the full verification baseline (`./.venv/bin/pytest -q`, `./.venv/bin/ruff check .`, `./.venv/bin/ruff format --check .`, `./.venv/bin/mypy verdict --strict`, `git diff --check`) and confirm the pre-existing dirty paths match the recorded pre-implementation snapshot afterwards, so any later dirty state is attributable (SC-003, Constitution IV)
- [ ] T003 Declare `pip-audit==2.10.1`, `bandit==1.9.4`, and `cyclonedx-bom==7.3.1` as pinned entries in the `dev` extra of `pyproject.toml`, replacing the unversioned ad-hoc install at `.github/workflows/security.yml:33` and adding the documented Python SBOM generator (FR-005a, FR-006)
- [ ] T004 Refresh `uv.lock` after T003 and confirm the dependency lock is the only additional side effect (FR-005a, SC-011)

**Checkpoint**: Baseline recorded, security tooling is a declared and locked dependency rather than a floating install

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The severity policy and the exception mechanism. Every check in every story
reads these, so nothing else can start until they exist.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

- [ ] T005 Add the severity vocabulary and ordering (`low` < `medium` < `high` < `critical`) to `verdict/security.py`, with an `unmappable severity` path that resolves toward blocking, never below the threshold (FR-002)
- [ ] T006 [P] Add per-tool severity normalisation to `verdict/security.py` mapping `pip-audit`, `bandit`, `npm audit`, and OSV vocabularies into the shared enum, per [data-model.md](./data-model.md#finding) (FR-002)
- [ ] T007 [P] Write `contracts/security-exceptions.schema.json` in the repository root `contracts/` directory, copied from [contracts/security-exceptions.schema.json](./contracts/security-exceptions.schema.json) (FR-005)
- [ ] T008 Implement the exception evaluator in `verdict/security.py`: create the tracked repository-root `security-exceptions.json` (initially an empty exception list that validates against the schema), load it from that fixed path, validate against the schema with `jsonschema`, evaluate `expires_on` against an independently supplied build clock rather than any record-supplied time, and resolve absent, schema-invalid, and expired entries all to "no exception" (FR-005d, FR-005e)
- [ ] T009 Add tests in `tests/test_security.py` for severity normalisation, including an unmappable severity that must be treated as at or above the threshold (FR-002)
- [ ] T010 Add tests in `tests/test_security.py` for the three exception failure modes — file absent, file schema-invalid, entry expired — each asserting the finding still blocks and that an invalid file is itself reported; include a forged record timestamp proving expiry uses the build clock, and a stale entry (references no live finding) that is reported but does not block (FR-005d, FR-005e)
- [ ] T011 Extend `verdict/compatibility_manifest.py` to schema version `2`, adding `security_policy` with `blocking_severity` and `policy_version` **inside the hashed region**, per [contracts/compatibility-manifest-v2.md](./contracts/compatibility-manifest-v2.md) (FR-023)
- [ ] T012 Add the version-2 rejection cases to `verdict/compatibility_manifest.py`: a version-1 manifest, an absent `security_policy`, and an unrecognised `blocking_severity` are each rejected rather than defaulted, and `verdict compat check` names both the expected and the declared threshold whenever they diverge (FR-025)
- [ ] T013 [P] Extend `tests/test_compatibility_manifest.py` with round-trip coverage for version 2 and each rejection case, asserting the hash covers the policy so a manifest cannot be re-emitted at a weaker threshold, plus a divergence case asserting the failure message names both the expected and the declared threshold (FR-025, SC-007)
- [ ] T014 Add a regression test in `tests/test_compatibility_manifest.py` asserting that a version-1 reader given a version-2 manifest is rejected by the existing `__post_init__` guard on `schema_version != "1"`; keep this task focused on the fail-closed behavior, not on counting source branches (FR-023)
- [ ] T015 Wire `verdict compat manifest` and `verdict compat check` into `.github/workflows/ci.yml` as non-advisory steps, completing the CI half ADR-024 left open (FR-024)
- [ ] T016 [P] Add a test in `tests/test_runtime_compatibility.py` asserting the compat step exists in `.github/workflows/ci.yml` and carries neither `continue-on-error` nor `|| true` (FR-024)

**Checkpoint**: One threshold exists, is carried across the repository boundary, and cannot be waived except through a schema-validated, expiring entry

---

## Phase 3: User Story 1 — A release cannot ship an unresolved critical finding (Priority: P1) 🎯 MVP

**Goal**: A finding at or above the declared severity stops the release. It does not warn.

**Independent Test**: Introduce a finding at the declared severity and confirm the release
workflow fails; lower it below the threshold and confirm the release proceeds. Confirm no
security step carries `continue-on-error` or `|| true`.

### Tests for User Story 1

- [ ] T017 [P] [US1] Add a test in `tests/test_generate_gates_report.py` asserting `_is_advisory` classifies each new security step as enforcing, so an advisory step cannot masquerade as a gate (FR-001, FR-003)
- [ ] T018 [P] [US1] Add a test in `tests/test_release_workflow.py` asserting every third-party `uses:` reference across `.github/workflows/` resolves to an immutable revision and none to a moving branch (FR-005a, SC-011)
- [ ] T019 [US1] Add a test in `tests/test_release_workflow.py` asserting a bill of materials is generated for every published artifact and that each carries a provenance attestation, and that no SBOM step carries `continue-on-error` or `|| true` (FR-006, FR-007)
- [ ] T020 [US1] Add parameterized tests in `tests/test_generate_gates_report.py` proving tool crash, missing report, unparseable output, and skipped execution each produce `BLOCKED` and never `PASS` (FR-003)
- [ ] T021 [US1] Add dependency-scope tests in `tests/test_security.py` proving a shipped Python or npm dependency at the threshold blocks while an equivalent build/test-only finding is reported without blocking (FR-004)
- [ ] T022 [US1] Add a refusal-path test in `tests/test_release_workflow.py` that feeds a blocked report to `scripts/verify_gates.py`, asserts nonzero exit, and verifies every PyPI, npm, and GitHub Release publication job in `.github/workflows/release.yml` has an unconditional `needs` dependency on the `verify-release` job with no `always()` bypass (FR-001, SC-001)
- [ ] T023 [US1] Update `tests/test_release_workflow.py::test_release_workflow_publishes_the_attested_python_artifacts_once` to require an immutable `pypa/gh-action-pypi-publish` SHA and reject `@release/v1`; confirm the test fails against the current moving reference before T027 changes the workflow (FR-005c)

### Implementation for User Story 1

- [ ] T024 [US1] Replace the three divergent thresholds in `.github/workflows/security.yml` — `pip-audit --local` (blocks on any advisory), `bandit -ll` (medium), `npm audit --audit-level=high` (high) — with the single declared severity read from the policy: a step runs `verdict compat manifest`, extracts `security_policy.blocking_severity` as a step output, and passes that value explicitly to each subsequent scanner/evaluator; native threshold flags are used only where supported, while `scripts/generate_gates_report.py` centrally normalises outputs that cannot express the policy (FR-002)
- [ ] T025 [US1] Remove the implied OSV config-defined severity threshold: retain `osv-scanner.toml` only for documented shipped-vs-development package overrides, capture raw OSV findings into release evidence, and pass them through the shared evaluator from T024. Do not invent an unsupported OSV severity configuration option (FR-002, FR-004)
- [ ] T026 [US1] Pin every currently existing third-party action reference except `pypa/gh-action-pypi-publish` to an immutable commit revision with the human-readable version in a trailing comment across `.github/workflows/`; T027 owns the publisher pin, and every later-added action is pinned in its introducing task (FR-005a)
- [ ] T027 [US1] Replace `pypa/gh-action-pypi-publish@release/v1` in `.github/workflows/release.yml` with an immutable revision — it is the one reference on a moving branch, on the publishing step (FR-005c)
- [ ] T028 [US1] Add a pinning check step to `.github/workflows/security.yml` that fails when any third-party reference is not immutable (FR-005b, FR-005c)
- [ ] T029 [US1] Add `cyclonedx-bom==7.3.1` bill-of-materials generation for the Python distribution to `.github/workflows/release.yml`, written as CycloneDX JSON into the evidence directory as a non-advisory step (FR-006, FR-007)
- [ ] T030 [US1] Add `@cyclonedx/cyclonedx-npm@6.0.1` bill-of-materials generation for each published npm package to `.github/workflows/release.yml`, as a non-advisory Node 22 step inside the `verify-release` job — not a separate job — producing one CycloneDX JSON file per artifact (FR-006, FR-007)
- [ ] T031 [US1] Extend the existing `actions/attest-build-provenance` usage in `.github/workflows/release.yml` to attest the bills of materials alongside the distributions (FR-007, FR-008)
- [ ] T032 [P] [US1] Add full-history secret scanning to `.github/workflows/security.yml` with `gitleaks/gitleaks-action@e0c47f4f8be36e29cdc102c57e68cb5cbf0e8d1e` (`v3.0.0`), checkout `fetch-depth: 0`, and `GITLEAKS_VERSION=8.30.1`; disable PR comments and advisory upload paths so detection blocks the job (FR-001, FR-005a)
### Cross-cutting dynamic verification (US3)

- [ ] T033 [P] [US3] Create the new `tests/test_server_dynamic.py` file with dynamic verification of the optional server surface, starting the app in-process and testing unauthenticated access, injection, malformed input, and oversized payloads; assert every case is rejected without crashing, disclosing internal detail, or persisting the rejected payload; this cross-cutting test lands with the `security.yml` pass while covering the boundary story (FR-009, FR-010, SC-009)
- [ ] T034 [US1] Extend the `_check_supply_chain_scans` needle table in `scripts/generate_gates_report.py` with rows for the bill of materials, secret scanning, the pinned-revision check, and dynamic verification, per [contracts/gate-report.md](./contracts/gate-report.md) (FR-001, FR-005b, FR-006, FR-009)
- [ ] T035 [US1] Update `tests/test_generate_gates_report.py` for the extended needle table, covering the missing-needle (`BLOCKED`) and all-advisory (`FAIL`) branches for each new row (FR-003, FR-006, FR-009)
- [ ] T036 [US1] Enforce in `.github/workflows/security.yml` and `scripts/generate_gates_report.py` that a check which cannot be evaluated — tool crash, missing report file, unparseable output, or a skipped step — resolves to `BLOCKED`, and that an absent result is never recorded as `PASS` (FR-003)
- [ ] T037 [US1] Classify dependency findings by shipping scope in `.github/workflows/security.yml`: apply the blocking severity only to dependencies shipped to users, and report build- and test-only findings without blocking. `npm audit --omit=dev` already scopes correctly; `pip-audit --local` does not, so scope the Python audit to the runtime dependency set and report the development set separately (FR-004)
- [ ] T038 [US1] Split `.github/workflows/release.yml` into `verify-release` and `publish-release`: the non-publishing first job (`permissions: contents: read, id-token: write, attestations: write`; no `environment`) builds the exact Python/npm candidates, generates SBOMs and attestations, emits a canonical source-SHA-bound `compatibility-manifest-v2.json` plus its digest, assembles every non-document evidence input, and reserves exact-head locations for the policy-document and exception-evidence steps T045/T048 add later. It uploads a SHA/digest-bound `release-candidate`; pin every newly introduced `uses:` action to an immutable revision in this task. The write-authorized second job (`environment: pypi`; `permissions: contents: write, packages: write, id-token: write`) declares unconditional `needs: verify-release`, downloads and verifies that candidate, performs immutable-target preflight immediately before the first write, attaches the canonical manifest and its digest to the immutable GitHub release, and publishes without rebuilding or regenerating evidence. T048 owns the final placement of `scripts/generate_gates_report.py` and `scripts/verify_gates.py` after both evidence steps (FR-001, FR-003, FR-005a, FR-023, SC-001)

**Checkpoint**: The User Story 1 release boundary, shared severity policy, and pinned tooling are in place. Its first complete all-gate candidate validation occurs after T045 copies the documents and T048 adds exception evidence.

---

## Phase 4: User Story 2 — A reviewer reproduces the security and privacy evidence (Priority: P1)

**Goal**: A reviewer can read what was checked, what was found, and what was accepted,
without re-running the pipeline.

**Independent Test**: Run the gates report and confirm G5.1 and G5.2 report `PASS` rather
than `BLOCKED`, with real files present in the evidence directory.

### Tests for User Story 2

- [ ] T039 [P] [US2] Add a test in `tests/test_launch_gates.py` asserting G5.1 and G5.2 report `PASS` when the documents are present in the evidence directory and `BLOCKED` when they are not (FR-018, SC-002)
- [ ] T040 [US2] Add a test in `tests/test_generate_gates_report.py` asserting a symlinked artifact is rejected, pinning the existing `is_symlink` guard so a future copy step cannot be "optimised" into a link (FR-018)
- [ ] T041 [US2] Add tests in `tests/test_security.py` proving `evidence/security_exceptions.json` contains only schema-valid, unexpired exceptions, preserves every required field, and excludes expired or malformed entries (FR-005f)
- [ ] T042 [P] [US2] Add changed-path, checked-attestation, unchecked-attestation, malformed-event, and non-pull-request cases to `tests/test_threat_model_review.py` before implementing the review gate (FR-019)

### Implementation for User Story 2

- [ ] T043 [P] [US2] Write `THREAT_MODEL.md` at the repository root covering the trust boundaries, assets, adversaries, and mitigations of the control plane and its optional server surface (FR-016)
- [ ] T044 [P] [US2] Write `PRIVACY_POLICY.md` at the repository root stating what is collected, what is stored, the retention rules and their dispositions, and the no-egress guarantee (FR-017)
- [ ] T045 [US2] After T038 has created `verify-release`, add its document-evidence copy step and the corresponding step in `.github/workflows/acceptance-gates.yml`: both copy `THREAT_MODEL.md` and `PRIVACY_POLICY.md` into `evidence/` as **real files**, placed before their non-advisory generator step (FR-018)
- [ ] T046 [US2] Verify the copy actually moves G5.1 and G5.2 off `BLOCKED` by running `uv run python scripts/generate_gates_report.py --evidence-dir evidence` and reading `evidence/gates_status.json` — artifacts resolve against the evidence directory, not the repository root, so authoring the documents alone is not sufficient (FR-018, SC-002)
- [ ] T047 [P] [US2] Record the review and sign-off expectations for security and privacy changes in `SECURITY.md`, pointing at the T050 attestation and the T051 check (FR-019)
- [ ] T048 [US2] Implement `verdict/security.py::write_active_exceptions_evidence` to validate and filter exceptions against the independently supplied build clock, serialize every required field canonically to `evidence/security_exceptions.json`, and invoke that helper from both `.github/workflows/acceptance-gates.yml` and `.github/workflows/release.yml`; in `verify-release`, place `scripts/generate_gates_report.py` and `scripts/verify_gates.py` after both the T045 document copies and this exception-evidence step, so the all-gate check sees complete evidence. Neither workflow may duplicate schema or expiry logic in shell (FR-003, FR-005f)
- [ ] T049 [US2] Add an aggregate-evidence test in `tests/test_launch_gates.py` that runs the complete gate report with missing document artifacts and asserts G5.1, G5.2, and every future artifact-backed gate are explicitly `BLOCKED` (not merely not `PASS`), while G5.3 workflow-needle status remains determined by its workflow evidence (FR-003, FR-018, SC-002)
- [ ] T050 [US2] Create `.github/pull_request_template.md` (does not exist today) with the exact threat-model review attestation checkbox and affected-path guidance (FR-019)
- [ ] T051 [US2] Implement `scripts/check_threat_model_review.py` to read base/head SHAs and pull-request text from `GITHUB_EVENT_PATH`, derive changed paths with `git diff <base>...<head>`, fail closed on malformed PR events, require the checked attestation for security-sensitive paths, and report non-PR events as not applicable (FR-019)
- [ ] T052 [US2] Wire `scripts/check_threat_model_review.py` into `.github/workflows/ci.yml` as a non-advisory pull-request step with neither `continue-on-error` nor `|| true` (FR-019)

**Checkpoint**: G5.1 and G5.2 are unblocked and a reviewer can reconstruct the evidence from the bundle alone

---

## Phase 5: User Story 3 — Private data cannot cross a boundary it was never meant to cross (Priority: P2)

**Goal**: Telemetry stays local, retention is enforced, and erasure and the append-only
evidence chain coexist.

**Independent Test**: Run the privacy tests offline; confirm no test needs a live endpoint,
and that the evidence chain still verifies after an erasure.

### Tests for User Story 3

- [ ] T053 [US3] Add the next test in the declared `tests/test_security.py` single-writer sequence: monkeypatch `socket.socket`, `socket.create_connection`, and `urllib.request.urlopen` to raise, then exercise the receipt store, memory plane, adaptive state, and CLI routing paths and assert none attempts a connection, enforcing the no-egress guarantee by failing check rather than by prose (FR-012, SC-006)
- [ ] T054 [US3] Add a test in `tests/test_security.py` asserting the chain still verifies after an erasure record is appended, and that what remains is non-reversible (FR-015a, FR-015b, SC-010)
- [ ] T055 [US3] Add a test in `tests/test_security.py` asserting no erasure path deletes or rewrites a prior record — a verification failure here means history was rewritten instead of appended to (FR-015, FR-015a)
- [ ] T056 [US3] Add a negative-path test set in `tests/test_security.py` asserting that content crossing the memory boundary — memory-plane entries, receipts, and adaptive state — never persists a credential or personal datum in recoverable form: seed each store with known secret and personal-data markers, then assert no marker is readable from any on-disk artefact (FR-011, SC-005)
- [ ] T057 [US3] Add a test in `tests/test_security.py` that reads each retention lifetime from the code constants in `ReceiptStore` and `adaptive_state`, advances time past it, and asserts the corresponding category is actually expired by `ReceiptStore.apply_retention` and `adaptive_state._enforce_retention`, so every documented period is enforced by software rather than by prose (SC-008)

### Implementation for User Story 3

- [ ] T058 [US3] Implement the governed erasure entry point composing the existing primitives — `MemoryPlane.tombstone`, `ReceiptStore.tombstone`, `ReceiptStore.apply_retention`, and the redaction helpers in `verdict/security.py`. Do **not** add a deleter; it would contradict ADR-017's ledger design (FR-015)
- [ ] T059 [US3] Enumerate the retention rules — category, lifetime, disposition, store — taken from the code constants T057 tests, in `PRIVACY_POLICY.md` (after T044 has created it; T044 is the only other writer), including the `retained-as-reference` disposition for non-reversible references (FR-013, FR-014)
- [ ] T060 [US3] Confirm `ReceiptStore.apply_retention` and `adaptive_state._enforce_retention` actually implement the lifetimes the policy states, and reconcile any divergence in favour of the enforced behaviour rather than the prose (FR-014, SC-008)
- [ ] T061 [US3] Validate governed-erasure inputs in `verdict/security.py` so a malformed erasure request is refused rather than partially applied (FR-015)

**Checkpoint**: All three user stories are independently functional in `verdict-core`

---

## Phase 6: Core Closeout, Delivery, and Publication Gate

**Purpose**: Finish and independently verify the initial `verdict-core` work unit before any
dependent repository changes begin.

- [ ] T062 [P] Write `docs/adr/ADR-028-security-privacy-launch-gate.md` recording the Core gate, two-job release boundary, evidence ownership, the Core → Node → Core-follow-up rollout order, and the rollback path (revert the Core PR; version-1 readers stay fail-closed on a v2 manifest; no registry mutation occurs before T068), satisfying Constitution III before the first dependent merge (FR-020–FR-025, Constitution III)
- [ ] T063 Run quickstart Scenarios 1–7 from [quickstart.md](./quickstart.md) in `verdict-core` and write start/end timestamps, results, and unavailable signals to ignored `evidence/core-quickstart-results.json` (SC-003)
- [ ] T064 Run the Core verification baseline (`./.venv/bin/pytest -q`, `./.venv/bin/ruff check .`, `./.venv/bin/ruff format --check .`, `./.venv/bin/mypy verdict --strict`, `./.venv/bin/python -m build`, `git diff --check`) and record exact commands and outcomes in ignored `evidence/core-verification.json` (SC-003, Constitution IV)
- [ ] T065 Compare `git status --short` with the T002 snapshot and write ignored `evidence/clean-tree-status.json`: paths already dirty in the snapshot are permitted only when unchanged from that snapshot; every new or changed path must be feature-owned under `specs/238-security-privacy-launch-gate/`, `verdict/`, `scripts/`, `tests/`, `.github/workflows/`, `contracts/`, and `docs/adr/`, or be `.github/pull_request_template.md`, root `THREAT_MODEL.md`, `PRIVACY_POLICY.md`, `SECURITY.md`, `security-exceptions.json`, `osv-scanner.toml`, `pyproject.toml`, `uv.lock`, `package.json`, `package-lock.json`, or `.gitignore`. Fail on every other path and never edit generated `evidence/gates_status.json` (Constitution IV)
- [ ] T066 Update every Core release version surface from `0.2.0` to `0.3.0` (the version carrying compatibility manifest v2): `pyproject.toml`, `verdict/__init__.py`, `contracts/package.json`, `verdict/client-sdk/package.json`, its `@bodanglin/verdict-contracts` peer/dev dependency, the private root `package.json`, and `package-lock.json`; rerun the T064 full verification baseline and `scripts/verify_release_versions.py --tag v0.3.0`, then verify the local Core `evidence/` directory contains exact-head distributions, SBOMs, threat/privacy documents, exception evidence, clean-tree evidence, canonical digests, a canonical source-SHA-bound `compatibility-manifest-v2.json`, and an all-PASS generated `gates_status.json`. The `release-candidate` bundle is produced only by the `v*` tag run and is verified in T068 (SC-001, SC-004, Constitution IV)
- [ ] T067 After explicit delivery authorization, commit only the Core feature paths, push `238-security-privacy-launch-gate`, open the Core pull request, resolve independent review findings, require green CI on the exact head, and merge through the repository-authorized API path; record the PR, head SHA, checks, and merge SHA in ignored `evidence/core-delivery.json` (SC-001, SC-004, Constitution III)
- [ ] T068 Only after separate explicit publication authorization, push tag `v0.3.0` on the exact merge SHA, wait for `verify-release`, download and verify the `release-candidate` bundle (exact-tag distributions, SBOMs, attestations, documents, exception evidence, digests, all-PASS `gates_status.json`) before allowing `publish-release` to run, then independently verify the immutable GitHub release, PyPI artifact, npm artifacts, SBOMs, attestations, and public-install result; write exact registry observations to ignored `evidence/core-publication.json` (SC-001, SC-004)

**Checkpoint**: Core is merged, independently verified, and—only if separately authorized—
published. Planning or a merged PR alone does not satisfy T068. If publication is deferred,
the feature pauses here: Phases 7 and 8 are unreachable until T068 completes, and no Node
worktree is created.

---

## Phase 7: `verdict-node` Parity (SEPARATE REPOSITORY AND PULL REQUEST)

**Purpose**: Bring the Node repository onto the published Core policy without sharing a
writer, worktree, commit, or release decision with Core.

**⚠️ Constitution III**: T069 first establishes the isolated Node worktree, its Core-release
receipt, and its package prerequisite; no other Node implementation or test task starts until
that prerequisite is complete. All Phase 7 paths are owned by
`/home/nick/dev/verdict-node/.worktrees/238-verdict-node`.

### Node prerequisite and tests

- [ ] T069 After T068 independently proves the exact Core `0.3.0` publication, verify that registry version and create `/home/nick/dev/verdict-node/.worktrees/238-verdict-node` from current `/home/nick/dev/verdict-node` `origin/main` on branch `238-security-privacy-parity`; add `evidence/` to the Node `.gitignore` as the first Node-owned change, update `@bodanglin/verdict-contracts` in `package.json` from `^0.1.0` to `^0.3.0`, refresh `package-lock.json`, install the resolved dependency, and download the Core release's canonical `compatibility-manifest-v2.json`; verify its source SHA and digest before recording the manifest path/digest, merge SHA, registry version, lookup timestamp, commands, resolved package version, and clean starting status in ignored `/home/nick/dev/verdict-node/.worktrees/238-verdict-node/evidence/core-prerequisite.json`. T070/T074 consume that verified fixture after this prerequisite (FR-023, FR-024, Constitution III)
- [ ] T070 [P] Add manifest-v2 parsing, absent-policy, unknown-severity, hash-coverage, and deliberate one-sided-threshold failure tests to `/home/nick/dev/verdict-node/.worktrees/238-verdict-node/tests/contract-parity.test.ts`, including diagnostics naming expected and declared thresholds (FR-023, FR-025, SC-007)
- [ ] T071 [P] Create (new file) absent, malformed, expired, and valid exception-file tests in `/home/nick/dev/verdict-node/.worktrees/238-verdict-node/tests/security-exceptions.test.ts` against `/home/nick/dev/verdict-node/.worktrees/238-verdict-node/contracts/security-exceptions.schema.json` (FR-005, FR-005d, FR-005e)
- [ ] T072 [P] Add workflow tests to `/home/nick/dev/verdict-node/.worktrees/238-verdict-node/tests/workflow-security.test.ts` requiring local dependency, secret, static-analysis, and compatibility gates and proving no Core gate result can authorize Node publication (FR-020, FR-022)
- [ ] T073 Add release-workflow tests to `/home/nick/dev/verdict-node/.worktrees/238-verdict-node/tests/workflow-security.test.ts` requiring one CycloneDX SBOM and provenance attestation per npm artifact, with publication dependent on Node's own non-advisory gates (FR-021, SC-004)

### Node implementation

- [ ] T074 Implement the typed manifest-v2 `security_policy` reader and fail-closed comparison in new file `/home/nick/dev/verdict-node/.worktrees/238-verdict-node/src/compatibility-manifest.ts`, exported through `/home/nick/dev/verdict-node/.worktrees/238-verdict-node/src/index.ts` (FR-023, FR-025)
- [ ] T075 Add `/home/nick/dev/verdict-node/.worktrees/238-verdict-node/security-exceptions.json`, create `contracts/` and copy the schema to `/home/nick/dev/verdict-node/.worktrees/238-verdict-node/contracts/security-exceptions.schema.json`, and implement schema/expiry evaluation in new file `/home/nick/dev/verdict-node/.worktrees/238-verdict-node/src/security-exceptions.ts` (FR-005, FR-005d, FR-005e)
- [ ] T076 Create `/home/nick/dev/verdict-node/.worktrees/238-verdict-node/.github/workflows/security.yml` with Node-local dependency advisories, Gitleaks history scanning, static analysis, and the shared blocking severity; every unavailable or malformed result blocks (FR-003, FR-020)
- [ ] T077 Pin every third-party action reference in `/home/nick/dev/verdict-node/.worktrees/238-verdict-node/.github/workflows/` to an immutable SHA with a human-readable version comment (FR-005a, SC-011)
- [ ] T078 Add `@cyclonedx/cyclonedx-npm@6.0.1` under Node 22 to `/home/nick/dev/verdict-node/.worktrees/238-verdict-node/.github/workflows/npm-publish.yml`, producing one CycloneDX JSON SBOM per npm artifact (FR-021)
- [ ] T079 Extend `/home/nick/dev/verdict-node/.worktrees/238-verdict-node/.github/workflows/npm-publish.yml` to attest every npm artifact and SBOM and make publication depend unconditionally on Node's own security and compatibility results (FR-021, FR-022, SC-004)
- [ ] T080 Wire the manifest-v2 compatibility check into `/home/nick/dev/verdict-node/.worktrees/238-verdict-node/.github/workflows/ci.yml` as a non-advisory required job with neither `continue-on-error` nor `|| true` (FR-024)

### Node validation and delivery

- [ ] T081 Run every declared script from `/home/nick/dev/verdict-node/.worktrees/238-verdict-node/package.json`—build, test, lint, typecheck, format check, package dry-run, and package verification—and record exact outcomes in ignored `/home/nick/dev/verdict-node/.worktrees/238-verdict-node/evidence/node-verification.json` (FR-020–FR-024)
- [ ] T082 Confirm `/home/nick/dev/verdict-node/.worktrees/238-verdict-node` has no unexpected tracked or untracked output after validation and write the canonical status comparison to ignored `/home/nick/dev/verdict-node/.worktrees/238-verdict-node/evidence/clean-tree-status.json` (Constitution IV)
- [ ] T083 After explicit delivery authorization, commit only Node-owned paths, push `238-security-privacy-parity`, open the Node pull request, resolve independent review findings, require green CI on the exact head, and merge; record PR, checks, head SHA, and merge SHA in ignored `/home/nick/dev/verdict-node/.worktrees/238-verdict-node/evidence/node-delivery.json` (FR-020–FR-024, Constitution III)
- [ ] T084 Only after separate explicit publication authorization, publish the Node package and independently verify its registry artifact, SBOM, provenance, and install result; record exact observations in ignored `/home/nick/dev/verdict-node/.worktrees/238-verdict-node/evidence/node-publication.json` (SC-004)

**Checkpoint**: Node parity is implemented, tested, merged, and—only if authorized—published
from its own repository evidence.

---

## Phase 8: Core Cross-Repository Coherence Follow-up (SEPARATE CORE PULL REQUEST)

**Purpose**: Return to a fresh Core base only after Node lands, reconcile the contract and
documentation, and prove the complete two-repository outcome.

- [ ] T085 Verify the exact Node merge and publication evidence, fetch current Core `origin/main`, and create `/home/nick/dev/verdict-core/.worktrees/238-core-coherence` on branch `238-security-privacy-coherence`; add `evidence_bundle/` to the Core `.gitignore` (only `evidence/*.json` is ignored today), and write both repository SHAs, published versions, and clean starting status to ignored `/home/nick/dev/verdict-core/.worktrees/238-core-coherence/evidence/coherence-prerequisite.json` (FR-020–FR-025, Constitution III)
- [ ] T086 Update `/home/nick/dev/verdict-core/.worktrees/238-core-coherence/docs/adr/ADR-024-cross-repo-compatibility-gate.md` and `/home/nick/dev/verdict-core/.worktrees/238-core-coherence/docs/adr/README.md` from "Partially Implemented" only when the Node receipts prove the downstream declaration and CI wiring are merged (FR-024)
- [ ] T087 Run all eight scenarios in `/home/nick/dev/verdict-core/.worktrees/238-core-coherence/specs/238-security-privacy-launch-gate/quickstart.md`, record start/end timestamps and total elapsed time against SC-003's 15-minute budget, and write every pass, failure, blocked, and unavailable result to ignored `/home/nick/dev/verdict-core/.worktrees/238-core-coherence/evidence/quickstart-results.json` (SC-003)
- [ ] T088 Perform the constitution-required cross-repository coherence audit and write contract versions, Core/Node SHAs, registry observations, CI runs, rollout order, rollback path, and unresolved limitations to ignored `/home/nick/dev/verdict-core/.worktrees/238-core-coherence/evidence/cross-repo-coherence.json` (FR-020–FR-025, Constitution III)
- [ ] T089 Run the Core repository-native test, lint, format, type, build, package, compatibility, security, and failure-path checks in `/home/nick/dev/verdict-core/.worktrees/238-core-coherence` and record exact commands/outcomes in ignored `/home/nick/dev/verdict-core/.worktrees/238-core-coherence/evidence/final-verification.json` (SC-003, Constitution IV)
- [ ] T090 Verify `/home/nick/dev/verdict-core/.worktrees/238-core-coherence/evidence_bundle/` contains both repositories' immutable receipts, SBOM/provenance proof, the two policy documents, clean-tree evidence, and generated `gates_status.json`, all bound to exact source states (SC-004)
- [ ] T091 After explicit delivery authorization, commit only the ADR/coherence paths in `/home/nick/dev/verdict-core/.worktrees/238-core-coherence`, push `238-security-privacy-coherence`, open a separate Core pull request, resolve independent review findings, require green CI on the exact head, and merge; record the final PR and merge evidence without claiming a new publication (FR-024, Constitution III)

**Checkpoint**: The cross-repository feature is coherent, independently evidenced, and
closed through a separately reviewed Core follow-up. No repository shares a commit or writer.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — start immediately
- **Foundational (Phase 2)**: Depends on Setup — **BLOCKS all user stories**, because every check reads the severity policy and the exception evaluator
- **User Stories (Phases 3–5)**: All depend on Foundational
- **Core closeout (Phase 6)**: Depends on all desired Core user stories; T067 delivers the Core PR and T068 is the separate publication authority gate
- **Node parity (Phase 7)**: Depends on T067 and T068 completing with independently verified Core merge and registry evidence
- **Core coherence follow-up (Phase 8)**: Depends on the Node PR and any required Node publication completing with exact receipts; it starts from fresh Core `origin/main`

### User Story Dependencies

- **US1 (P1)**: Can start after Phase 2. T038 builds the release boundary independently of the policy-document copy; the docs are added to that boundary by T045 after T043/T044 author them.
- **US2 (P1)**: Can start after Phase 2. Shared-workflow order is T038 → T045 → T048; T043/T044 author both documents before T045 copies them.
- **US3 (P2)**: Can start after Phase 2. T059 writes `PRIVACY_POLICY.md` only after T044 has created it.

### Within Each User Story

- Tests are written first and must fail before the implementation lands
- Policy and normalisation before the checks that read them
- Checks before the gate-report rows that assert them
- Story complete before moving to the next priority

### Critical Ordering Constraints

Six orderings are not negotiable and will produce silent failures if reversed:

1. **T023 before T027.** The updated immutable-reference test must fail against the current `@release/v1` workflow before T027 replaces it with a pinned SHA.
2. **T045 before T046.** The copy step must exist before the verification that it worked. Skipping T046 is the specific way this feature ships looking complete while G5.1 and G5.2 stay `BLOCKED`.
3. **T068 gates Phase 7.** No Node worktree or edit starts until the Core merge and separately authorized publication are independently verified.
4. **T084 gates Phase 8.** ADR-024 cannot be marked complete and the coherence worktree cannot start until the Node merge and required publication evidence exist.
5. **T038 before T045 before T048; T043/T044 before T045.** `release.yml` has one sequential writer: T038 creates the boundary, T045 adds document evidence after the documents exist, then T048 adds exception evidence.
6. **T044 before T059.** `PRIVACY_POLICY.md` is created once by T044; T059 appends the retention table.

### Parallel Opportunities

- T006, T007, T013, T016 within Foundational, once T005 lands; T009 and T010 share `tests/test_security.py` and run sequentially
- T017 and T018 (US1 tests) together; T020 follows T017 in the same file, T021 joins the `tests/test_security.py` queue
- After workflow-wide pinning completes, T032 (`security.yml`) and T033 (`test_server_dynamic.py`) can proceed together; release-workflow tasks remain sequential
- T039, T040, T042 (US2 tests) together; T041 joins the `tests/test_security.py` queue; T043, T044, T047 are three different documents
- `tests/test_security.py` has one designated writer across stories: T009 → T010 → T021 → T041 → T053 → T054 → T055 → T056 → T057, in that order
- Once Phase 2 completes, US1, US2, and US3 can proceed in parallel by three people, subject to constraints 5 and 6 above and the single `tests/test_security.py` writer
- In Phase 7, T070, T071, and T072 are independent Node test files; T073 follows T072 in `workflow-security.test.ts`, T074 follows T070, and T075 follows T071

---

## Parallel Example: User Story 1

```bash
# Launch the User Story 1 tests together:
Task: "Assert _is_advisory classifies each new security step as enforcing in tests/test_generate_gates_report.py"
Task: "Assert every third-party uses: reference is immutable in tests/test_release_workflow.py"

# After workflow-wide pinning is complete, launch the independent tasks together:
Task: "Add pinned history secret scanning to .github/workflows/security.yml"
Task: "Add adversarial optional-server tests in tests/test_server_dynamic.py"

# Keep all .github/workflows/release.yml changes sequential in one writer session.
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
5. Phase 6 → close, review, merge, and—only with separate authorization—publish the initial Core work unit
6. Phase 7 in a separate Node worktree and pull request → `verdict-node` reaches parity and is independently validated
7. Phase 8 in a fresh Core worktree and follow-up pull request → ADR-024 and cross-repository evidence become truthful and complete

### Parallel Team Strategy

1. The team completes Setup and Foundational together — everything depends on them
2. Then: one person on US1 (the largest), one on US2, one on US3
3. Phase 6 has one Core integration owner; Phase 7 starts only after its merge/publication gates
4. Phase 7 has one Node writer; Phase 8 later has a different, fresh Core worktree owner

---

## Notes

- [P] tasks touch different files and have no incomplete dependencies
- [Story] labels map each task to a user story for traceability
- Verify tests fail before implementing
- Commit after each task or logical group; one repository per commit
- Every failure mode in this feature resolves toward blocking: absent, malformed, expired, and unparseable all mean "no exception"
- Report skipped or unavailable evidence as itself — never as a pass
