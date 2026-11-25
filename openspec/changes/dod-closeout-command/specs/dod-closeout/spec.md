# Spec Delta: dod-closeout

Terms. The **closeout home** is `$VERDICT_HOME/closeout` (`verdict.paths.verdict_home()`); a story's
directory in it is `<home>/<story>/`. **Installed main** is the git checkout the installed services run
from. A **cycle** is one pass over a story's steps: each acceptance item in declaration order, then
`verdict doctor --json` (timeout 180 s), and `on_main` (`gh pr checks <N> --required`, timeout 60 s) in every
cycle until one run of it has passed. A cycle **passes** when every item and `doctor` pass in it and
`on_main` has a passing run, in it or in an earlier cycle, under the current fingerprint. The **canonical command** of an argument
list `argv` is `verdict ` followed by `shlex.join(argv)`; it never holds an interpreter path or a host
path. The **fingerprint** of a declaration is the SHA-256 of the canonical JSON of its evidence-defining
fields: `pr`, `freshness_window_hours` and, per item in order, `criterion`, the canonical command, `expect`,
`expect_exit_code` and `real_state_marker`. Every scenario below has one test, named `test_` plus the
scenario title in snake case.

## ADDED Requirements

### Requirement: Closeout SHALL learn stories only from declarations in installed main

`verdict closeout` SHALL take its list of stories from `proof/closeout/<story>.yaml` files in the working
tree of installed main, validated against `proof/closeout.schema.json`. It SHALL NOT read the DoD tracker,
an issue or a pull request body to learn a story, a command or an expectation. A declaration holds the
story name, `kind: product`, `author` (an agent identity, not a GitHub login), the pull request number,
`freshness_window_hours` (at least 1), `stays_true_job`, the OpenSpec change or a not-applicable reason,
`live: false`, and one or more acceptance items: `criterion`, `argv` (a list of strings), `expect`,
`expect_exit_code`, `real_state_marker` and `timeout_seconds` (at most 600). Any other key SHALL be
refused. A refused declaration SHALL stop only its own story, and its reason SHALL name the field.

#### Scenario: Valid declaration is accepted
- **WHEN** a declaration holds every field with a window of 1 hour and an allowlisted `argv`
- **THEN** the story is loaded and listed by `verdict closeout status`

#### Scenario: Window below one hour is refused
- **WHEN** `freshness_window_hours` is 0.25 or 0.99
- **THEN** the story is refused naming `freshness_window_hours` and no command of it runs

#### Scenario: Unknown key is refused
- **WHEN** an item or the story carries `shell`, `env`, `cwd`, `binary` or any key the schema does not name
- **THEN** the story is refused naming that key

#### Scenario: Argv given as a string is refused
- **WHEN** `argv` is a string, an empty list, or a list holding a non-string
- **THEN** the story is refused naming `argv` and nothing is split or run

#### Scenario: Story name must be a plain name equal to the file name
- **WHEN** `story` differs from the file stem, or holds `/`, `..` or an upper-case letter
- **THEN** the story is refused naming `story`

#### Scenario: Live declaration is refused
- **WHEN** `live` is `true`
- **THEN** the story is refused naming `live` (live runs are not part of this change) and nothing runs

#### Scenario: Host path or secret in the expectation is refused
- **WHEN** `expect` or `real_state_marker` holds `/home/<name>` or text that `redact_text` would change
- **THEN** the story is refused naming that field

#### Scenario: One bad declaration stops only its own story
- **WHEN** two declarations are present and one is refused
- **THEN** the other story is processed in the same invocation and the exit status reports the refusal

### Requirement: Closeout SHALL identify installed main before it runs anything

Before any story command, closeout SHALL establish three facts: the root it runs from equals the
`WorkingDirectory` of the installed `verdict-acceptance.service` unit file (both resolved); `HEAD` of that
root is a full 40-hex SHA; and the root has no tracked or untracked change (ignored files excluded). It
SHALL read git state with `--no-optional-locks`, so it never rewrites the index. When any fact fails it
SHALL spawn no further process, write no story record, print the named reason and exit with status 2.
The record of a step that runs in installed main (an acceptance item or `doctor`) SHALL carry this `HEAD`
as its `sha`, never the story's merge commit. The `on_main` record is the answer of `gh pr checks` about the
merge commit, not a run of installed main: its `sha` is the merge commit, which `scripts/check_dod.py`
requires (`on_main.sha == merged_sha`).

#### Scenario: Installed SHA is recorded for runs and the merge SHA for on_main
- **WHEN** a story merged at commit M is run while installed main is at a later commit S
- **THEN** the acceptance and `doctor` records and the draft's run `sha` values are S, and `on_main` and `merged_sha` are M

#### Scenario: Development checkout is refused
- **WHEN** closeout runs from a root that is not the unit file's `WorkingDirectory`
- **THEN** it exits 2 naming `not_installed_main`, spawns no process and writes no record

#### Scenario: Unit file missing or without a working directory is refused
- **WHEN** the unit file is absent, unreadable or has no `WorkingDirectory=` line
- **THEN** it exits 2 naming `installed_main_unknown`, and unknown is never treated as installed main

#### Scenario: Dirty installed main is refused
- **WHEN** the root has a modified tracked file or an untracked file
- **THEN** it exits 2 naming `installed_main_dirty` and spawns no story command

#### Scenario: Unrecorded build is refused
- **WHEN** `HEAD` cannot be read or is not a 40-hex SHA
- **THEN** it exits 2 naming `installed_sha_unrecorded`

#### Scenario: Identity checks leave the installed index untouched
- **WHEN** a tracked file has a stale stat time that a plain `git status` would refresh in the index
- **THEN** the index file's bytes are identical after the identity checks

### Requirement: The merge commit SHALL come from the declared PR and be an ancestor of installed main

For each story closeout SHALL ask `gh pr view <N> --json state,mergeCommit` (timeout 30 s) and SHALL cache
only a successful answer: `state` is `MERGED` and `mergeCommit.oid` is a 40-hex string, appended as a
`merge` entry holding `pr` and `oid`. A cached entry applies only while its `pr` equals the declaration's.
Without an applicable entry every invocation asks again. Before each cycle closeout SHALL check, from the
cached `oid`, that the commit exists in installed main and that `git merge-base --is-ancestor <merge>
<installed HEAD>` succeeds. Anything else (`OPEN`, `CLOSED`, a missing key, null, a wrong type, a command
failure or a timeout) SHALL skip the story before any acceptance command spawns, with the reason written to
the ledger as a skip, not as a run; it is never cached and never counts as a failed cycle.

#### Scenario: Merged ancestor proceeds
- **WHEN** the PR is merged and its merge commit is an ancestor of installed main
- **THEN** the story proceeds and its ledger holds the merge commit

#### Scenario: Unmerged pull request waits
- **WHEN** `state` is `OPEN` or `CLOSED`
- **THEN** the story is skipped with reason `pr_not_merged`, nothing runs and no `merge` entry is written

#### Scenario: Merge commit absent from installed main waits
- **WHEN** the merge commit is not an object in installed main (installed main is behind)
- **THEN** the story is skipped with reason `merge_commit_not_installed`, no command runs and no failed-cycle is counted

#### Scenario: Merge commit that is not an ancestor is refused
- **WHEN** the commit exists in installed main but is not an ancestor of its `HEAD`
- **THEN** the story is skipped with reason `merge_commit_not_ancestor` and nothing runs

#### Scenario: Merged lookup is asked once per story
- **WHEN** a story runs a first and a fresh cycle
- **THEN** `gh pr view` is spawned once and the second cycle reads the merge commit from the ledger

#### Scenario: Pull request that merges later progresses
- **WHEN** an invocation finds the pull request `OPEN`, `CLOSED`, or its lookup failing, timing out or malformed, and a later invocation finds it `MERGED`
- **THEN** each of the first invocations spawned `gh pr view` and wrote no `merge` entry, and the later one asks again, caches the commit and runs the first cycle

#### Scenario: Changed pull request refreshes the merge lookup
- **WHEN** a story with a cached merge commit for pull request 10 and a passing first cycle is redeclared with pull request 11
- **THEN** `status` (which spawns nothing) shows `waiting_install`, the next invocation asks `gh pr view 11`, the entry for 11 is the applicable one, and the commit and cycles of 10 are used by no cycle and no draft

#### Scenario: Malformed lookup result is refused
- **WHEN** the `gh` output is not JSON, lacks `state` or `mergeCommit`, has `mergeCommit: null`, a wrong type or a non-hex `oid`, or the call times out
- **THEN** the story is skipped naming the broken field, nothing is cached, and no acceptance command runs

### Requirement: Only allowlisted commands SHALL run, as an argument list, from installed main

A declared `argv` SHALL run only when it matches an entry of the allowlist, a table in code. An entry
names the subcommand words, the flags that must be present and the flags that may be present; every token
of `argv` SHALL equal one of those words or flags exactly. Abbreviated flags, `--flag=value` forms, value
flags and positional arguments SHALL be refused. Closeout SHALL execute
`[<installed root>/.venv/bin/python, "-m", "verdict", *argv]` with `shell=False`, `cwd` the installed
root, `stdin` closed and its own process group; the declaration cannot name a binary, a working
directory or an environment variable. The interpreter path SHALL be passed as written, never resolved:
the virtualenv is found from `pyvenv.cfg` beside that path, and its symlink target (a Python outside the
checkout is the normal layout) may lie anywhere. Before a story's first spawn closeout SHALL check that
`<root>/.venv` resolves inside the root and holds a regular `pyvenv.cfg`, that `.venv/bin/python` exists
and is executable, and that `<root>/verdict/__main__.py` is a regular file inside the root: with `-m` and
the root as `cwd` the root comes first on `sys.path`, so the `verdict` package that runs is installed
main's. The child's environment SHALL be closeout's own with `VERDICT_LIVE_SMOKE`, `VERDICT_PREPUSH_SKIP`,
`PYTHONHOME`, `PYTHONPATH` and `PYTHONSAFEPATH` removed (the last would keep the root off `sys.path`) and
`PYTHONDONTWRITEBYTECODE=1` set. Closeout SHALL spawn only these processes: read-only `git` commands
in the installed root, `gh pr view <N> --json state,mergeCommit`, `gh pr checks <N> --required`, an
allowlisted `verdict` command, and `scripts/check_dod.py --draft` of installed main. Every spawn SHALL go
through one function.

#### Scenario: Allowlisted command runs from installed main
- **WHEN** `argv` is `["doctor", "--json"]`
- **THEN** the spawn receives a list equal to the interpreter, `-m`, `verdict`, `doctor`, `--json`, with `shell` false, the installed root as `cwd`, closed stdin and a new session

#### Scenario: Unlisted subcommand is refused
- **WHEN** `argv` starts with a subcommand that has no allowlist entry, for example `route` or `run-receipt`
- **THEN** the story is refused naming the subcommand and nothing spawns

#### Scenario: Unlisted or abbreviated flag is refused
- **WHEN** `argv` holds `--fix`, `--no-ref` (an abbreviation of `--no-refresh`), `--json=true` or a value flag
- **THEN** the story is refused naming the token

#### Scenario: Missing required flag is refused
- **WHEN** `argv` is `["eligibility"]` without `--no-refresh`
- **THEN** the story is refused naming `--no-refresh`

#### Scenario: Live and mutating commands are refused
- **WHEN** `argv` names `orchestrate`, `autodev --allow-live`, `bench`, `setup`, `serve`, `acceptance run`, `doctor --fix` or `memory docs --fix`
- **THEN** each is refused by name, and a declaration cannot add an allowlist entry

#### Scenario: Symlinked virtualenv interpreter runs from installed main
- **WHEN** `.venv/bin/python` is a symlink to a Python outside the root, `.venv/pyvenv.cfg` exists, and closeout's environment holds `PYTHONSAFEPATH=1` and a `PYTHONPATH` with a decoy `verdict` package that has its own `__main__`
- **THEN** a real child started through the spawn function reports `sys.prefix` equal to the virtualenv and `verdict.__file__` under the root, and the spawn's first argument is the unresolved `.venv/bin/python` path

#### Scenario: Unusable virtualenv is refused
- **WHEN** `.venv/bin/python` is missing, a dangling symlink or not executable, `.venv/pyvenv.cfg` is missing, or `.venv` is a symlink resolving outside the root
- **THEN** the story is skipped naming `venv_unusable` and nothing spawns

#### Scenario: Package outside installed main is refused
- **WHEN** `<root>/verdict/__main__.py` is missing or a symlink resolving outside the root
- **THEN** the story is skipped naming `package_not_in_installed_main` and nothing spawns

#### Scenario: Child environment carries no consent switch or Python path override
- **WHEN** closeout's environment holds `VERDICT_LIVE_SMOKE=1`, `VERDICT_PREPUSH_SKIP=1`, `PYTHONHOME`, `PYTHONPATH` and `PYTHONSAFEPATH`
- **THEN** the child's environment holds none of them and holds `PYTHONDONTWRITEBYTECODE=1`

#### Scenario: Each allowlist entry leaves a temporary home unchanged
- **WHEN** every allowlist entry is run as a real child process with `HOME` and `VERDICT_HOME` in a temporary directory
- **THEN** a snapshot of that directory before and after differs in no file

#### Scenario: Only the five process kinds are spawned
- **WHEN** a full first and fresh cycle runs with every spawn recorded
- **THEN** each recorded argument list is one of the five kinds, none is a GitHub write (`gh issue`, `gh pr comment`, `gh pr create`, `gh label`, `gh api`), and none is `git` with a write verb

### Requirement: Each run SHALL be recorded from what executed

For every step closeout SHALL record, after the process ends, the start time (UTC, with offset), the `sha`
(installed `HEAD`; the merge commit for `on_main`), the fingerprint, the canonical command, the exit code and
the stored output's path and SHA-256. A cycle SHALL run its steps in the order of Terms (items, `doctor`,
then `on_main` while it has not passed) and every step runs even when an earlier one failed. A step
whose exit code differs from `expect_exit_code`, whose scrubbed output lacks `expect` or
`real_state_marker`, or that times out, SHALL be recorded as failed with the reason of the check that
rejected it, and its output SHALL be kept. A timeout SHALL kill the whole process group. A spawn that
raises SHALL leave no run record: the step is skipped with reason `spawn_error`, and no evidence is ever
built from it.

#### Scenario: First run records the installed SHA, time, exit code and output digest
- **WHEN** an allowlisted fixture command exits 0 with the expected text
- **THEN** the ledger holds one `first` entry with `sha`, an offset-bearing `at`, exit code 0 and an output file whose SHA-256 matches

#### Scenario: Failing command keeps its output and yields no draft
- **WHEN** the command exits 1 or prints without `expect`
- **THEN** the entry is failed with the matching reason (`exit_code_mismatch`, `expect_missing` or `marker_missing`), its output file exists, and no draft is written

#### Scenario: Red doctor fails the cycle
- **WHEN** every item passes and `verdict doctor --json` exits 1
- **THEN** the cycle is failed with reason `doctor_not_green`, its output is kept and no draft follows

#### Scenario: Unusable on_main fails the cycle
- **WHEN** `gh pr checks <N> --required` exits non-zero (pending or failing), or exits 0 with no Actions run URL in its output
- **THEN** the cycle is failed with reason `on_main_not_green` or `on_main_marker_missing`, and later cycles repeat `on_main` until it passes

#### Scenario: Cycle runs its steps in order and repeats on_main until it passes
- **WHEN** a story with two items has a cycle whose `on_main` fails, a next cycle whose `on_main` passes, and then a fresh cycle
- **THEN** the recorded spawns of the cycles are item 1, item 2, `doctor`, `on_main`; item 1, item 2, `doctor`, `on_main`; and item 1, item 2, `doctor` with no `on_main`

#### Scenario: Timeout kills the process group
- **WHEN** a command (fake interpreter script) spawns a child and sleeps past `timeout_seconds`
- **THEN** no process of the group survives, the entry is failed with reason `timeout`, and the cycle ends within the limit plus a bounded grace

#### Scenario: Spawn failure records no run
- **WHEN** the spawn function raises `OSError`
- **THEN** the ledger holds a skip with reason `spawn_error`, no output file, and no later draft cites it

### Requirement: Stored output SHALL hold no secret and no host path

Closeout SHALL apply `verdict.security.redact_text`, then replace every occurrence of the current `$HOME`
path with `~` and every remaining `/home/<name>` with `/operator`, and SHALL store, hash and judge only
that result. `expect` and `real_state_marker` SHALL be looked up in the stored text. The stored text SHALL
satisfy `redact_text(text) == text`. Closeout SHALL drain the child's stdout and stderr concurrently while
it runs, so neither stream blocks the other, and SHALL retain at most 1 MiB of both together. A child that
passes that cap SHALL have its process group killed and its step failed with reason `output_too_large`;
what was retained is scrubbed and kept like any failed step's output. The unscrubbed output SHALL NOT be
written, logged or hashed.

#### Scenario: Echoed secret is not stored
- **WHEN** the command prints a bearer token and a `sk-` style key
- **THEN** neither string appears in the output file, the ledger, the report or any log line

#### Scenario: Home path is rewritten
- **WHEN** the command prints `$HOME/.verdict/x` and `/home/someone/y`
- **THEN** the stored text holds `~/.verdict/x` and `/operator/y`

#### Scenario: Stored output is stable under redaction
- **WHEN** any scrubbed output is read back
- **THEN** `redact_text(text) == text`

#### Scenario: Expected text is judged after scrubbing
- **WHEN** `expect` appears only inside a string that scrubbing rewrites
- **THEN** the step fails with `expect_missing`, not passes on the unscrubbed text

#### Scenario: Large output on both streams does not block the child
- **WHEN** a real child writes 200 KiB to stderr and then 200 KiB to stdout (each past a pipe buffer, together under the cap) and exits 0
- **THEN** the step ends long before its timeout with both streams in the stored text and no `timeout`

#### Scenario: Oversized output fails the step
- **WHEN** a real child prints without end on stdout, on stderr, or on stderr first and then on stdout, under a 10 s timeout
- **THEN** the step ends within a few seconds failed with `output_too_large` and not `timeout`, no process of the group survives, and the stored output is at most 1 MiB and equal to its own redaction

### Requirement: The ledger SHALL be append-only and verified on every read

Each story keeps `<home>/<story>/ledger.jsonl`. Each entry carries `seq`, the previous entry's digest and
its own digest over its canonical JSON. An output file is written and flushed before the entry that cites
it is appended; the append is the commit point. Every read SHALL verify the chain and every cited
output's SHA-256. A story whose chain or output fails SHALL run nothing, report `ledger_corrupt` naming
the first bad entry, and leave other stories unaffected. Removing trailing entries cannot be detected;
its only effect is a repeated run, never a recorded run that did not happen.

#### Scenario: Edited exit code is refused
- **WHEN** an entry's `exit_code` is changed on disk
- **THEN** the story reports `ledger_corrupt` at that `seq` and no draft or run follows

#### Scenario: Edited output file is refused
- **WHEN** a cited output file is changed by one byte
- **THEN** the story reports `ledger_corrupt` and no draft is built from it

#### Scenario: Removed or reordered middle entry is refused
- **WHEN** an entry before the last is deleted or two entries are swapped
- **THEN** the chain check fails naming the first bad `seq`

#### Scenario: Orphan output without an entry is ignored
- **WHEN** the entry append fails after the output file was written
- **THEN** no run is counted for that step, the file is not cited by any draft, and the next invocation runs the step again

#### Scenario: Corrupt ledger stops only its story
- **WHEN** one of two stories has a corrupt ledger
- **THEN** the other story is processed and the exit status is 1

### Requirement: The freshness window SHALL start at the passing first run and never be waived

The first cycle that passes starts the window with its `at` times; the fresh cycle may start once the
clock is at least the declared hours after the latest first-run `at`, and SHALL be a new execution of the
same canonical commands. Before then, an invocation SHALL spawn nothing for the story beyond the identity
checks. A failed cycle SHALL be retried by a later invocation, at most three failed cycles in all; the
fourth SHALL NOT run and the story is `needs_person` until `verdict closeout reset <story>` appends a reset
entry (history is kept). Every step entry SHALL carry the fingerprint of the declaration it ran under. A
change to any fingerprinted field, a command included, SHALL void the cycles recorded under another
fingerprint: they stay in the ledger and the report, but count as no first run, no fresh run and no failed
cycle, and no draft cites them. A change outside the fingerprint (`timeout_seconds`, `author`,
`stays_true_job`, `openspec`) voids nothing. No option, environment variable or declaration value SHALL
set the clock or shorten the window.

#### Scenario: Run before the window spawns nothing
- **WHEN** a story has a passing first cycle and the clock is 59 minutes after it with a 1 hour window
- **THEN** the invocation spawns nothing for that story after the identity checks and `status` shows `waiting_window` with the time it ends

#### Scenario: Run after the window records a fresh run
- **WHEN** the clock is past the window
- **THEN** every step runs again, the entries are `fresh`, and each `at` is at least the window after the same step's first `at`

#### Scenario: Window counts from the passing first run
- **WHEN** a first cycle failed and a later one passed
- **THEN** the window ends the declared hours after the passing cycle, not after the failed one

#### Scenario: Changed declaration voids the first run
- **WHEN** after a passing first cycle one of these changes: `freshness_window_hours` (longer or shorter), or an item's `criterion`, `argv`, `expect`, `expect_exit_code` or `real_state_marker`
- **THEN** that first run is void, `status` shows `needs_first` with reason `declaration_changed`, no draft uses it, and the next invocation runs a new first cycle

#### Scenario: Declaration fields outside the fingerprint do not void the first run
- **WHEN** after a passing first cycle only `timeout_seconds`, `author`, `stays_true_job` or `openspec` changes
- **THEN** the first run stays valid and `status` is unchanged

#### Scenario: Changed declaration restarts the failed cycle count
- **WHEN** two cycles failed under one fingerprint and the declaration then changes
- **THEN** the next invocation runs a cycle, the earlier failures stay in the ledger and the report, and a story already `needs_person` runs without a reset

#### Scenario: Failed cycle is retried on the next invocation
- **WHEN** a cycle fails and the next invocation comes
- **THEN** the cycle runs again and the failed one stays in the ledger

#### Scenario: Third failed cycle leaves the story for a person
- **WHEN** three cycles have failed
- **THEN** the next invocation spawns nothing for the story after the identity checks and `status` shows `needs_person`

#### Scenario: Reset resumes a story and keeps its history
- **WHEN** `verdict closeout reset <story>` runs on a `needs_person` story
- **THEN** the next invocation runs the cycle and the earlier failed entries are still in the ledger and the report

#### Scenario: No option changes the clock or the window
- **WHEN** `verdict closeout run --help` and the parser are inspected, and a window of 0.5 is declared
- **THEN** there is no time, window or force option, and the declaration is refused

### Requirement: A draft SHALL be built only from verified recorded runs and kept outside the repository

When a story has a passing first cycle and a passing fresh cycle, closeout SHALL write a draft under
`<home>/<story>/draft/docs/proof/dod/`: `<story>.json` and every cited output copied byte for byte. It
SHALL fill `merged_sha` and `pr` from the applicable `merge` entry, `on_main` from the recorded `gh pr
checks` run, each acceptance run from its ledger entries, `setup_doctor` from the fresh cycle's `doctor`
run, and the declared fields from the declaration, all from step entries whose fingerprint equals the
declaration's and the `merge` entry whose `pr` does. It SHALL NOT write `parent_confirmation`. It SHALL run
`scripts/check_dod.py --draft --root <installed root> --evidence-root <draft dir>` on installed main and
SHALL offer the draft only when that exits 0; a rejected candidate goes to `<home>/<story>/rejected/`
with the checker's output. It SHALL write `report.md` beside the draft listing every run, failed and
voided runs included, by step, time, exit code and output path. It SHALL NOT write under `docs/proof/dod/` of any
repository, mark a story done, or write a confirmation.

#### Scenario: Complete story yields a draft that passes the draft check
- **WHEN** a fixture story has a passing first and fresh cycle, with merge commit M earlier than installed `HEAD` S
- **THEN** the draft exists with `on_main.sha` M and run `sha` values S, `check_dod.py --draft` exits 0 on it, and the ledger gains a `draft` entry with the file's digest

#### Scenario: Rejected candidate is moved aside and not offered
- **WHEN** `check_dod.py --draft` exits 1 on a candidate
- **THEN** the candidate and the checker's output are under `rejected/`, no `draft/` directory and no `draft` entry exist, and `status` shows the story as `draft_invalid`

#### Scenario: Draft carries no confirmation
- **WHEN** a draft is written
- **THEN** its JSON has no `parent_confirmation` key, and `report.md` says confirmation is pending

#### Scenario: Strict check fails on the draft for confirmation and archive only
- **WHEN** the plain `check_dod.py` runs on the draft with a stub confirmation added and the change not archived
- **THEN** the errors are exactly the missing archive and nothing about hashes, ancestry, window or redaction; with no stub the only schema error is `parent_confirmation`

#### Scenario: Too-early fresh run fails the draft check
- **WHEN** a hand-built draft has a fresh run less than the window after the first
- **THEN** `check_dod.py --draft` reports `not after the freshness window` and exits 1

#### Scenario: No draft for a run that did not happen
- **WHEN** a story has only a first cycle, or its ledger is corrupt, or a step was a `spawn_error` skip
- **THEN** no draft is written and the candidate directory holds nothing

#### Scenario: Failed runs are listed in the report and not in the evidence
- **WHEN** a cycle failed before the passing one, and an earlier cycle was voided by a changed declaration
- **THEN** `report.md` lists both with their output paths, and the draft JSON cites only passing runs of the current fingerprint

### Requirement: Closeout SHALL write only under its home and never change anything else

Every file closeout creates or changes SHALL be under `<closeout home>/` and SHALL be written through one
helper that refuses an absolute path, `..` or a symlink that leaves the home. Closeout SHALL NOT write in
installed main, in any other repository, in `~/.verdict` outside the closeout home, or in GitHub, and it
SHALL NOT archive an OpenSpec change, tick a task, mark a story done, install or restart a service, or
reinstall installed main. Two invocations SHALL NOT run at once: a second finds the lock held, changes
nothing and exits 3. The exit status of `run` SHALL be 0 when every story progressed or waits (pull request
not merged, merge commit not installed, window open), and 1 when any story failed, was refused, needs a
person or was skipped for another reason. `verdict closeout status` SHALL spawn nothing and write nothing.

#### Scenario: A full cycle writes only under the closeout home
- **WHEN** a story runs first and fresh cycles against a temporary home and a fake installed main
- **THEN** a before and after snapshot of the temporary home differs only under `.verdict/closeout/`, the fake installed main has no changed, added or removed file and an identical index, and no file under the closeout home holds the home path or `/home/`

#### Scenario: Escaping path is refused
- **WHEN** the write helper is given an absolute path, `..` or a story directory that is a symlink out of the home
- **THEN** it raises a named error and nothing is written outside

#### Scenario: Waiting exits zero and any other skip exits one
- **WHEN** one story is in each of these conditions: `pr_not_merged`, `merge_commit_not_installed`, an open window, a lookup failure, `venv_unusable`, `package_not_in_installed_main` and `spawn_error`
- **THEN** the exit status is 0 for the first three and 1 for each of the others

#### Scenario: Status shows an empty ledger as not started
- **WHEN** a declared story has no ledger entry
- **THEN** `verdict closeout status` shows it as `not_started`, with no process spawned

#### Scenario: Status spawns nothing and writes nothing
- **WHEN** `verdict closeout status --json` runs with spawns recorded and a snapshot taken
- **THEN** no process is spawned and no file changes

#### Scenario: Second invocation exits busy
- **WHEN** one invocation holds the lock and another starts
- **THEN** the second exits 3 and changes nothing

#### Scenario: Real home is refused by the test guard
- **WHEN** the home is deliberately pointed at the guard's stand-in for the real home
- **THEN** the write fails by name under the shared home guard instead of writing
