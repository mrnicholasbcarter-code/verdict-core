# Definition of done for a story with no OpenSpec change

## Why
`scripts/check_dod.py` requires every evidence file to name an archived
OpenSpec change (`openspec.change`). AGENTS.md asks for an OpenSpec change only
for behaviour changes, so a story that removes dead code or fixes a defect
inside an existing spec has no change to name. Such a story can merge and run
on installed main, but it can never pass the check. BOD-384 (#1009), BOD-397
(#1010) and BOD-413 (#1013, #1014) merged on 2026-11-14 and are stuck there.
The operator decided on 2026-11-14: the check accepts a story with no OpenSpec
change.

## What Changes
- An evidence file may say `"openspec": {"status": "not_applicable",
  "reason": "<why>"}` instead of naming a change. The reason needs at least 10
  characters.
- `scripts/check_dod.py` fails that claim when the merged commit changed any
  path under `openspec/`, or when a change, active or archived, is named after
  the story's ticket (`bod-384-...` or `bod384-...`).
- Every other part of the definition of done is unchanged: real runs from
  merged main, the freshness window, setup/doctor, green required checks and a
  parent confirmation by someone else.
- A story that names a change is checked exactly as before. `--all` still
  fails on a change archived without an evidence file that names it.
- AGENTS.md "Definition of done" part 5, CONTRIBUTING.md and the script's
  docstring describe the new form.

## Capabilities
### New Capabilities
- `repository-tooling`: the CI gates that keep OpenSpec task state and the
  definition of done honest. The active change `post-merge-live-tasks`
  introduces this capability; this change adds one requirement to it.

### Modified Capabilities
None.

## Impact
`scripts/check_dod.py`, `proof/dod.schema.json`, `tests/test_check_dod.py`,
AGENTS.md and CONTRIBUTING.md. No product code under `verdict/` changes.
Stories with no OpenSpec change can be recorded as done once their evidence
passes; no existing evidence file changes meaning.
