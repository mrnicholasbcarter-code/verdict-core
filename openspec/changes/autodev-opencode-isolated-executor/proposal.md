# OC2a: OpenCode autodev rung, isolated executor behind an explicit flag

## Why

`verdict autodev` repairs code in the operator's own repository. Its model tier is
`PatchExecutor`: one gateway call that returns a diff, applied atomically after
`git apply --check`. It cannot run the unit's tests or linters before answering, so a
repair that fails verification is only found after the edit has landed.

OpenCode runs an edit, test and fix loop inside a tree before it returns. Giving autodev
that loop on free routes advances the mission (accepted work per paid dollar): the same
unit gets more chances to pass on a route that costs nothing. ADR-053 Decision 3 already
accepts OpenCode as the autodev coding rung.

Autodev runs in the user's repository, so the controls have to be at least as strong as
`PatchExecutor`'s. The cross-family review of PR #1299 found three autodev defects.
This change closes the first two and leaves the third to OC2b:

1. **Isolation.** OpenCode edited the user's repository directly. An out-of-bounds or
   failed attempt was not rolled back, and pre-existing dirty content was not protected.
2. **The context file counted as an edit.** `.verdict-context.md` in the user's tree was
   attributed to the unit by `_touched_since`, so `_verify` rejected a correct repair.
3. **Retries left OpenCode** (OC2b, #1314, not in this change).

The operator split the story on 2026-11-24. This change is **OC2a**: the isolated
executor, restore after a failed verification, and `run_autodev` wiring behind an explicit
flag, with retries disabled for it.

## What Changes

- New module `verdict/autodev_opencode.py` with `OpenCodeUnitExecutor`. Per attempt it
  seeds an unshared git clone from the user's current tree, runs OpenCode in the clone,
  computes what the worker changed from file contents (never by running git in the
  clone), checks every changed path against the unit's `owned_files`, and copies only
  validated owned-file changes back, with a journal so they can be undone.
- `verdict/autodev_run.py`: the executor parameter becomes a small protocol instead of the
  concrete `PatchExecutor`; `_run_unit_once` calls `restore()` when `_verify` fails after
  an applied attempt; `run_autodev` refuses retries when the executor is isolated.
- `verdict/cli.py`, `verdict/commands/parsers_routing.py`, `verdict/commands/dispatch.py`:
  a new opt-in flag `--worker-harness opencode` and a readiness check that runs before any
  probe, planner call or model call. When opencode or serve is unavailable autodev stops
  with one message. It never falls back to `PatchExecutor`.
- `docs/CLI_REFERENCE.md`: the flag, what is isolated and what is not.
- Tests: `tests/test_autodev_opencode.py`, `tests/test_autodev_opencode_wiring.py`.

## Not in this change

Each item belongs to OC2b (#1314) or another issue and has no task here.

- Retries on OpenCode on the newly admitted route (defect 3).
- Attempt-ledger and receipt lineage `harness=opencode`. Until OC2b lands, attempt rows
  for OpenCode units keep the label `verdict-autodev-executor` (design, "Known gaps").
- OpenCode as the default for model-tier units.
- Delivery of the worker credential to OpenCode (#1313 replaces it with a signed
  per-attempt assignment).
- Correcting ADR-053 Decision 3's wording (OC2b).
- `verdict autodev packet-execute` (operator: out of scope, a later story).
- A live autodev run on OpenCode (OC2b).

## Capabilities

### New Capabilities

- `autodev-opencode-executor`: an opt-in autodev executor that runs OpenCode in an
  unshared attempt clone and transfers back only validated owned-file changes.

### Modified Capabilities

None. The default autodev path (`PatchExecutor`, retry ladder) is unchanged.

## Impact

- Without the flag nothing changes: no readiness check, no clone, no OpenCode process.
- With the flag, `run_autodev` receives `retry_routes=()`; the probed alternates that the
  unpinned selection finds are reported but not used.
- `PatchExecutor` stays the model tier for every run without the flag. The mechanical tier
  (`_try_mechanical`, ruff) is untouched and still runs first in the user's tree.
- `openspec/changes/opencode-worker-adapter` and ADR-053 are not edited here. Main's copy of
  that change still lists the autodev rung (its tasks.md task 5.1 and its spec requirement
  "OpenCode added as autodev model-tier coding executor"). PR #1299 removes that claim [U];
  this change is the only owner of the autodev rung until OC2b.

## Dependencies

- **Build waits on #1299 (OC1), which is not merged.** This change reads its executor
  (`OpenCodeExecutor`) for facts only. The branch is owned by another agent and was not
  edited or checked out. Every fact taken from it is marked [U] and is re-read by task 1.1.
- **#1313 (OC1b)** will replace the worker credential with a signed per-attempt assignment
  and remove the `.verdict-context.md` file. This design depends on neither: the unit
  executor never touches the credential, and the context file is handled if present and
  never required.
- **#1314 (OC2b)** follows this change.

Source tags used in this change:

- **[main]**: read at main `c51cd994`.
- **[U]**: read on the unmerged OC1 branch at head `f684a7df`. Not on main. May change.
- **[exp]**: observed in a scratch repository with git 2.56.1 for this design.

## Evidence boundary

- **KNOWN [main]:** `run_autodev` has one production caller, `cmd_autodev`. Units run in
  sequence in the user's tree. `_run_unit_once` attributes changes by `_tree_state` before
  and after, `_verify` fails any touched path outside `owned_files` and runs the unit
  command in the user's repo. A failed `_verify` after an applied attempt does not retry
  and does not roll back. `PatchExecutor` returns `PatchAttempt` with outcome `applied`,
  `rejected` (tree untouched) or `error`. `GET /health` on serve needs no credential.
- **KNOWN [exp]:** the clone, hook, config and index behaviour listed in design.md,
  "Verified git behaviour".
- **KNOWN [U]:** `OpenCodeExecutor.run(prompt, *, route_id, cwd, timeout_seconds)` is async,
  never raises and returns `WorkerTerminal` with `harness="opencode"`. It reaps the worker's
  process group before it returns. It runs git in `cwd` only before launch, to check whether
  the context file is tracked. It writes `.verdict-context.md` into `cwd`.
- **INFERRED:** a route autodev admitted by probe can still be refused by serve's own
  admission. PR #1299's review reported serve failing closed for every route on a spare
  instance (#1306). Such a refusal fails the unit and leaves the repository untouched.
- **NOT AVAILABLE:** a live run of OpenCode on a unit through serve. It is OC2b's live
  check. The worker's own test loop inside a clone without ignored files such as `.venv` is
  unmeasured (design.md, "Risks").
- **Memory (Hindsight, 2026-11-24):** the OpenCode adapter page records the #1299 review
  that found direct repository edits without rollback as the most serious autodev issue,
  the split of autodev into #1301, and that PR #1299 head `f684a7df` was not ready to
  merge, with credential wiring still missing. It matches the issue and the research.

## Acceptance criteria

Mapped from #1301 as split. Each test must fail before the build and pass after.

- An out-of-bounds edit by a worker leaves the user's repository byte-identical, for a
  plain edit, a staged edit, a worker commit, an unowned new file and a symlink swap.
- A successful owned-file repair is accepted although the executor's context file was
  created, and the file never reaches the user's repository.
- A failed verification after transfer restores every owned file (for a verification command
  that writes nothing to the repository); a pre-existing dirty unowned file is untouched; a
  file the user changed after the transfer, its mode included, is not overwritten.
- A transfer never writes through a symlinked or swapped parent directory and never overwrites
  a file the user changed, its mode included, after the seed.
- With opencode or serve unavailable, autodev stops with one message before any probe or
  model call, and does not construct `PatchExecutor`.
- With retry routes supplied and the isolated executor, `run_autodev` refuses before any
  planner call.
- Gates: focused tests, ruff, mypy strict, reachability ratchet, lint gates, both OpenSpec
  validators. The full suite runs in CI on the exact head.

Left to OC2b: retries on OpenCode, lineage `harness=opencode`, the live run.

## GitHub issue

Tracked in [#1301](https://github.com/mrnicholasbcarter-code/verdict-core/issues/1301)
(OC2a). Follow-up: [#1314](https://github.com/mrnicholasbcarter-code/verdict-core/issues/1314)
(OC2b). Depends on [#1299](https://github.com/mrnicholasbcarter-code/verdict-core/pull/1299)
(OC1) and is shaped by [#1313](https://github.com/mrnicholasbcarter-code/verdict-core/issues/1313)
(OC1b).
