# Feature Specification: Context Intelligence Lift

**Feature Branch**: `336-context-intelligence-lift`

**Created**: 2026-08-30

**Status**: Draft  
**Tracking Issue**: #336 (parent epic #80). Numbering note: the branch was first cut as `feat/272-p3-context` and the spec first written as `277-context-intelligence-lift`; both numbers belong to unrelated issues (#272 ExecutionStrategy, open; #277 PR #263 merge task, closed), so #336 was minted to give this feature a non-conflicting number.

**Input**: User-owned 272 Phase 3 remainder: compile a model-aware pack from retrieval, docs, code, and memory so a cheaper identity can succeed where it would fail unaided; prove lift with a paired live evaluation, not a fixture pass.

## Provenance

This specification is the Phase 3 remainder of `specs/272-operational-routing-loop` (Context intelligence and shared recall lift) plus the product thesis “a reasoning planner plans slices, pulls docs, and straps a compact pack onto a cheaper worker.”

- Spec 272 Phase 3 exit signal: a paired evaluation shows which context packages materially change verified success for at least one cheaper/alternative model.
- Feature 276 already proves live cheaper-first execute on a real identity. This feature does not re-prove catalog discovery. It uses a cheaper identity from that live path as the worker under test.
- Confirmed constraints: Core owns policy; memory adapters never own policy; fail closed; no secrets in packs or receipts; do not dump whole repositories; do not require a particular memory, graph, vector, or vendor runtime; fixture-only is not a pass.

## Clarifications

### Session 2026-08-30

- Q: Who owns policy when packing retrieved memory, docs, or code? → A: Core owns policy. Retrieval adapters may supply units with provenance. They MUST NOT admit, exclude, or re-rank against policy. Missing required evidence fails closed.
- Q: May a vendor memory or graph service be required? → A: No. Local searchable archive plus local docs and code are sufficient. Optional adapters may contribute units when present. Absence of a vendor MUST NOT block packing or the paired evaluation.
- Q: What is the bounded unit that proves lift? → A: One named, pre-stated check whose correct answer is a unique fact that lives in retrieved docs, code, or memory and is not guessable from the task wording alone. The same cheaper identity runs twice: unaided, then with the pack. An independent checker accepts or rejects a required artifact. Open-ended chat does not count.
- Q: What counts as a cheaper identity for the proof? → A: A concrete identity selected by the existing live cheaper-first path (local, then free, then cheaper). Paid identities MUST NOT be the lift subject while a cheaper unused qualified identity remains. Fixture catalogs MUST NOT satisfy the live proof.
- Q: How is working memory different from durable memory? → A: Working memory is the current task’s typed slots (goal, slices, retrieved units, pack digest). Durable memory is a searchable archive ingested with provenance and later retrieved by query. Chat history dump is neither.
- Q: When the pack cannot fit the cheaper identity’s context limit? → A: Compile a model-aware subset. Record every omitted unit with a reason. Never dump the remainder. Optional compaction may summarize only when it preserves provenance of what was dropped. Required policy and the unique fact needed by the named check MUST NOT be dropped; if they cannot fit, fail closed rather than send a truncated pack that pretends to be complete.
- Q: Must a live reasoning identity produce retrieval slices? → A: No. Core MUST plan bounded slices deterministically from the task and available source categories so packing is reproducible without a second live model. A stronger identity MAY propose extra slices; Core still bounds them and refuses repository dumps. The cheaper identity is the lift subject, not the planner.
- Q: What exact artifact does the named check require? → A: The cheaper identity must reply with only the JSON object `{"lift_fact":"<exact planted token>"}`. The independent checker parses that object and passes only if `lift_fact` equals the planted token. Any extra keys, prose, or mismatch fails. The planted token is a synthetic non-secret string written into local docs and/or durable memory before the run; it MUST NOT appear in the unaided task wording.
- Q: Must unaided and packed share a conversation? → A: No. Two independent executions of the same named check on the same cheaper identity. Working state is per attempt. Durable memory is shared (the planted fact). The unaided attempt MUST NOT receive the pack.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Plan slices and retrieve only what the cheaper worker needs (Priority: P1)

An operator has a bounded task that a cheaper model cannot finish from the prompt alone. A planner produces retrieval slices (what to look up: docs, code symbols, durable memories) instead of pasting the repository. Retrieval returns a small set of source-attributed units. Missing required sources are named as omissions, not silently skipped.

**Why this priority**: Without slice-based retrieval, packing either dumps the repo or guesses. This is independently valuable even if no model is called.

**Independent Test**: Plant a unique fact in one doc, one code location, and one durable memory. Ask for slices for a task that needs that fact. Confirm the retrieved set includes the planted units, excludes unrelated bulk files, and records any source category that could not be retrieved.

**Acceptance Scenarios**:

1. **Given** a bounded task and planted facts in docs, code, and memory, **When** slices are planned and retrieval runs, **Then** the retrieved set includes each planted unit with provenance and does not include the whole repository.
2. **Given** a source category with a deterministic default location, **When** retrieval runs, **Then** that category is auto-discovered; if it cannot be read, it is recorded as an omission with a reason.
3. **Given** a source category with no deterministic default location, **When** nothing is supplied, **Then** the limitation is named explicitly rather than treated as complete.
4. **Given** retrieval results, **When** inspected, **Then** each unit names source, freshness or observed time, and trust; secrets and raw transcripts are absent.

---

### User Story 2 - Compile a model-aware pack with typed slots (Priority: P1)

The operator compiles retrieved units into a compact pack sized to the cheaper identity’s context limit. The pack uses typed slots (goal, policy, docs, code, memory, examples) rather than a chat dump. Injection-unsafe content is sanitized or excluded. Every include, transform, exclude, and omit decision is on a receipt that carries no payload secrets.

**Why this priority**: Retrieval without compilation still overflows cheaper models. Compilation is the product that makes the cheaper worker viable.

**Independent Test**: Compile the same retrieved set for a small context limit and a larger one. Confirm the small pack stays within budget, preserves the unique fact required by the named check, records dropped units, and never contains secrets or the whole repository.

**Acceptance Scenarios**:

1. **Given** retrieved units and a cheaper identity’s context limit, **When** compilation runs, **Then** used tokens stay within that limit and the unique required fact is present unless fail-closed (scenario 4).
2. **Given** contradictory units for the same slot key, **When** compilation runs, **Then** the conflict is recorded and the pack does not silently pick one.
3. **Given** content that looks like a credential or injection payload, **When** compilation runs, **Then** it is excluded or sanitized and the decision is on the receipt.
4. **Given** a budget too small for policy plus the unique required fact, **When** compilation runs, **Then** compilation is refused rather than emitting a pack that omits them.
5. **Given** a completed pack, **When** a reviewer inspects the receipt, **Then** they can see included slots, omitted units with reasons, token use vs budget, and no secrets, prompts, or completions.

---

### User Story 3 - Prove cheaper-model lift with a paired live run (Priority: P1)

The same cheaper live identity runs the same named check twice: once unaided, once with the compiled pack. An independent checker scores both. The receipt reports lift (unaided fail and packed success), no-lift (both succeed or both fail), or blocked (live surface unavailable). Fixture-only scoring is not a pass.

**Why this priority**: This is the Phase 3 exit signal. Packing that is never measured against a real cheaper identity is theater.

**Independent Test**: On a reachable live gateway, select a cheaper qualified identity. Run the named check unaided, then with the pack, through that identity. Confirm the checker results, the identity id, and the lift conclusion are on the receipt. Repeat with the live surface down and confirm blocked, not passed.

**Acceptance Scenarios**:

1. **Given** a reachable live gateway and a cheaper qualified identity, **When** the paired run executes, **Then** both attempts use that same concrete identity and the named check (not an open-ended session).
2. **Given** unaided failure and packed success on the independent checker, **When** the receipt is inspected, **Then** the conclusion is lift and names the identity, both outcomes, and the pack digest.
3. **Given** both attempts succeed or both fail, **When** the receipt is inspected, **Then** the conclusion is no-lift (truthful), not a pass claimed as lift.
4. **Given** the live surface is unreachable, **When** the paired run is requested, **Then** the result is blocked, not passed, and fixture scoring is not substituted.
5. **Given** a receipt, **When** inspected, **Then** it contains no secrets, credentials, raw prompts, or completions.

---

### User Story 4 - Keep working state and durable memory distinct (Priority: P2)

During packing, short-term working state holds typed slots for this task only. Durable memory is a searchable archive: operators can ingest a verified fact and later retrieve it by query. Ingest is gated; adapters cannot write policy or unverified authority. Session dump is not ingest.

**Why this priority**: Mixing chat history into the archive (or treating the archive as the prompt) recreates dump-the-repo failure. This story is independently testable without live inference.

**Independent Test**: Ingest a unique fact into durable memory through the gate. Start a new working state with no chat history. Retrieve by query and compile. Confirm the fact is found. Confirm a rejected secret write never appears in search or in the pack.

**Acceptance Scenarios**:

1. **Given** a gated ingest of a unique verified fact, **When** a new working state searches durable memory, **Then** that fact is retrieved with provenance.
2. **Given** a write that contains a secret or raw transcript, **When** ingest is attempted, **Then** it is refused, recorded as a rejection, and never appears in later search or packs.
3. **Given** working state for a task, **When** the task ends, **Then** working slots are not automatically the durable archive; only gated ingest persists.
4. **Given** an optional memory adapter is absent, **When** ingest or search runs, **Then** the local archive still works and the run does not require that adapter.

---

### User Story 5 - Optional compaction without pretending completeness (Priority: P3)

When retrieved units exceed the cheaper identity’s budget, an optional compaction step may summarize lower-priority units. Compaction is model-aware and must record what was summarized versus dropped. It MUST NOT replace required policy or the unique named-check fact with a summary. Compaction is not required for Stories 1–3.

**Why this priority**: Compaction is useful at scale but is not the Phase 3 exit. Honest omission already satisfies the cheaper-model proof.

**Independent Test**: Feed more units than the budget allows. With compaction off, omitted units are listed. With compaction on, summarized units are marked summarized, required fact remains verbatim, and the receipt still names omissions.

**Acceptance Scenarios**:

1. **Given** surplus units and compaction off, **When** compilation runs, **Then** surplus units are omitted with reasons and the required fact remains.
2. **Given** surplus units and compaction on, **When** compilation runs, **Then** summaries never cover required policy or the unique named-check fact, and summarized-vs-dropped is inspectable.
3. **Given** a compaction result, **When** inspected, **Then** it contains no secrets and does not claim the omitted original text is still present.

---

### Edge Cases

- Empty retrieval (no docs, code, or memory hits): do not invent units; named-check pack compile fails closed if the unique required fact is absent.
- Whole-repository or unbounded directory offered as a source: refuse to dump; retrieve only slice-selected units.
- Stale durable memory (age beyond declared freshness): eligible for retrieval only with a stale mark; MUST NOT be treated as fresh evidence.
- Contradictory docs vs memory for the same key: record the conflict; do not silently merge.
- Cheaper identity context limit unknown: treat as unclassified for packing; do not guess a budget; fail closed for the live proof.
- Live catalog has only paid identities: do not use a paid identity as the lift subject while claiming cheaper-model lift; report that no cheaper qualified identity exists.
- Unaided attempt transport failure: do not score as model quality; classify as blocked or operational failure, not no-lift.
- Packed attempt uses a different identity than unaided: invalid pair; do not report lift.
- Planner proposes slices that would dump the repo: reject those slices; keep bounded retrieval.
- Adapter reports success without provenance: fail closed for that unit; do not pack it.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST plan retrieval slices for a bounded task (docs, code, durable memory) instead of attaching a whole repository or a chat transcript. Slice planning MUST be deterministic from the task and available source categories. A stronger identity MAY propose additional slices; Core MUST still bound them and MUST refuse slices that would dump a repository.
- **FR-001a**: The named check artifact is exactly JSON `{"lift_fact":"<exact planted token>"}`. The independent checker MUST parse JSON and pass only if `lift_fact` equals the planted token. Extra keys, prose, or mismatch MUST fail. The planted token MUST be a synthetic non-secret string written into local docs and/or durable memory before the run and MUST NOT appear in the unaided task wording.
- **FR-001b**: Unaided and packed attempts MUST be independent executions of the same named check on the same cheaper identity. Working state MUST be per attempt. Durable memory MAY be shared. The unaided attempt MUST NOT receive the pack.
- **FR-002**: The system MUST retrieve only slice-selected units and MUST attribute each unit with source, observed time or freshness, and trust.
- **FR-003**: The system MUST auto-discover source categories that have a deterministic default location and MUST record an omission with a reason when a category cannot be retrieved.
- **FR-004**: A category without a deterministic default location MAY stay caller-supplied; that limitation MUST be named explicitly and MUST NOT be treated as complete.
- **FR-005**: Retrieval adapters MUST NOT own admission, exclusion, or policy. Core MUST apply policy after retrieval. Missing required evidence MUST fail closed.
- **FR-006**: The system MUST compile a model-aware pack from retrieved units into typed slots (at minimum: goal, policy, docs, code, memory) sized to the cheaper identity’s fetched context limit.
- **FR-007**: The pack MUST NOT be a chat-history dump. Typed slots MUST be addressable independently (working-state injection by slot name).
- **FR-008**: Compilation MUST stay within the cheaper identity’s context limit. Surplus units MUST be omitted with reasons or, if optional compaction is enabled, summarized without claiming the original text is present.
- **FR-009**: Required policy units and the unique fact required by the named check MUST be retained. If they cannot fit, compilation MUST be refused.
- **FR-010**: Injection-unsafe content and secrets MUST be excluded or sanitized. Packs and receipts MUST NOT contain secrets, credentials, raw prompts, completions, or raw tool arguments.
- **FR-011**: Contradictory units for the same slot key MUST be recorded as conflicts. The pack MUST NOT silently pick a winner.
- **FR-012**: Every include, transform, exclude, omit, and (if used) summarize decision MUST appear on a payload-free pack receipt bound to a pack digest.
- **FR-013**: Working state MUST hold the current task’s typed slots only. Durable memory MUST be a searchable archive with gated ingest and query retrieval. Completing a task MUST NOT automatically ingest the working state.
- **FR-014**: Durable ingest MUST refuse secrets and raw transcripts, persist the refusal as evidence, and MUST NOT later retrieve refused content.
- **FR-015**: No particular memory, graph, vector, or vendor runtime MAY be required. Local archive, local docs, and local code MUST be sufficient for packing and for the paired evaluation.
- **FR-016**: The named check MUST be the FR-001a artifact plus an independent checker. The correct artifact MUST depend on the planted token present in retrieved units and MUST NOT be guessable from the task wording alone.
- **FR-017**: The live proof MUST select a cheaper qualified concrete identity from the existing live cheaper-first path (local, then free, then cheaper). A paid identity MUST NOT be the lift subject while a cheaper unused qualified identity remains.
- **FR-018**: The same cheaper identity MUST run the named check twice: unaided, then with the compiled pack. The pair is invalid if the identity differs or if either attempt is a fixture stub.
- **FR-019**: Lift MUST be reported only when unaided fails the checker and packed succeeds. Both succeed or both fail MUST be reported as no-lift. Transport, quota, and catalog failures MUST be reported as blocked or operational, never as model quality or as lift.
- **FR-020**: If the live surface is unreachable, the paired evaluation MUST be blocked and MUST NOT be reported as passed. Fixture retrieval MAY test compilation rules and MUST NOT satisfy SC-004.
- **FR-021**: Optional compaction MUST preserve provenance of summarized and dropped units, MUST NOT replace required policy or the unique named-check fact, and MUST NOT be required for Stories 1–3.
- **FR-022**: Selection, packing, and lift conclusions for the same sources, task, identity, and policy MUST be deterministic.

### Key Entities

- **Retrieval slice**: A bounded lookup request naming what to pull (a doc class, a code symbol, or a memory query) rather than a directory dump.
- **Context unit**: One source-attributed item with content, source, freshness or observed time, trust, and slot type.
- **Working state**: Typed slots for the current task only (goal, slices, retrieved units, pack digest). Not a transcript.
- **Durable memory record**: A gated, provenance-bearing archive item that can be ingested and later searched across tasks.
- **Context pack**: The compiled, budgeted rendering of units into typed slots for one cheaper identity.
- **Pack receipt**: Payload-free record of include/exclude/omit/summarize decisions, token use vs budget, conflicts, omissions, and pack digest.
- **Named check**: Ask the cheaper identity to reply with only `{"lift_fact":"<exact planted token>"}`. The checker passes iff that JSON parses and `lift_fact` equals the planted token.
- **Paired lift receipt**: Record of the cheaper identity, unaided outcome, packed outcome, pack digest, and lift / no-lift / blocked conclusion. No secrets.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: For a planted unique fact in docs, code, or memory, retrieval includes that fact and excludes at least 90% of files in a repository that has 20 or more files, measured by unit count vs file count.
- **SC-002**: 100% of compiled packs for a declared context limit stay within that limit, and 100% of omitted units have a recorded reason.
- **SC-003**: A reviewer who is not a developer, given only the pack receipt, correctly names at least one included source, one omission or drop reason, and whether the unique required fact was kept, in under 5 minutes.
- **SC-004**: On a reachable live cheaper identity, the paired named check produces an inspectable lift or no-lift conclusion that names the identity, both checker results, and the pack digest. A down live surface is blocked, not passed.
- **SC-005**: Lift is claimed only when unaided fails and packed succeeds on the independent checker. Zero lift claims from fixture stubs or from swapped identities.
- **SC-006**: 100% of packs and receipts pass a sensitive-payload negative suite — zero secrets, credentials, raw prompts, completions, or tool arguments.
- **SC-007**: Repeating the same sources, task, identity, and policy twice produces the same pack digest and the same lift conclusion class.

## Assumptions

- Feature 276 live cheaper-first execute remains the way to obtain a cheaper concrete identity. This feature does not weaken catalog, probe, or cost-class rules.
- Existing pack compilation, envelope, memory archive, memory write gate, documentation inventory, and code-intelligence retrieval are reused rather than replaced.
- “Cheaper identity” means a kept local, free, or cheaper concrete identity from live routing. Unknown cost class remains unclassified and is not the lift subject.
- The named check is small enough to rerun. It is not an open-ended coding session.
- Unique planted facts used in tests and live proof are synthetic and non-secret.
- Optional graph, vector, or vendor memory adapters may add units when present; their absence is not a defect.
- Spec 272 Phases 4–5 (learning and swarms) remain out of scope.
- Secrets never belong in packs, receipts, working state, or durable memory.
