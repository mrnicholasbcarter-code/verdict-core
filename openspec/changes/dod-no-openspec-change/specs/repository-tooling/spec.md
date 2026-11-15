# Spec Delta

## Purpose
CI gates for this repository that keep OpenSpec task state and the executable
definition of done honest, so a change cannot merge, be called done or be
archived on unfinished work.

## ADDED Requirements

### Requirement: A story with no OpenSpec change can still be proven done
The definition-of-done check MUST accept an evidence file that names no
OpenSpec change only when it states `not_applicable` with a reason of at least
10 characters. It MUST fail that file when the merged commit changed any path
under `openspec/`, or when an active or archived change is named after the
story's ticket. Every other part of the check MUST apply unchanged. An
evidence file that names a change MUST be checked as before, and checking every
evidence file MUST still fail on a change archived without an evidence file
that names it.

#### Scenario: A story with no change passes on its evidence
- **WHEN** an evidence file states `not_applicable` with a reason, the merged
  commit changed nothing under `openspec/`, and no change is named after the
  story
- **THEN** the definition-of-done check reports the story done
- **TEST** `tests/test_check_dod.py::test_story_with_no_openspec_change_is_done`

#### Scenario: The merged commit changed OpenSpec files
- **WHEN** the evidence file states `not_applicable` and the merged commit
  changed a file under `openspec/changes/` or `openspec/specs/`
- **THEN** the check fails and names the changed path
- **TEST** `tests/test_check_dod.py::test_no_change_claim_fails_when_the_merged_commit_changed_openspec`

#### Scenario: A change is named after the story
- **WHEN** the evidence file states `not_applicable` and an active or archived
  change starts with the story's ticket
- **THEN** the check fails and names that change
- **TEST** `tests/test_check_dod.py::test_no_change_claim_fails_when_a_change_is_named_after_the_story`

#### Scenario: Another ticket's change does not count
- **WHEN** the only similar change belongs to a ticket whose number merely
  starts with the story's number
- **THEN** the check reports the story done
- **TEST** `tests/test_check_dod.py::test_a_change_for_another_ticket_does_not_block_a_no_change_story`

#### Scenario: The claim needs its exact form
- **WHEN** the `openspec` entry is empty, lacks the reason, has a reason under
  10 characters, uses another status, or mixes a change name with the status
- **THEN** the check fails on the `openspec` entry
- **TEST** `tests/test_check_dod.py::test_no_change_needs_the_exact_shape_and_a_reason`

#### Scenario: An archived change still needs its own evidence
- **WHEN** every evidence file is checked, one names no change, and a change
  was archived that no evidence file names
- **THEN** the check reports the story done and still exits 1 for the archive
- **TEST** `tests/test_check_dod.py::test_all_still_flags_an_unproven_archive_beside_a_no_change_story`
