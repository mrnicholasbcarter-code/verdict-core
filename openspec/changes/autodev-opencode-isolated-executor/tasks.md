# Tasks

This is the build plan for OC2a (#1301). The build waits on #1299 (OC1) being merged.

Conventions for every task:

- Until #1306 merges, run every test and every command that writes state with
  `TESTENV='env HOME=/tmp/oc2a-home VERDICT_HOME=/tmp/oc2a-home/.verdict'`.
- Test first: show each new test failing before its code exists, and each guard's test
  failing with the guard removed, then passing. Put the output in the PR.
- Tests that call `run_autodev` pass `mechanical=False` and `store=ReceiptStore(":memory:")`,
  because the mechanical tier runs ruff in the user's tree and the default ledger writes
  `repo/.verdict/receipts.db`.
- Test names below are proposed. No test calls a live model, a probe or the real
  `opencode`; the OpenCode executor is a stub with `async run(...)` or a fake binary.
- Only tasks 1.1 and 5.1 import OC1 code. Tasks 2 to 4 depend on the stub contract only.

## 1. Preconditions

- [ ] 1.1 Confirm #1299 is merged to main. Re-read what this design took from it [U]: the
      `OpenCodeExecutor.run(prompt, *, route_id, cwd, timeout_seconds)` signature and that it
      never raises, the `harness`, `env=` and binary-resolver symbols, `PINNED_VERSION`, the
      context file and its byproduct handling, the serve base-URL setting and the credential
      source. Record every difference in design.md under a new "Drift after #1299" heading and
      adjust tasks 3 and 5 before coding. Also re-read #1313's state. Files:
      `openspec/changes/autodev-opencode-isolated-executor/design.md`. Verify:
      `git log origin/main --oneline -- verdict/orchestration/executor_opencode.py` lists the
      OC1 merge, and `$TESTENV uv run --frozen python -c "from verdict.orchestration.executor_opencode import OpenCodeExecutor, PINNED_VERSION"` exits 0.

## 2. The attempt tree (no OpenCode needed)

- [ ] 2.1 Seed the attempt tree (D2, D3). `seed_attempt_tree(repo, parent_dir, owned_files)` clones with
      `--no-local`, removes `origin`, overlays modified, deleted, staged and untracked files
      from `git status --porcelain=v1 -z --untracked-files=all` (symlinks copied as symlinks),
      takes manifest M0, and refuses with a named reason for no HEAD, a submodule, a nested
      repository, an unsafe path component or an exceeded limit. The tree is removable on every
      path. Files: `verdict/autodev_opencode.py`, `tests/test_autodev_opencode.py`. Verify:
      `$TESTENV uv run --frozen python -m pytest -q -n 2 tests/test_autodev_opencode.py -k seed`
      with tests for: no `objects/info/alternates` and no hardlinked object file; no `origin`
      remote; the user's hook, local config value and `info/exclude` entry absent from the
      clone; the overlay matches the user's dirty tree (modified, deleted, staged, untracked)
      and omits ignored files; an unmodified owned file whose checkout bytes differ from the working
      tree (an `eol` attribute) holds the user's bytes in the clone; the user's repository is byte-identical after seeding
      (definition in design.md); each refusal creates no worker and leaves no directory.
- [ ] 2.2 Manifest and delta (D4). `attempt_delta(tree, m0, repo, unit)` walks the tree with
      the root `.git` excluded, builds M1, applies the byproduct rule through
      `git check-ignore --no-index -z --stdin` run in the user's repository, then ownership
      through `WorkUnit.owns` on leaf entries (directories are not entries), the regular-file rule
      and the empty-delta rule. It never runs git in the tree, never follows a link in it and
      opens only regular files. The walk, M0, M1 and the delta-byte reads are all
      descriptor-relative (`scandir` on a directory descriptor, children opened with
      `O_DIRECTORY | O_NOFOLLOW` relative to their parent, files with `O_NOFOLLOW | O_NONBLOCK`
      and an `fstat` check). Files: `verdict/autodev_opencode.py`,
      `tests/test_autodev_opencode.py`. Verify: `$TESTENV uv run --frozen python -m pytest -q -n 2 tests/test_autodev_opencode.py -k delta`
      with tests for: a plain edit, a `git add`ed edit, a committed edit and a reverted edit
      (delta follows file contents only); an unowned new file; an owned delete; a mode change;
      a symlink in an owned path; a planted `core.fsmonitor` and a planted `pre-commit` hook in
      the tree's `.git` leave their marker file uncreated; a new owned file in a new directory is
      the only changed path (the directory is not); a directory swapped for an owned file's
      parent and a file swapped for a directory are rejected by ownership; a FIFO and a symlink
      substituted for a regular file are recorded by kind and never opened (the test would hang
      on a FIFO without `O_NONBLOCK`); a path that changes kind between the metadata read and the open is `error`; an ancestor
      directory swapped for a symlink to a path outside the tree, through a test seam between
      the directory listing and the open, reads nothing outside the tree and is `error`; a file over the per-file limit is a named refusal; a new path in a user-ignored
      directory is a byproduct; the same path existing at seed is not; a worker-written
      `.gitignore` hiding an unowned file is itself out of bounds and does not hide it; a tree
      over the limit is a named refusal.
- [ ] 2.3 Transfer with a journal (D5). `transfer(delta, user_repo, m0)` resolves every path
      from a root descriptor with `O_DIRECTORY | O_NOFOLLOW` per component, creates missing
      parents with `mkdir` by descriptor, compares each path's kind, executable bit and
      SHA-256 with M0 before any write and again immediately before its own write, writes a
      temporary file by descriptor and `rename`s it by descriptor, sets the executable bit from
      M1, records the journal (prior bytes, kind and mode, written fingerprint, created
      directories), and rolls back through restore on any write failure. It refuses to run
      where `os.supports_dir_fd` lacks what it needs. Files: `verdict/autodev_opencode.py`,
      `tests/test_autodev_opencode.py`. Verify:
      `$TESTENV uv run --frozen python -m pytest -q -n 2 tests/test_autodev_opencode.py -k transfer`
      with tests for: each refusal writes nothing and leaves the user's repository
      byte-identical (a symlinked parent, a non-regular target, a changed user file, a user file
      that exists when M0 has no entry, an oversized file, a path that escapes the root); a
      `chmod`-only change by the user after the seed is a refusal; an intervening user edit
      injected through a test seam between the preflight check and the first write is
      `rejected` and not overwritten; the same edit on the second of three files restores the
      first, leaves the edit and is `error` naming the second path (a multi-file late
      conflict); a late conflict where the user also changed an earlier written path leaves
      that path and lists it as not restored; a parent directory swapped for a symlink through
      the seam is refused and nothing is written through it; a parent directory renamed away
      with a symlink or another directory put at its old name is refused by the device and
      inode check and nothing is written into the displaced directory; an injected write
      failure on the second of three files restores all three; a hash mismatch between the
      delta read and M1 is `error`.
- [ ] 2.4 Restore (D6). `restore(journal)` undoes the last transfer in reverse order with the
      same descriptor-relative traversal and at-the-moment re-check as task 2.3, checking kind,
      executable bit and SHA-256 against what the transfer wrote; it leaves alone and reports
      any path that differs, removes directories it created when empty, and is idempotent.
      Files: `verdict/autodev_opencode.py`, `tests/test_autodev_opencode.py`. Verify:
      `$TESTENV uv run --frozen python -m pytest -q -n 2 tests/test_autodev_opencode.py -k restore`
      with tests for: a modified owned file, a created file and its created directory, a
      deleted file, a user edit made after the transfer (not overwritten, named), a user
      `chmod` made after the transfer (not undone, named), a parent swapped for a symlink
      between transfer and restore (nothing written through it), and a second call (no-op).

## 3. The executor

- [ ] 3.1 Prompt (see "Prompt" in design.md). `build_opencode_prompt(unit)` names the
      objective, owned files, verification command and context pack, and the rules (edit only
      owned files, run the command, no commit or push). Files: `verdict/autodev_opencode.py`,
      `tests/test_autodev_opencode.py`. Verify:
      `$TESTENV uv run --frozen python -m pytest -q -n 2 tests/test_autodev_opencode.py -k prompt`
      with a test that the context pack bytes appear unchanged and a test that a credential
      value placed in the environment does not appear.
- [ ] 3.2 `OpenCodeUnitExecutor.execute_unit` and the `PatchAttempt` mapping (D7). Seed, run the
      injected OpenCode executor with `route_id=config.model` and `cwd=<tree>`, take the delta,
      transfer, map to `applied`, `rejected` or `error` with the real reason, and remove the
      tree in a `finally`. `config.model` is the route, sampling fields are `None`,
      `response_headers` is `{}`, `restore()` delegates to task 2.4. Files:
      `verdict/autodev_opencode.py`, `tests/test_autodev_opencode.py`. Verify:
      `$TESTENV uv run --frozen python -m pytest -q -n 2 tests/test_autodev_opencode.py -k execute`
      with tests for: an owned edit is `applied` with usage `reported=False` when tokens are
      null; a not-ok terminal (timeout, stall, serve refusal text, non-zero exit) transfers
      nothing even though the tree holds edits; an exception and a cancelled run both remove
      the tree; a running event loop returns `error` and removes the tree; a stub terminal
      whose error text is already redacted (it contains `[REDACTED]`) while the credential
      value sits in the process environment gives an attempt reason that keeps the redacted
      text and never contains the credential value. Redaction stays the OpenCode executor's job
      [U] and is not reimplemented here.
- [ ] 3.3 Isolation acceptance matrix against a hostile stub worker. Each `rejected` variant
      checks the user's repository with the byte-identical definition (design.md) before and
      after: a plain out-of-bounds edit; a staged out-of-bounds edit; a worker `git commit`
      of an out-of-bounds change; an unowned new file; a symlink swapped into an owned path;
      a worker `git push origin` (the remote was removed, so it has no target); a planted
      `core.fsmonitor` and a planted hook in the tree's `.git`. Plus: an owned repair is
      accepted while the stub also writes `.verdict-context.md` into `cwd`, with the file
      absent from the user's repository, and the same repair is accepted when the stub writes
      no context file. Files: `tests/test_autodev_opencode.py`. Verify:
      `$TESTENV uv run --frozen python -m pytest -q -n 2 tests/test_autodev_opencode.py -k matrix`.
      One more test records the limit ADR-053 Decision 4 accepts: a stub worker that writes
      to the user's repository by absolute path is not stopped. It is
      `test_absolute_path_write_is_an_accepted_risk`, marked `xfail(strict=True)` on the
      assertion "the user's repository is unchanged", so the limit stays visible and a future
      change that closes it turns the test red until the mark is removed.

## 4. `run_autodev` wiring

- [ ] 4.1 Protocols, the gateway check and the restore hook (D6, D7). Add the `UnitExecutor`
      protocol and the two separate markers (`isolated` and a `restore` method); type the
      `executor` parameter and `_run_unit_once` with them; skip only the base-URL equality for
      an executor marked `isolated` and keep the model equality; call `restore()` in `_run_unit_once` after a failed `_verify`
      for an `applied` attempt and append the real restore result to the reason. Files:
      `verdict/autodev_run.py`, `tests/test_autodev_opencode_wiring.py`. Verify:
      `$TESTENV uv run --frozen python -m pytest -q -n 2 tests/test_autodev_opencode_wiring.py -k "restore or gateway"`
      and `$TESTENV uv run --frozen python -m pytest -q -n 2 tests/test_autodev_wiring.py`
      (existing `PatchExecutor` behaviour unchanged). New tests: a failed verification after
      a transfer leaves the repository byte-identical to before the attempt, with a
      pre-existing dirty unowned file untouched; a verified unit is not restored; a user edit
      after the transfer is not overwritten and the reason names it; a `PatchExecutor` run
      never calls `restore`; an executor that has `restore()` but is not marked `isolated` still
      fails the gateway equality check and is not subject to the retry refusal; the same
      failed verification where the verification command itself rewrites a transferred file
      leaves that file alone and names it, and leaves a file it created in place.
- [ ] 4.2 Refuse retries and handle null sampling (D7). With an executor marked `isolated` and non-empty
      `retry_routes` or a `retry_executor_factory`, raise `AutodevError` before the planner
      call. Make `_executor_lineage` and the attempt-record write accept `None` sampling values,
      omitting the keys if the record schema requires numbers. Files:
      `verdict/autodev_run.py`, `tests/test_autodev_opencode_wiring.py`. Verify:
      `$TESTENV uv run --frozen python -m pytest -q -n 2 tests/test_autodev_opencode_wiring.py -k "retry or lineage"`
      with tests that: retry routes and a factory each raise before any decomposer call; a
      failed unit runs once, is not retried, and `PatchExecutor` patched to raise is never
      constructed; the attempt record for an isolated unit is written under a temp
      `VERDICT_HOME` without error.

## 5. CLI

- [ ] 5.1 Readiness check and the credential seam (D8, D9). As functions in
      `verdict/autodev_opencode.py`, called from `cmd_autodev` by task 5.2 before
      `prepare_live_autodev` and `prepare_live_autodev_selected`, check the opencode binary
      (resolved and runnable) and serve (`GET /health` is 200 with `"engine": "verdict"`).
      Reuse OC1's binary resolver and version probe, and extract one shared helper if the
      doctor's probe is not callable. A failure prints one message and exits 1,
      reporting through `_report_autodev_failure` with a new label argument so the error is not
      labelled "decomposition failed" (its default label and existing callers are unchanged). A version different from the pinned one is named in the
      output and does not fail. One function builds the OpenCode executor for the CLI (the
      only place that knows the credential source). Files: `verdict/autodev_opencode.py`,
      `verdict/cli.py` (the label argument only), `tests/test_autodev_opencode_wiring.py`. Verify:
      `$TESTENV uv run --frozen python -m pytest -q -n 2 tests/test_autodev_opencode_wiring.py -k readiness`
      with tests that, for a missing binary, a binary that fails `--version`, serve refusing the
      connection, serve returning a non-Verdict body and a non-200: the exit status is 1, exactly
      one error line (and one JSON object with `--json`) is printed with a label that names the
      readiness check and not "decomposition failed", and `prepare_live_autodev`,
      `prepare_live_autodev_selected`, the decomposer, `run_autodev` and `PatchExecutor` are
      patched to raise and never called; and that a version mismatch continues with a note.
- [ ] 5.2 The flag. Add `--worker-harness` (choices: `opencode`; default none) to the autodev
      parser, pass it through dispatch to `cmd_autodev`, and with it build the isolated
      executor with the builder from task 5.1, pass `retry_routes=()`, and leave the selection
      report unchanged. Files: `verdict/commands/parsers_routing.py`, `verdict/commands/dispatch.py`, `verdict/cli.py`,
      `tests/test_autodev_opencode_wiring.py`. Verify:
      `$TESTENV uv run --frozen python -m pytest -q -n 2 tests/test_autodev_opencode_wiring.py -k cli`
      and `$TESTENV uv run --frozen python -m pytest -q -n 2 tests/test_autodev_wiring.py`
      with tests that: without the flag `PatchExecutor` is used, no readiness check runs and no
      clone is made; with the flag and `--dry-run` nothing runs; with the flag `run_autodev`
      receives the isolated executor and no retry routes; the help text says retries and
      lineage are not yet supported.

## 6. Docs

- [ ] 6.1 Document the flag in `docs/CLI_REFERENCE.md`: what it does, that it is opt-in and
      experimental, the one-message fail-closed behaviour, what is isolated (design.md,
      "What byte-identical means"), what is not (an absolute-path write, the mechanical tier,
      ignored files), that retries and lineage are not yet supported, and where the attempt tree
      lives. Describe only what runs. Files: `docs/CLI_REFERENCE.md`. Verify:
      `uv run --frozen python scripts/check_doc_links.py`.

## 7. Gates and review

- [ ] 7.1 Run ruff check and format, mypy strict, the lint gates and the reachability ratchet,
      with every new symbol reachable from `cmd_autodev`. Check the diff against
      `.claude/pipeline-lessons.md`. Get the cross-family review of the local commit and fix
      every valid finding. Files: none. Verify:
      `uv run --frozen ruff check . && uv run --frozen ruff format --check .`,
      `uv run --frozen mypy verdict --strict`,
      `uv run --frozen python scripts/check_reachability.py --ratchet-base origin/main`,
      `uv run --frozen python scripts/check_lint_gates.py` and `git diff --check` all exit 0.
- [ ] 7.2 Validate this change with both validators and tick the tasks that have evidence.
      Files: `openspec/changes/autodev-opencode-isolated-executor/`. Verify:
      `uv run --frozen python verdict/openspec_vendor/validate_openspec_change.py openspec/changes/autodev-opencode-isolated-executor`
      and `npx -y @fission-ai/openspec@1.13.2 validate autodev-opencode-isolated-executor --strict`.

## 8. After merge

- [ ] (post-merge) 8.1 On installed main, run `VERDICT_OPENCODE_BIN=/nonexistent verdict autodev
      --allow-live --worker-harness opencode --objective "x" --repo <scratch repository>`.
      Verify: it exits 1 with the single readiness message naming the binary, before any probe
      or planner call, and `git -C <scratch repository> status --porcelain` is empty. The live
      unit run on OpenCode through serve is OC2b's check (#1314), not this one.
