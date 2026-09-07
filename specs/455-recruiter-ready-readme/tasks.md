# Tasks: Recruiter-Ready README and Proof Demo

**Input**: Design documents from `specs/455-recruiter-ready-readme/`

**Prerequisites**: `plan.md`, `spec.md`, `research.md`, `data-model.md`, `contracts/proof-output.md`, `quickstart.md`

**Tests**: Required by the specification. Write or strengthen regression checks before correcting documentation/output behavior.

## Phase 1: Setup and Baseline

**Purpose**: Bind work and evidence to the exact implementation state without changing behavior.

- [X] T001 Record the implementation starting SHA, PR head SHA, changed-file scope, and existing required check names in `specs/455-recruiter-ready-readme/tasks.md` implementation notes without marking prospective evidence complete
- [X] T002 Inspect the canonical report renderer, README proof block, installer verification path, and documentation tests in `verdict/flagship_demo.py`, `README.md`, `quickstart.sh`, `tests/test_flagship_demo.py`, and `tests/test_documentation_smoke.py`

---

## Phase 2: Foundational Regression Contracts

**Purpose**: Establish shared checks that block story completion when documentation drifts.

- [X] T003 Add a failing exact-output regression that extracts the fenced proof block from `README.md` and compares it with the canonical rendered report in `tests/test_flagship_demo.py`
- [X] T004 Add failing local Markdown relative-path and heading-fragment validation for changed README links in `tests/test_documentation_smoke.py`

**Checkpoint**: Confirm T003 and T004 fail for the intended current gaps before implementation changes.

---

## Phase 3: User Story 1 — Understand and Run the Flagship Proof (Priority: P1)

**Goal**: One install command and one no-key proof command expose the exact tested decision, exclusions, fixture receipt, and PASS result.

**Independent Test**: In an isolated empty directory with credential variables removed and no gateway, install the exact candidate and run `verdict quickstart --non-interactive --dry-run`; the command exits 0, matches the README block, and creates no application state.

- [X] T005 [US1] Make the proof block in `README.md` exactly match the canonical human-readable fixture output required by `contracts/proof-output.md`
- [X] T006 [US1] Keep `quickstart.sh` on the same installed CLI proof path and verify it contains no duplicate fixture or report logic in `quickstart.sh`
- [X] T007 [US1] Change `verdict/flagship_demo.py` only if T005 cannot satisfy the exact-output contract without altering behavior outside the receipt/report boundary, and update `tests/test_flagship_demo.py` first if such a change is required
- [X] T008 [US1] Run the focused proof and documentation tests plus changed-file lint/shell checks from `quickstart.md`, recording actual commands and outcomes against the exact implementation snapshot in a repository-approved evidence artifact under `docs/proof/`
- [X] T009 [US1] Create a disposable environment outside the checkout, remove provider credential variables, install the exact candidate, run the documented command from an empty directory, and record the real install source, return codes, output, write-state observation, revision, and limitations under `docs/proof/`
- [X] T010 [US1] Produce an actual sanitized terminal recording or GIF of T009 from the final candidate, bind it to the represented revision, and add its real path/link to `README.md` and the issue-closure evidence; do not reconstruct output or timing

**Checkpoint**: US1 is incomplete until T009 and T010 contain real evidence; automated test success alone is insufficient.

---

## Phase 4: User Story 2 — Compare Economic Motivation Honestly (Priority: P2)

**Goal**: Preserve a visibly deterministic mock comparison without implying invoices, production savings, model quality, latency, or availability.

**Independent Test**: Run the documented mock command and verify README wording and values against actual output/evidence while retaining the mock/live limitations.

- [X] T011 [US2] Verify the current mock output and evidence source, then correct only mismatched values or qualification language in `README.md` and `docs/benchmarks/routing-demo.md`
- [X] T012 [US2] Add or retain regression assertions in `tests/test_documentation_smoke.py` that distinguish deterministic estimates from observed invoices and live-provider claims

**Checkpoint**: US2 passes only when every displayed number is supported by the cited mock evidence at the reviewed revision.

---

## Phase 5: User Story 3 — Keep Setup and Documentation Paths Consistent (Priority: P2)

**Goal**: Ensure commands, metadata, links, fragments, and fixture/live wording remain consistent across public entry points.

**Independent Test**: Documentation smoke tests report zero unresolved changed local paths/fragments, the contributor and installer paths invoke the same proof, and automated structural checks find the required README order.

- [X] T013 [US3] Add required README ordering and one-install/one-proof-command structural assertions to `tests/test_documentation_smoke.py`
- [X] T014 [US3] Resolve every path, fragment, metadata, anchor, or claim-boundary failure found by T004 and T013 in `README.md` without weakening the tests
- [X] T015 [US3] Record automated skim evidence for product, install, proof, limitation, and next-action structure in the approved evidence artifact under `docs/proof/`, explicitly labeling it automated rather than human comprehension evidence
- [ ] T016 [US3] Have a real reviewer perform the 90-second skim and record the actual result, reviewer/date, revision, and limitations in the approved evidence artifact under `docs/proof/`; leave this task open if no reviewer performs it

**Checkpoint**: Automated structure may pass while T016 remains genuinely blocked; do not infer or self-certify human verification.

---

## Phase 6: Cross-Cutting Verification and Handoff

- [ ] T017 Run every repository-native required test, lint, format, type, build, packaging, security, contract, and failure-path gate identified from repository configuration, and record exact commands/outcomes against the final snapshot in `docs/proof/`
- [ ] T018 Verify PR state and every required check conclusion on the exact final head SHA using authoritative GitHub data, recording neutral, missing, or unavailable signals without converting them to success in `docs/proof/`
- [X] T019 Map FR-001–FR-012, SC-001–SC-007, and every issue #455 acceptance, validation, and evidence item to concrete evidence in `docs/proof/ISSUE_455_README_EVIDENCE.md`, leaving every unsupported item explicitly `not-run` or `blocked`
- [ ] T020 Request independent review of the changed implementation, tests, documentation, capture, and `docs/proof/ISSUE_455_README_EVIDENCE.md`; resolve blocking findings before representing issue #455 as complete

---

## Dependencies and Execution Order

- T001–T002 establish the baseline.
- T003–T004 must precede corrective implementation.
- US1 T005–T010 is the MVP and must finish before the story can be represented as recruiter-ready.
- US2 T011–T012 and US3 T013–T016 may proceed after foundational tests, but edits to `README.md` require one integration owner and serialization.
- T017–T020 run only after the intended implementation and evidence artifacts are final.
- T009 precedes T010 because the terminal capture must show the same final fresh-install journey.
- T015 never satisfies T016; automated and human evidence remain separate.

## Parallel Opportunities

- After T003–T004, T012 and T013 can be authored in parallel only if writers own different test regions or isolated worktrees and one owner integrates.
- T009 environment preparation and T011 mock verification can run independently after the candidate is fixed.
- T015 can be prepared from automated results while awaiting the human reviewer for T016.
- GitHub gate retrieval for T018 waits for the final head but is otherwise independent of local full-suite execution T017.

## Implementation Strategy

1. Complete T001–T004 and demonstrate the intended regressions fail.
2. Deliver US1 through T008, then collect real fresh-install and terminal evidence through T010.
3. Verify honest cost wording and documentation consistency through T016.
4. Run full gates and exact-head review through T020.
5. Do not close issue #455 while T009, T010, T016, or any required gate remains incomplete.

## Implementation Notes

- Starting implementation commit and original PR head: `1a983b1a262aa6d5eb4213004eb39b852c5cb0a9`.
- Original PR scope before recovery: `.specify/feature.json`, `README.md`, `quickstart.sh`, `specs/455-recruiter-ready-readme/`, `tests/test_flagship_demo.py`, and `verdict/flagship_demo.py`.
- Required remote checks observed on the original head were benchmark, contract parity, CodeQL, OSV, Python/Node security, SBOM, lint, test, type-check, dynamic check, install smoke, example smoke, and build. They are not evidence for the final uncommitted implementation snapshot; T018 remains open.
- The pre-existing untracked `handy.txt` is outside this feature and must remain untouched.

## Implementation Notes

- Starting implementation commit and original PR head: `1a983b1a262aa6d5eb4213004eb39b852c5cb0a9`.
- Original PR scope before recovery: `.specify/feature.json`, `README.md`, `quickstart.sh`, `specs/455-recruiter-ready-readme/`, `tests/test_flagship_demo.py`, and `verdict/flagship_demo.py`.
- Required remote checks observed on the original head were benchmark, contract parity, CodeQL, OSV, Python/Node security, SBOM, lint, test, type-check, dynamic check, install smoke, example smoke, and build. They are not evidence for the final uncommitted implementation snapshot; T018 remains open.
- The pre-existing untracked `handy.txt` is outside this feature and must remain untouched.

## Traceability

- FR-001–FR-006, FR-011–FR-012; SC-001–SC-004: T003, T005–T010
- FR-007–FR-008; SC-007: T011–T012
- FR-009–FR-010; SC-005–SC-006: T004, T013–T016
- Full issue #455 validation and closure evidence: T009–T010, T015–T020
