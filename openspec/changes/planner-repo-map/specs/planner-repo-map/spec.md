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

### Requirement: The AGENTS.md section is size-bounded and trusted as repository content

The AGENTS.md "Where things live" text SHALL be capped at 4 096 bytes of UTF-8,
cut on a character boundary, before it is added to the planner prompt. The cap
bounds size only: it is not injection protection. The remaining text is
forwarded to the planning model verbatim. `AGENTS.md` is read from
`<repo>/AGENTS.md` with a plain file read that follows symlinks, and the code
does not refuse a symlink or check that its target is inside the repository.
The repository passed to `orchestrate` is therefore trusted as the operator's
own; a hostile or symlinked `AGENTS.md` is neither detected nor refused.

#### Scenario: Section larger than the cap
- **GIVEN** a "Where things live" section of multi-byte text larger than
  4 096 bytes
- **WHEN** the repo map is built
- **THEN** the section is cut to at most 4 096 bytes, ends on a whole
  character, and is not dropped

### Requirement: Receipt records the repo-map digest for each planning call

Each `FrontierPlanner.plan()` call SHALL emit one `plan_context` event, before
its first model call, carrying `repo_map_sha256`: the SHA-256 hex digest of the
repo map text placed in that call's prompt. The receipt's `planner_attempts`
section SHALL include that entry, in `seq` order with the other planner events.
A validation-repair call made inside the same `plan()` call re-sends the same
prompt (the planning prompt plus the validation error), so it carries the same
repo map, and SHALL NOT emit a second `plan_context`.

#### Scenario: Completed orchestrate run
- **WHEN** planning succeeds
- **THEN** `receipt.json` contains a `plan_context` entry in
  `planner_attempts` with a 64-character hex `repo_map_sha256` that equals the
  SHA-256 of the repo map in the prompt the planner received

#### Scenario: Validation repair
- **GIVEN** the first planner answer fails validation
- **WHEN** the repair call runs
- **THEN** the repair prompt contains the same repo map as the first call
- **AND** that `plan()` call has recorded exactly one `plan_context`

## UNCHANGED Invariants

- Building the repo map makes no network or model calls (deterministic from
  `git ls-files` and local file reads).
- The map text itself is not stored in `events.jsonl`; only its SHA-256 is
  recorded (`plan_context` event).
- `--constraints-file` continues to populate `CONSTRAINTS:` in the planner
  prompt; `run_started` continues to record only its SHA-256.
