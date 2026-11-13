# policy-enforcement-contract Specification

## Purpose
Govern native execution boundaries at dispatch/resume, review/integration, and Done/learning, enforcing packet validity, exact source identity, and strict rejection rules before side effects.

## ADDED Requirements

### Requirement: Dispatch and resume boundaries validate the full packet before side effects
Every native dispatch or resume entrypoint mapped in `docs/policy/bod-340-boundary-map.md` (E1 `_orchestrate`/`run_golden_path`, E2 `run_golden_path --resume`, E8 direct worker `cli_run`/`_worker_admission`, E9 Prime bridge `start`, E11 `prime_supervisor:main`, E12 `ready_gate:ready_decision`, E15 `resume --create`) MUST validate one execution packet before creating a run directory, worktree, process, model call, or durable state transition. The validation MUST check:
1. Story, spec, and ADR revisions (Linear issue revision `issue_updated_at`, AC IDs, OpenSpec change ID and digest, ADR ID, status, and digest).
2. Semantic disposition (`change` or `maintenance-no-change`).
3. Mandatory retrieval completed via the BOD-306 memory dispatch gate returning a non-`unavailable` status for new work (running work survives retrieval loss).
4. Owned scope and lease: the story lease is held, active, unstale, and matches the packet attempt, generation, worktree, and branch. Resumed runs re-check the same packet identity.
5. Live, capable route: verified via existing permit path (`commit_dispatch`, `revocation_epoch`) and recorded in the audit record.
6. Bound authorization: `admit_action` receives populated Linear issue, AC, brief, ownership, safety, and proof references.

A denial MUST be settled immediately (recording a terminal denial event and receipt or refusal outcome) so supervisors and improvement loops do not restart or learn from it.

#### Scenario: Compliant dispatch admits with full packet bound (Entrypoints: E1, E8, E9, E11)
- **WHEN** dispatch is requested through E1 (`verdict orchestrate`), E8 (`worker_runtime`), E9 (Prime bridge), or E11 (`prime_supervisor`) with a packet satisfying all six checks
- **THEN** dispatch is admitted, the lease generation and packet identity are bound to the execution context, and the audit record contains the permit ID and reference digests

#### Scenario: Missing or mismatched Linear issue or AC revision denies before side effects (Entrypoints: E1, E8, E9)
- **WHEN** dispatch is attempted with missing issue/AC references or an `issue_updated_at` differing from the current Linear issue state
- **THEN** dispatch is denied immediately before any run directory, worktree, or worker process is created, leaving a durable refusal or terminal denial event

#### Scenario: Resumed run validates packet identity and re-checks revisions (Entrypoints: E2, E15)
- **WHEN** a run resumes via E2 (`run_golden_path --resume`) or E15 (`verdict resume --create`)
- **THEN** the gate re-validates the packet, spec, and ADR revisions; any revision mismatch invalidates only the affected nodes and refuses branch creation or unvalidated execution

#### Scenario: Mandatory retrieval unavailable blocks new dispatch while running work survives (Entrypoints: E1, E8)
- **WHEN** new work is dispatched while ai-memory retrieval is `unavailable`
- **THEN** the dispatch gate fails closed with 0 model calls; previously running work continues unaffected

#### Scenario: No-dependency story is evaluated by the policy gate (Entrypoint: E12)
- **WHEN** a story with no dependencies is evaluated by E12 (`ready_decision`)
- **THEN** the gate calls `admit_action` before declaring `READY`, and an unavailable or denying policy returns `WAIT`

#### Scenario: Denied orchestrate run settles without restart or learning (Entrypoints: E1, E2, E3, E4)
- **WHEN** an orchestrate run is denied at E1 or E2
- **THEN** the runtime writes a terminal `BLOCKED` receipt and event, E3 (`verdict supervise`) terminates without restarts, and E4 (`verdict improve`) writes no `learned` lesson from the denied run


### Requirement: Review and integration boundaries verify packet identity and exact tested source
The review and integration entrypoints (E6 `DagRuntime._conclude`/`_review_with_bound`, E7 `completion_verdict`/`write_run_receipt`, E17 `scripts/prime_workflow.py` `check-proof`/`open-pr`/`merge-guard`) MUST verify that the executed work matches the exact packet identity recorded at dispatch and that proof, review, and integration barriers are bound to the exact tested source HEAD SHA.

#### Scenario: Integration and review verify matching packet and HEAD SHA (Entrypoints: E6, E7, E17)
- **WHEN** a story or node reaches review or PR submission with proof, review, and integration commits bound to the exact HEAD SHA and the dispatch packet identity unchanged
- **THEN** the gate admits the work for review, PR creation, or receipt completion

#### Scenario: Tested source HEAD mismatch blocks integration and review (Entrypoints: E6, E7, E17)
- **WHEN** the proof head, review head, or local branch commit differs from the tested source HEAD SHA recorded in the packet
- **THEN** review and integration fail closed with status `BLOCKED`, and no PR or `COMPLETE` receipt is produced

#### Scenario: Resumed run revalidates prior nodes on revision change (Entrypoints: E2, E6)
- **WHEN** a resumed run contains `prior_validated` nodes but governing spec or ADR revisions changed
- **THEN** the review gate invalidates the affected nodes and requires revalidation before integration


### Requirement: Done and learning boundaries require independent and installed-main acceptance
The Done and learning transitions (E4 `verdict improve`, E18 `check_dod.py`/`prime_workflow.transition`) MUST enforce independent whole-task acceptance and installed-main acceptance before any story or change is declared `DONE`.

#### Scenario: Story transitions to DONE only with passing DoD evidence (Entrypoint: E18)
- **WHEN** a story completion or `DONE` transition is requested
- **THEN** the gate requires a passing evidence file verified by `check_dod.py` containing independent parent confirmation and green installed-main acceptance commands

#### Scenario: Improvement cycle ignores policy-denied runs (Entrypoint: E4)
- **WHEN** a controller run ends in a policy denial or policy-induced `BLOCKED` status
- **THEN** E4 (`verdict improve`) skips the `learned` stage for that cycle and records no lessons into `cycles.jsonl`


### Requirement: Rejection rules enforce artifact validity, consistency, and interface compatibility
The contract validator MUST reject, with an explicit named refusal reason, any operation violating governing artifact rules:
1. Missing material OpenSpec change or ADR for code-changing work without a reviewed no-change disposition.
2. Citing a superseded ADR as governing.
3. Conflict between code, spec, and ADR at the bound revisions.
4. A Proposed ADR represented as Accepted (or any status line mismatch).
5. Unauthorized weakening of scenarios or assertions (removal or loosening of `#### Scenario` blocks or test assertions without a reviewed disposition).
6. Incompatible shared-interface change failing `verdict compat` under `contracts/`, `schemas/`, or `verdict/schemas/`.

#### Scenario: Missing material OpenSpec change or ADR is rejected (Entrypoints: E1, E8, E9)
- **WHEN** code-changing work is dispatched without an OpenSpec change or governing ADR, and lacks a `maintenance-no-change` disposition
- **THEN** the validator rejects dispatch naming `missing_material_governing_artifact`

#### Scenario: Superseded ADR cited as governing is rejected (Entrypoints: E1, E8, E17)
- **WHEN** a dispatch packet or bundle cites an ADR whose file status is `Superseded`
- **THEN** the validator rejects dispatch or review naming `superseded_adr_rejected`

#### Scenario: Proposed ADR claimed as Accepted is rejected (Entrypoints: E1, E8, E17)
- **WHEN** a packet asserts `status: Accepted` for an ADR whose header is `Status: Proposed`
- **THEN** the validator rejects the packet naming `adr_status_mismatch`

#### Scenario: Unauthorized scenario or assertion weakening is blocked (Entrypoints: E6, E17, E21)
- **WHEN** a diff removes or weakens existing `#### Scenario` blocks or test `assert` statements without an approved disposition
- **THEN** review and merge gates fail closed naming `unauthorized_weakening_detected`

#### Scenario: Incompatible shared-interface change is rejected (Entrypoints: E6, E17, E20, E21)
- **WHEN** changes under shared contract or schema directories break compatibility under `verdict compat check`
- **THEN** integration and review gates fail closed naming `interface_compatibility_violation`


### Requirement: Legitimate maintenance path provides reviewed no-change disposition
Legitimate maintenance operations (documentation, refactoring, comments with no behavior change) MUST link current governing artifacts using an explicit `maintenance-no-change` disposition and an identified reviewer.

#### Scenario: Reviewed maintenance disposition admits without spec changes (Entrypoints: E1, E8, E9)
- **WHEN** work is submitted with semantic disposition `maintenance-no-change`, linking current valid ADR and spec revisions with reviewer endorsement
- **THEN** dispatch is admitted without requiring new OpenSpec deltas or ADR updates

#### Scenario: Behavioral change disguised as maintenance is rejected (Entrypoints: E6, E17)
- **WHEN** a change declared as `maintenance-no-change` modifies production interfaces or alters behavioral test scenarios
- **THEN** the review gate rejects the submission naming `disposition_behavior_mismatch`


### Requirement: Preserved system invariants and failure isolation
The policy enforcement contract MUST preserve all existing security and runtime invariants:
1. Single-use dispatch permits and revocation epochs.
2. Stream terminal semantics and close-once cleanup.
3. Idempotency tokens and secret redaction across audit records.
4. Running-work safety: running work survives retrieval or policy-source loss; only new dispatch is refused.
5. Isolation of changes: modification to a governing source invalidates only affected evidence, not unrelated runs.
6. Absence of blanket bypass or secondary policy authority.

#### Scenario: Single-use permit cannot be reused across dispatches (Entrypoints: E5, E8, E19)
- **WHEN** a dispatch permit ID is submitted a second time or outside its revocation epoch
- **THEN** the permit check refuses authorization and marks the attempt invalid

#### Scenario: Policy source failure fails closed for new dispatches while protecting running work (Entrypoints: E1, E5, E8)
- **WHEN** the approved policy source becomes unavailable or corrupted mid-execution
- **THEN** subsequent new admissions fail closed immediately, while already running nodes proceed under their bound lease
