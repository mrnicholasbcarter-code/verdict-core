# Design

## Context
- `proof/dod.schema.json` requires `openspec.change`, and `_openspec()` in
  `scripts/check_dod.py` fails unless that change is archived with every task
  ticked.
- AGENTS.md ("Workflow") asks for an OpenSpec change for behaviour changes and
  an ADR for architectural ones. A dead-code removal has neither.
- `setup_doctor` already has a second form, `{"status": "not_applicable",
  "reason": ...}`, for stories with no service.
- No evidence file is on main yet, so no existing file is affected.

## Goals / Non-Goals

**Goals:**
- Let a story with no OpenSpec change pass the check on its real evidence.
- Fail the claim when the repository itself shows a change belongs to the story.

**Non-Goals:**
- Deciding whether a given story should have had an OpenSpec change. That stays
  with review and the parent confirmation.
- Relaxing any other part of the definition of done.
- Changing `scripts/check_openspec_tasks.py` or the archive rule.

## Decisions
- **Same shape as `setup_doctor`.** `openspec` is one of `{"change": <name>}`
  or `{"status": "not_applicable", "reason": <text, 10+ characters>}`. Mixed or
  partial objects fail the schema.
- **The merged commit must not touch `openspec/`.** A story whose merge changed
  a spec or a change directory has an OpenSpec change by definition. The check
  uses the same first-parent file list as the existing `repo-tooling` rule.
- **No change may be named after the story's ticket.** The ticket is the
  leading `<letters>-<digits>` of the story name. A change directory, active or
  archived (date prefix removed), that starts with that ticket, with or without
  the dash and not followed by another digit, fails the claim. `bod-7` does not
  match `bod-70-...`. A story name with no ticket is matched by its whole name.
- **`--all` is unchanged.** An evidence file with no change names none, so an
  archived change still needs its own evidence file.

Alternatives considered:
- Requiring a small OpenSpec change for every story. Rejected: the schema's own
  instruction says not to invent a requirement to satisfy validation, and the
  operator rule is to never write a change only to pass a checker.
- Allowing the form only for `repo-tooling` stories. Rejected: BOD-384, BOD-397
  and BOD-413 are product stories.

## Test strategy
`tests/test_check_dod.py`, in a temporary git repository:
- a story with the new form passes;
- it fails when the merged commit changed `openspec/changes/...` or
  `openspec/specs/...`;
- it fails when a change named after the ticket exists (active with and without
  the dash, bare, and archived), and passes beside another ticket's change;
- partial, mixed and short-reason objects fail the schema;
- `--all` reports the story DONE and still fails on an unproven archive.

Each guard is checked by a temporary edit that removes it; the matching tests
must turn red.

## Risks / Trade-offs
- A behaviour-changing story with no change at all can use the new form. The
  check cannot judge behaviour; the reason is recorded in the evidence file and
  read by the parent who confirms the story.
- The name rule depends on changes being named after their ticket. A change
  with an unrelated name is not caught by the name rule, only by the
  merged-commit rule.
