# Feature Specification: Recruiter-Ready README and Proof Demo

**Feature Branch**: `feat/455-recruiter-ready-readme`

**Created**: 2026-09-06

**Status**: Draft

**Input**: User story from GitHub issue #455: make Verdict understandable in a 90-second skim, provide a credential-free proof demo with named drop reasons and a receipt identifier, keep the quickstart script aligned, and remove stale public metadata.

## Clarifications

### Session 2026-09-06

- Q: May this existing PR keep its Spec Kit records in `specs/455-recruiter-ready-readme/`, where the repository tooling already resolves them? → A: Keep the existing location. The user approved this story-specific exception to the workspace `.specify/specs/` convention.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Understand and run the flagship proof (Priority: P1)

A recruiter, hiring manager, or engineer landing on Verdict can identify the product, install it, run one command without credentials or a gateway, and see a deterministic routing decision with the selected route, named exclusions, and an explicitly labeled fixture receipt.

**Why this priority**: The portfolio's first job is to create a credible technical conversation quickly. A visitor must see the product's differentiator before reading architecture or implementation history.

**Independent Test**: In a clean environment with no provider credentials, no gateway, and an empty working directory, install Verdict and run the documented quickstart command. The command exits successfully, performs no provider call, writes no application state, and prints the selected route, all named exclusions, receipt identifier/mode, and pass status.

**Acceptance Scenarios**:

1. **Given** a clean environment with Python 3.10+ and no API keys, **When** the visitor installs Verdict and runs `verdict quickstart --non-interactive --dry-run`, **Then** the command exits 0 without network/provider access and prints the deterministic fixture result.
2. **Given** the deterministic fixture, **When** the visitor reads the output, **Then** it shows `demo/frontier-tools` selected, three named exclusions (`missing capability: tools`, `quota exhausted`, and `health unknown`), and `fixture:issue-35 (deterministic_fixture)` as the receipt identifier and mode.
3. **Given** the README's first screen, **When** the visitor skims it for 90 seconds, **Then** they can identify what Verdict is, how to install it, how to run the no-key proof, what the output demonstrates, and where to find deeper verification.

---

### User Story 2 - Compare the product's economic motivation honestly (Priority: P2)

A technically skeptical reader can see a deterministic mock cost comparison and understand that it uses estimates and a defined baseline rather than observed invoices or live-provider quality claims.

**Why this priority**: Cost motivation explains why routing matters, but it must not outrank the executable proof or imply production savings that the current evidence does not establish.

**Independent Test**: Run the documented deterministic mock benchmark from a contributor checkout and compare its output/documentation with the README wording. The wording identifies the request count, fixed price estimates, routed and baseline values, and the limitations of the mock.

**Acceptance Scenarios**:

1. **Given** the local benchmark fixture, **When** the visitor runs `uv run python -m verdict.routing_demo --mock`, **Then** the run uses no provider spend and reports the defined routed-cost/baseline comparison.
2. **Given** the benchmark section, **When** the visitor reads it, **Then** they can distinguish deterministic estimates from observed invoices and live-provider results.

---

### User Story 3 - Keep setup and documentation paths consistent (Priority: P2)

A contributor or evaluator can follow the README, source checkout instructions, and `quickstart.sh` without encountering contradictory commands, stale anchors, or claims that no longer match the linked evidence documents.

**Why this priority**: Documentation drift is itself a negative engineering signal. One working proof path is more valuable than multiple contradictory setup journeys.

**Independent Test**: Run the documented quickstart, the source-checkout equivalent, the flagship-demo regression tests, and the documentation smoke tests from the feature branch. Review all changed README anchors and relative links.

**Acceptance Scenarios**:

1. **Given** a source checkout, **When** the evaluator follows the README's contributor command or runs `quickstart.sh`, **Then** the credential-free verification path is available and its output uses the same selected route, exclusions, receipt identifier, and status vocabulary as the tested fixture.
2. **Given** the repository's current ADR/proof counts and live-observation documentation, **When** the README is reviewed, **Then** it contains no stale ADR count, stale test-count assertion, contradictory catalog status, or broken renamed anchor.
3. **Given** live gateway documentation, **When** a reader follows it, **Then** the README clearly separates dated live observations from offline fixture proof and states that blocked/skipped live runs make no claim.

---

### Edge Cases

- A gateway is unavailable: the primary proof remains runnable offline and does not fail because live setup is absent.
- Provider credentials are present in the environment: the fixture output remains identical and does not read or persist them.
- The visitor runs from an empty directory: the proof command does not write application state into that directory.
- A live catalog request times out: documentation labels the outcome blocked/degraded rather than presenting it as a successful qualification.
- A fixture receipt is synthetic: the output labels it `deterministic_fixture`; the README does not describe it as a production or live-provider receipt.
- A README link target exists as a file but its fragment was renamed: fragment-level validation catches the stale navigation.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The README MUST open with the approved one-sentence positioning: "The LLM router that says no: cheapest qualified model, a named reason for every drop, a receipt for every decision."
- **FR-002**: The primary README path MUST present, in order, the product explanation, install command, no-key proof command/output, named drop reasons and receipt boundary, cost motivation, and architecture/deeper verification links.
- **FR-003**: The documented primary proof command MUST be `verdict quickstart --non-interactive --dry-run` and MUST be executable without provider credentials, a live gateway, or network access.
- **FR-004**: The proof output MUST include the selected route, all three fixture exclusions with their exact reason text, a receipt identifier, a receipt mode, and a successful status.
- **FR-005**: The fixture receipt MUST be explicitly labeled as deterministic fixture evidence and MUST NOT be presented as live-provider execution, production readiness, or independent proof of a real repository change.
- **FR-006**: `quickstart.sh`, the installed CLI output, and the regression test MUST use one shared fixture result rather than maintaining divergent routing/output logic.
- **FR-007**: The README MUST distinguish the deterministic mock cost comparison from observed invoices, live-provider quality, latency, availability, and production savings.
- **FR-008**: The README MUST distinguish offline fixture proof from dated live gateway observations and MUST state that blocked or skipped live runs make no claim.
- **FR-009**: The README MUST report current repository metadata accurately, including 30 numbered ADRs and the current CI-relevant test scope, or omit brittle counts where a stable count cannot be guaranteed.
- **FR-010**: All changed README relative links and section fragments MUST resolve at the audited revision.
- **FR-011**: The feature MUST preserve existing public CLI behavior outside the receipt field added to the deterministic fixture result and its human-readable rendering.
- **FR-012**: The feature MUST NOT add provider credentials, network access, raw prompts, private paths, or application state to the credential-free proof path.

### Key Entities

- **Flagship fixture result**: The deterministic routing result containing task requirements, eligible route, candidate explanations, decision contract, and fixture receipt metadata.
- **Fixture receipt metadata**: A public, non-secret identifier and mode that tells the reader the output is deterministic fixture evidence rather than a live execution receipt.
- **README proof path**: The ordered public documentation journey from product statement through install, proof command/output, limitations, and deeper verification.
- **Public claim boundary**: The distinction between verified local contracts, dated observations, estimates, unsupported claims, and future work.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A clean environment with no credentials or gateway completes the documented install-and-proof journey with an exit code of 0 and no provider/network call.
- **SC-002**: The proof output contains one selected route, exactly three named exclusions, one fixture receipt identifier/mode, and a successful status.
- **SC-003**: The README's primary journey requires no more than one install command and one proof command before showing the differentiating output.
- **SC-004**: Focused flagship-demo and documentation-smoke tests pass, and the changed Python files pass lint.
- **SC-005**: A link/fragment audit finds zero broken local targets in the changed README path.
- **SC-006**: A reviewer can identify the product, run the proof, understand the fixture/live boundary, and locate deeper evidence within a 90-second skim.
- **SC-007**: The README contains no stale ADR count, stale catalog status, or unqualified production/live-provider claim introduced or preserved by this feature.

## Assumptions

- The existing deterministic flagship fixture is the source of truth for the no-key proof; this feature does not create a second routing engine.
- Python 3.10+ and the repository's documented package installation are available to the evaluator.
- Live gateway checks remain optional and are not required for the primary recruiter-ready proof.
- The existing test suite and repository CI define the required implementation gates; this feature adds focused assertions for the receipt output.
- A fixture receipt identifier is intentionally synthetic and is labeled as such.
- The scope is limited to `verdict-core`; LiteLLM, OmniRoute adapters, quant consolidation, and cockpit work remain separate Project #6 stories.
- The Spec Kit phase artifacts for this story are created under the repository's established `specs/` convention; no duplicate `.specify/specs/` copy is created.
