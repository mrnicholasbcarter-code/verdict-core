# Data Model: Recruiter-Ready README and Proof Demo

This feature adds no database or durable application model. These records describe the documentation and evidence contracts that implementation and validation must keep aligned.

## Flagship Fixture Result

| Field | Rule |
|---|---|
| selected route | Exactly `demo/frontier-tools` for the specified deterministic fixture |
| exclusions | Exactly three entries with candidate name and canonical reason |
| receipt identifier | Stable synthetic identifier `fixture:issue-35` |
| receipt mode | `deterministic_fixture`; never represented as a live receipt |
| status | Successful only after fixture validation passes |

**Relationships**: One fixture result supplies the installed CLI report, README proof block, `quickstart.sh` verification, and focused tests.

**Validation**: The README block must equal the canonical human-readable report under the documented normalization rule. No provider credentials, network access, or application-state write may influence it.

## README Proof Block

| Field | Rule |
|---|---|
| command | `verdict quickstart --non-interactive --dry-run` |
| output | Exact canonical report, not a hand-maintained subset |
| ordering | Positioning, install, proof command/output, named reasons/receipt, cost comparison, architecture |
| claim boundary | Fixture, mock estimate, and dated live observation remain distinct |

**State transition**: `unverified` → `matched` only after the regression test compares the checked-in block with actual renderer output at the same source revision.

## README Link Target

| Field | Rule |
|---|---|
| raw destination | Markdown link destination from README |
| path | Local repository-relative target, when applicable |
| fragment | Optional normalized heading fragment |
| result | `resolved` or `broken` |

**Validation**: Every changed local destination must resolve both its file path and fragment. External links are reported separately and must not be silently treated as locally verified.

## Evidence Record

| Field | Rule |
|---|---|
| evidence type | clean-install transcript, terminal capture, link check, automated skim structure, human skim, focused checks, or remote PR gates |
| source revision | Exact commit SHA or immutable dirty-worktree snapshot |
| command/check | Actual command or authoritative check URL/name |
| outcome | `pass`, `fail`, `blocked`, `not-run`, or `not-applicable` |
| artifact pointer | Repository path or durable external link when one exists |
| limitations | State what the evidence does not establish |
| reviewer/time | Record only observed reviewer identity/date/time; do not synthesize |

**State transition**: `not-run` → `pass`/`fail` after observation, or `blocked` with the reason. Automated evidence never transitions the human-skim record to `pass`.
