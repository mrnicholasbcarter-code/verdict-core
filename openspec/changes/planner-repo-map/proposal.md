# Planner always receives the repo structure map

## Why

`verdict orchestrate` plans owned files for each worker node. In the first
self-build run (#1269), the planner assigned the wrong owned files for the CLI
node because it lacked the repository structure map. The AGENTS.md "Where
things live" section was passed manually as `--constraints-file` in the second
run and fixed the assignments. This story makes that map automatic.

Correct owned-file assignments reduce blocked attempts and wasted model calls,
directly advancing the mission metric (accepted work per paid dollar).

## What Changes

- `repo_map()` in `verdict/orchestration/planner.py` is updated to:
  - Sort files by the `include` tuple order first (so `verdict/` appears before
    `tests/`), then alphabetically within each group. The previous global sort
    filled the 400-file cap with `docs/` and `tests/` files, leaving `verdict/`
    out entirely.
  - Prepend the AGENTS.md "Where things live" section (capped at 4 096 bytes)
    when it exists in the repo root. The section is sourced by a plain file
    read; no network or model call is made.
- `FrontierPlanner.plan()` emits a `plan_context` event immediately after
  building the repo map, recording `repo_map_sha256` so the receipt shows
  which map the planner received.
- `plan_context` is added to the `EVENT_TYPES` allow-list in
  `verdict/orchestration/contracts.py` and to the `planner_attempts` section
  built by `verdict/orchestration/receipt.py`.

No change to `--constraints-file`, which continues to supply additional
operator guidance under `CONSTRAINTS:` in the planner prompt.

## Capabilities

### Modified Capabilities

- `orchestrate-plan-decomposition`: the planner now always receives the repo
  structure map without any `--constraints-file` needed.

## Impact

Affected files:
- `verdict/orchestration/planner.py` (repo_map, FrontierPlanner.plan)
- `verdict/orchestration/contracts.py` (EVENT_TYPES)
- `verdict/orchestration/receipt.py` (planner_attempts filter)
- `docs/CLI_REFERENCE.md` (--constraints-file description updated)

No change to orchestrate's public interface, the receipt schema, or vgate.

## Evidence boundary

- **KNOWN:**
  - `repo_map()` calls `git ls-files` via subprocess; no network or model calls.
  - The 400-file cap fills with `docs/` before `verdict/` on the current repo
    (379 verdict/, 641 tests/, 161 docs/ files).
  - `FrontierPlanner.plan()` already takes `events: Any | None = None`.
  - `plan_started`, `plan_repair_started`, `plan_repair_terminal` are already
    in `planner_attempts`; `plan_context` follows the same pattern.

- **NOT AVAILABLE:** a measured saving from the ordering fix alone; the SB1
  findings confirm the wrong owned files were assigned without the map.

## Acceptance criteria

- On installed main, an `orchestrate` run for a CLI-adding change plans owned
  files under `verdict/commands/`. Its receipt shows `repo_map_sha256` in
  `planner_attempts`, with no `--constraints-file` passed.
