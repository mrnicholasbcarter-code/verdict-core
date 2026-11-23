# Spec Delta — planner-repo-map

## ADDED Requirements

### Requirement: Planner prompt always includes the repo structure map

`verdict orchestrate` SHALL build a repo structure map before each planning
call and include it in the planner prompt under `REPO MAP:`. The map SHALL
consist of the AGENTS.md "Where things live" section (when present) followed
by a compact file listing. No network or model call SHALL be made to build it.

#### Scenario: Repo with AGENTS.md "Where things live"
- **GIVEN** `AGENTS.md` exists in the repository root with a "## Where things
  live" section
- **WHEN** a planning call is made
- **THEN** the planner prompt's `REPO MAP:` section includes the "Where things
  live" table from AGENTS.md

#### Scenario: Repo without AGENTS.md
- **GIVEN** `AGENTS.md` does not exist in the repository root
- **WHEN** a planning call is made
- **THEN** the planner prompt's `REPO MAP:` section includes only the file
  listing, without error

#### Scenario: AGENTS.md lacks the section
- **GIVEN** `AGENTS.md` exists but has no "## Where things live" heading
- **WHEN** a planning call is made
- **THEN** the planner prompt's `REPO MAP:` section includes only the file
  listing, without error

### Requirement: File listing is ordered by include-tuple prefix

The file listing in `REPO MAP:` SHALL list files under the first include prefix
(default `verdict/`) before files under the second prefix (`tests/`), and so
on, sorted alphabetically within each group and capped at `max_files` total.
Alphabetical-only ordering across all prefixes SHALL NOT be used.

#### Scenario: verdict/ and tests/ files present
- **WHEN** both `verdict/` and `tests/` files are tracked
- **THEN** all `verdict/` file entries appear before any `tests/` file entries
  in the listing

### Requirement: Receipt records repo-map digest per planning attempt

For each planning attempt, the receipt's `planner_attempts` section SHALL
include a `plan_context` entry carrying `repo_map_sha256`: the SHA-256 hex
digest of the repo map text the planner received.

#### Scenario: Completed orchestrate run
- **WHEN** planning succeeds
- **THEN** `receipt.json` contains a `plan_context` entry in
  `planner_attempts` with a 64-character hex `repo_map_sha256`

## UNCHANGED Invariants

- Building the repo map makes no network or model calls (deterministic from
  `git ls-files` and local file reads).
- The map text itself is not stored in `events.jsonl`; only its SHA-256 is
  recorded (`plan_context` event).
- `--constraints-file` continues to populate `CONSTRAINTS:` in the planner
  prompt; `run_started` continues to record only its SHA-256.
