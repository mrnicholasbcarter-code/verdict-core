# Native community roster discovery and qualification

## Why

The community adapter returned zero rows for real upstream rosters, and external catalog rows did not feed the native screen. This hid potentially useful capacity from Verdict's evidence-backed selection.

## What Changes

- Parse the current roster by immutable revision, retain full provider/model identity, and reject broken or empty source results.
- Add a read-only roster view and a roster-scoped native screen. Keep unmatched rows and report why a route was not screened.
- Separate external list pricing from send authorization and measured capability. An operator declaration of unmetered connections applies only to the opted-in screen. Known refusals remain exclusions; model-specific refusals do not exclude unrelated models.

## Capabilities

### Modified Capabilities

- `identify-maintenance`: Report roster coverage and qualify matching routes through the native screen.

## Impact

This change does not install credentials, alter timers, register gateway providers, or bypass task admission. A live coding-task receipt is separate from parser and tool-probe evidence.

## Evidence boundary

- **KNOWN:** The old parser returned zero rows from both the pinned and current upstream files. The repaired adapter reads 257 pinned entries and 222 current entries. A native live screen on an isolated cache produced exact-route tool evidence; a separate eligibility read reached `TASK_ELIGIBLE` for the same route.
- **INFERRED:** Feeding current community entries to the existing screen should increase the pool of qualified routes as missing models are registered and tested; inventory alone does not establish this gain.
- **NOT AVAILABLE:** A verified coding-task receipt using this discovered route on installed main, full external catalog coverage, and unattended qualification of all accessible routes.

## Acceptance criteria

- Frozen upstream data parses without executing JavaScript, preserves nested IDs, and fails visibly on an invalid or empty roster.
- The native report accounts for every parsed roster entry; unmatched, unavailable, refused and budget-deferred routes stay visible.
- The native screen can measure capability without treating a model list price as a failed capability test or expanding spend authority by default.
- A model-scoped payment refusal does not stop a sibling route. Account-wide failures and rate limits still stop the relevant pool.
- Prove the installed discovery-to-task path separately after merge; do not count a roster match or an eligibility result as a completed coding task.
