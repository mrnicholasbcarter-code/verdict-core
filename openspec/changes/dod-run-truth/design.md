# Design

## Context
The DoD checker validates raw output markers but does not interpret the structured JSON emitted by `verdict orchestrate`.

## Goals / Non-Goals

**Goals:**
- Validate only structured orchestration result objects identified by the required result keys.
- Compare recorded exit code, evidence timestamp, outcome, and review status fail-closed.
- Keep plain text and unrelated JSON behavior unchanged.

**Non-Goals:**
- Run bundle directory verification and changes to receipt verification.
- Reinterpret arbitrary JSON or transcripts.

## Decisions
The command matcher is deliberately broad because a false match can only add a requirement, never remove one. A raw output is a structured result only when its complete stdout, after allowing trailing whitespace, is a top-level JSON object containing `outcome`, `run_id`, `started_at`, `finished_at`, and `events_digest`. For commands matching `verdict orchestrate --json`, this shape is mandatory; leading noise is rejected because stdout must be the command's result JSON. Structured results require `COMPLETE` exactly when the recorded exit code is zero, require review status `PASS` for zero exit, and require the recorded `at` timestamp to be within five minutes of the result interval. Structured parent confirmations have no exit code and instead require COMPLETE and PASS. Malformed or other JSON remains ordinary raw output.

The checker compares raw artifact SHA-256 values across acceptance runs and parent confirmation. Any duplicate digest used for two distinct runs fails with `the same raw output cannot serve as two runs`; setup and on-main artifacts are not acceptance runs.

## Test strategy
Synthetic result JSON fixtures exercise each truth rule in both failing and passing forms, duplicate acceptance/confirmation output, and non-JSON text. Existing fixture helpers continue to create temporary git repositories and evidence files.

## Security / trust implications
Raw output remains untrusted evidence. Parsing is stdlib JSON only, with no execution or provider calls; digest and redaction checks remain mandatory.

## Routing / context / memory implications
No routing, model selection, context packing, or memory behavior changes. The checker consumes only evidence artifacts already selected by the evidence file.

## Migration / rollback
No evidence migration is required. Existing non-structured artifacts continue to pass unchanged; rollback is a single script/test/docs revert.

## ADR impact
No ADR is needed: this is a narrow fail-closed repository gate extension under the existing repository-tooling capability.

## Limits
The gate checks that a recorded run is internally consistent and matches its own result; it cannot prove the result JSON was produced by the command, because a hand-edited valid result would pass. Duplicate detection compares file digests, so two different files describing the same `run_id` require the separate run-id check.

The gate keys on the recorded command text; an author who records a different command string is not covered, and reviewers must check that the recorded command is the acceptance command. Shell aliases and wrapper scripts that hide the command are out of scope.

## Risks / Trade-offs
Structured output must be recognized conservatively to avoid changing historical transcript behavior. The five-minute tolerance accommodates clock and recording skew while still rejecting fabricated timestamps.
