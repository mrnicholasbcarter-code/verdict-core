# Design — planner-repo-map

## Context

`repo_map()` is called in `FrontierPlanner.plan()` and its output goes into the
`REPO MAP:` section of the planning prompt. Currently it sorts all matched files
alphabetically across all include prefixes. Because `d < t < v`, `docs/` and
`tests/` files fill the 400-file cap before any `verdict/` file appears. The
planner receives a map with no `verdict/` structure, so it cannot correctly
assign owned files in that tree.

Separately, the AGENTS.md "Where things live" section names which file paths
hold each subsystem. Without it the planner can only guess at structure even
with correct file listings. The SB1 self-build (20261123T145400Z) confirmed
this root cause; run 2 passed the section as `--constraints-file` and fixed the
owned-file assignments.

## Goals / Non-Goals

Goals:
- `verdict/` files always appear in the repo map before `tests/` files;
- AGENTS.md "Where things live" section appears in the planner prompt
  automatically, without any `--constraints-file`;
- the receipt records which repo map the planner received (sha256);
- no network or model call is added to the planning path.

Non-goals:
- code-graph file facts (MCP/model calls at planning time — deferred);
- changing the planning prompt's overall structure or the JSON contract;
- removing `--constraints-file` (improvement.py still uses it for lessons).

## Decisions

1. **Include-order sorting.** Files are grouped by prefix in include-tuple order
   then sorted within each group, then capped. `by_prefix[p].extend(sorted(...))`
   for each `p in include` before the `[:max_files]` slice.

2. **AGENTS.md section.** `_agents_md_where_things_live(root)` reads
   `root/AGENTS.md`, extracts the "## Where things live" block up to the next
   same-level heading or end-of-file, caps at 4 096 bytes, and returns `""` on
   any error. `repo_map()` prepends it with a labelled header when non-empty.

3. **plan_context event.** `FrontierPlanner.plan()` emits `plan_context` with
   `repo_map_sha256` right after `repo_map_text = repo_map(repo)`, when
   `events is not None`. The pattern follows `plan_repair_started`.

4. **Event registration.** `plan_context` is added to `EVENT_TYPES` in
   `contracts.py`. It is added to the `planner_attempts` filter in `receipt.py`
   so the receipt shows it without schema changes.

5. **No double computation.** `repo_map()` is called once in
   `FrontierPlanner.plan()`; the sha256 is derived from that text. No separate
   call in `run.py`.

## Risks / Trade-offs

- Tests that monkeypatch `verdict.orchestration.planner.repo_map` with
  `lambda repo: "offline map"` still work: the lambda bypasses AGENTS.md
  extraction, returning a string without a section header. Existing assertions
  on prompt content are unaffected.
- The file cap (400) now fills with `verdict/` first. `tests/` and `docs/` files
  may be truncated. The planner needs code structure more than test listings
  for owned-file assignment.

## Security / trust implications

`AGENTS.md` is a checked-in file in the repository under operator control.
Its content is not untrusted input. It is read with `errors="replace"` and
capped at 4 096 bytes, so malformed UTF-8 and very large files cannot cause
prompt injection beyond that cap. No path traversal: `root / "AGENTS.md"` is
always a child of the repo root.

## Routing / context / memory implications

No model or network call is added. The repo map is built from `git ls-files`
(a local subprocess) and a local file read. Total added latency is negligible.

## Migration / rollback

The change is additive: `repo_map()` returns a larger string. Existing calls
with no AGENTS.md continue to work unchanged. Monkeypatched calls in tests keep
working. Rolling back removes the AGENTS.md prepend and restores alphabetical
sort; the planner regresses to SB1 behaviour but nothing fails.

## ADR impact

No new ADR. This implements the fix identified in SB1 findings (2026-11-23).
