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
- the receipt records the SHA-256 of the repo map text the planner received
  (the map itself is not stored);
- no network or model call is added to the planning path.

Non-goals:
- code-graph file facts (MCP/model calls at planning time — deferred);
- changing the planning prompt's overall structure or the JSON contract;
- removing `--constraints-file` (improvement.py still uses it for lessons).

## Decisions

1. **Include-order sorting.** Files are grouped by prefix in include-tuple order
   then sorted within each group, then capped. `tracked.extend(sorted(by_prefix[p]))`
   for each `p in include` before the `[:max_files]` slice.

2. **AGENTS.md section.** `_agents_md_where_things_live(root)` reads
   `root/AGENTS.md`, extracts the "## Where things live" block up to the next
   same-level heading or end-of-file, caps at 4 096 bytes of UTF-8 (cut on a
   character boundary), and returns `""` when the file cannot be read (`OSError`)
   or has no such section. `repo_map()` prepends it with a labelled header when
   non-empty. The file is read with `Path.read_text()`, which follows symlinks
   (see Security / trust implications).

3. **plan_context event.** `FrontierPlanner.plan()` emits `plan_context` with
   `repo_map_sha256` right after `repo_map_text = repo_map(repo)`, when
   `events is not None`. The pattern follows `plan_repair_started`. This is one
   event per `plan()` call, before its first model call. It is not one per model
   call: the validation-repair call inside the same `plan()` call re-sends the
   same prompt (`build_repair_prompt` is the planning prompt plus the validation
   error), so it carries the same repo map and emits no `plan_context` of its own.
   The map is not rebuilt for the repair call, so there is no new digest to
   record. The next planning attempt in `run.py` is a new `plan()` call: it
   rebuilds the map and records its own digest.

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

Trust assumption: the repository passed to `orchestrate` is the operator's own
checkout, so its `AGENTS.md` is as trusted as the rest of the tree the planner
and workers read. The code does not enforce that assumption:

- **The byte cap is a size bound, not injection protection.** The 4 096-byte cap
  limits how much text the section adds to the prompt. A few kilobytes are
  enough to carry instructions, and the section reaches the planning model
  verbatim inside the `REPO MAP:` block. Reading with `errors="replace"` only
  stops malformed UTF-8 from raising; it does not neutralise the text.
- **A symlinked `AGENTS.md` is followed.** `root / "AGENTS.md"` is a child of the
  repo root by name only. `Path.read_text()` follows symlinks, and the code does
  not resolve the path or check that it stays inside the repository. If
  `AGENTS.md` is a symlink to a file outside the repository, that file is read.
  Only its `## Where things live` block is kept (nothing is added when it has no
  such heading), and that block is sent to the planning route, which may be an
  external provider. Checked: a symlink to an outside file with that heading
  yields the outside file's section.
- **What does limit the effect.** The planner's answer is only proposed work:
  `parse_plan` validates it into a `WorkGraph`, and the runtime still applies
  per-node admission and verification. That limits what an injected instruction
  can change. It does not stop one reaching the model.

Refusing a symlinked `AGENTS.md`, or requiring its resolved path to stay inside
the repository, would close the second point. It is not part of this change.

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
