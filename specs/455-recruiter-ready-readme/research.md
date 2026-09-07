# Research: Recruiter-Ready README and Proof Demo

## Decision 1: Treat the human-readable CLI renderer as the output authority

**Decision**: Compare the README proof block mechanically with the output produced by the existing deterministic fixture renderer.

**Rationale**: Issue #455 requires the example to come from a tested executable fixture and prevents README/script drift. Exact comparison catches omitted, reordered, or stale lines rather than checking only selected substrings.

**Alternatives considered**:
- Duplicate expected lines in tests: rejected because it creates another source that can drift.
- Assert only the receipt and PASS lines: rejected because it would not catch the current partial-copy problem.
- Add a second renderer for README output: rejected because the specification requires one shared fixture result.

## Decision 2: Validate both relative paths and Markdown fragments

**Decision**: Extend repository-native documentation tests or a focused helper to parse changed README links, verify local files exist, and verify referenced headings/fragments resolve.

**Rationale**: File-existence checks do not catch renamed anchors, while FR-010 and SC-005 explicitly require fragment validation.

**Alternatives considered**:
- Manual link inspection only: rejected because it is not regression protection.
- External link-check service: rejected because no new dependency or network requirement is needed for local targets.

## Decision 3: Collect closure evidence prospectively from the final revision

**Decision**: After implementation and focused checks pass, run the install-and-proof journey in a fresh isolated environment and capture the actual terminal session. Store or link only real output bound to the tested revision.

**Rationale**: Issue #455 requires a fresh-environment transcript and terminal recording/GIF matching the shipped example. The constitution forbids inferred or invented evidence.

**Alternatives considered**:
- Reconstruct a transcript from expected output: rejected as fabricated evidence.
- Reuse a recording from an earlier revision: rejected unless it can be proven to match the final shipped output exactly.
- Claim CI as the terminal recording: rejected because CI success and a reader-visible recording are different evidence.

## Decision 4: Separate automated and human 90-second evidence

**Decision**: Automate structural checks for ordering, command count, exact output, links, and claims; record the manual 90-second skim only when a real reviewer performs it.

**Rationale**: Automation can establish objective structure but cannot truthfully claim human comprehension or elapsed skim time.

**Alternatives considered**:
- Infer human comprehension from README length: rejected as unsupported.
- Mark the criterion passed when automated checks pass: rejected because it conflates distinct evidence classes.

## Decision 5: Avoid runtime behavior expansion

**Decision**: Preserve the deterministic fixture and public CLI behavior; prefer README and test corrections. Modify `verdict/flagship_demo.py` only if needed to expose one canonical stable report contract already required by the spec.

**Rationale**: The story concerns discoverability and proof alignment, not a new routing engine or provider integration.

**Alternatives considered**:
- Add live-provider execution: explicitly out of scope.
- Introduce generated documentation infrastructure: rejected unless exact comparison cannot be implemented simply with current tests.
