# Definition of done verifies orchestration run truth

## Why
KNOWN: `scripts/check_dod.py` currently trusts recorded exit codes and timestamps even when an orchestration result JSON proves they are false.

INFERRED: Structured stdout is the authoritative result for an orchestrate acceptance run.

NOT AVAILABLE: Run bundle directory verification is not part of this change.

## What Changes
- Parse raw output that is a Verdict orchestration result and validate outcome, review status, and the recorded run time against the result.
- Reject reuse of one raw-output file, one structured-result digest or one `run_id` for two evidence runs.
- Judge the parent confirmation and the structured-stdout requirement by the acceptance item's expected exit code.
- Preserve existing substring checks for non-result raw output.
- Document the stronger orchestration evidence rule.

## Acceptance Criteria
- Structured results enforce outcome/exit agreement, result-window time, and PASS review for zero exit.
- Distinct acceptance and parent-confirmation runs require distinct raw-output files, and distinct digests and run ids when they are structured results.
- An honest story whose acceptance is a blocked run can pass.
- Plain text and unrelated JSON retain existing behavior.
- Existing repository validators and tests pass.

## Capabilities
### Modified Capabilities
- `repository-tooling`: extend the definition-of-done gate with run-truth validation.

## Impact
`scripts/check_dod.py`, `tests/test_check_dod.py`, `AGENTS.md`, and this change. No evidence bundles are modified.
