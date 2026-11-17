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
The command matcher is deliberately broad because a false match can only add a requirement, never remove one. A raw output is a structured result only when its complete stdout, after allowing trailing whitespace, is a top-level JSON object containing `outcome`, `run_id`, `started_at`, `finished_at`, and `events_digest`. For commands matching `verdict orchestrate --json`, this shape is mandatory; leading noise is rejected because stdout must be the command's result JSON. Structured results require `COMPLETE` exactly when the recorded exit code is zero, require review status `PASS` for zero exit, and require the recorded `at` timestamp to be within five minutes of the result interval. Malformed or other JSON remains ordinary raw output.

The matcher follows what the command line really accepts. `verdict orchestrate` takes every unambiguous prefix of `--json` (`--j`, `--js`, `--jso` all set the JSON flag), so the matcher treats any prefix of three or more characters as the flag. `python -m verdict.cli` runs the same entry point as `python -m verdict`. Shell punctuation is split from the words it touches (`--json>out.json`, `--json;`, a subshell). A recorded command is not always valid shell: evidence already records placeholders such as `<scratch-repo>`, and a goal can hold an apostrophe. When the command cannot be parsed, the matcher splits on whitespace and shell punctuation instead, so a parse failure adds the requirement and never removes it.

The expected exit code of the acceptance item governs two things. First, the structured-stdout requirement: `verdict orchestrate` prints its result when it exits 0 or 1, and every other exit code is a startup block that prints one line to stderr, so an item that expects such a code is checked as plain text. Second, the parent confirmation, which records no exit code: when the item it re-runs expects 0 the confirmation must be COMPLETE with review PASS; when the item expects a non-zero code the confirmation must not be COMPLETE and its review is not checked. Without this, an honest story whose acceptance is a blocked run could never be done. When two items record the same command with different expected exit codes, the last item decides, as it already does for the expected text.

The checker rejects one raw-output file used for two runs, and rejects one `run_id` used for two runs. For structured results it also rejects one SHA-256 used for two runs, because a result carries its run's id and times. It does not apply the digest rule to non-structured output: a deterministic command prints the same bytes on every run, and that evidence passed before this change. One run cited by two acceptance criteria (same `at`, command, path and digest) counts once; the first and fresh run of one criterion are always two runs. Both messages name the two run locations. Setup and on-main artifacts are not acceptance runs. The uniqueness pass reuses the text read by the first pass, so each artifact error is reported once.

No raw output may make the checker raise. `json.loads` raises `JSONDecodeError`, plain `ValueError` for an integer over the digit limit and `RecursionError` for deep nesting; all three mean "not a structured result". Date arithmetic at the edge of the calendar raises `OverflowError`, reported as an invalid structured result time. A `run_id` that is not a non-empty string is an error. Error lines that quote raw output are printed with characters the terminal cannot encode escaped.

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

The gate keys on the recorded command text; an author who records a different command string is not covered, and reviewers must check that the recorded command is the acceptance command. Shell aliases and wrapper scripts that hide the command are out of scope. So are a command passed as one quoted string (`bash -c '...'`) and a variable or command substitution in place of `verdict`.

## Risks / Trade-offs
Structured output must be recognized conservatively to avoid changing historical transcript behavior. The five-minute tolerance accommodates clock and recording skew while still rejecting fabricated timestamps.
