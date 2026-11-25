# Design

Tags: **[main]** read at `fe2c5e7f`. **[host]** observed on this host, 2026-11-25 (read-only). **[U]** read on an
unmerged branch, re-read by task 1.1. **[P]** this design's proposal, not an operator decision or a fact.
Behaviour is in the spec deltas; names, seams and test lists are in tasks.md. This file is the why.

## Context

Issue #1308 (size L) asks for a scheduled job that does the post-merge half of the Definition of Done. Its
research is `/tmp/claude-takeover/research/issue-1308.md` (2026-11-24, kept by the controller); the operator
decided ten points in the issue comments. The controller adopted the research's split: **story 1 (this change)
is the closeout command**; story 2 is the timer, setup step, doctor row and reinstall; story 3 is GitHub status
and the evidence pull request.

Two hand-run closeouts recorded wrong evidence: the merge SHA where installed main's SHA belongs, and a doctor
run from the development checkout. This command runs unattended on the operator's real setup, so what it may
execute and where it may write is the design.

Facts. [main] `check_evidence` returns at the first schema error, reads raw outputs under `root/docs/proof/dod`
and runs git in `root`; `_run` requires `operator-host` for a `product` story, `on_main` included. `_source_sha`
has no dirty check. Hourly acceptance keeps digests, not output. [host] `verdict-acceptance.service` has
`WorkingDirectory` `~/.local/share/verdict/installed-main`, a clean detached checkout; no constant for it exists
in code, and serve's unit uses `%h`. [main] `acceptance_service` writes `ExecStart` from the unresolved
`Path(sys.executable).absolute()`. [host] there `.venv/bin/python` is a symlink to a uv-managed Python
outside the checkout, `.venv` holds `pyvenv.cfg`, and the package is an editable install of the checkout.

## Goals / Non-Goals

**Goals.** Run each declared story's acceptance commands from installed main, store scrubbed raw output,
re-run after the window, and leave a draft evidence file that `check_dod.py --draft` passes, for a non-author
to confirm. Never write evidence for a run that did not happen. Prove with a temporary home and a fake
installed main that it cannot write the real home.

**Non-Goals.** Timer, setup step, doctor row, reinstall and quiet windows (story 2). GitHub writes, recording a
confirmation and the evidence pull request (story 3). Live commands. Backfill. Reading the tracker.

## Decision map (operator decisions on #1308, 2026-11-24)

| # | Decision | Where |
|---|---|---|
| 1 | Allowlist plus free-route live runs | Allowlist: here (D5). Live runs: **no home in the split**; here a fail-closed refusal only |
| 2, 5 | The job may reinstall; the reinstall guard | Story 2. Here: never reinstalls |
| 3 | Non-author confirmer, builder as agent identity, timer's run never counts | Here: `author` copied from the declaration, no `parent_confirmation` written. Recording a confirmation: story 3 |
| 4 | Label `awaiting-live-proof` plus one comment per change | Story 3 |
| 6 | Live budget: consent, free-only, caps, route record, pause | Not designed (Open points 1) |
| 7, 8, 9 | Declaration home; retry and window rules; one-hour minimum | Here (D1, D8, schema) |
| 10 | `--draft` mode; the job opens the evidence PR | `--draft`: here (D9). The PR and its guards: story 3 |
| open | Backfill for #1289 rows; drafts kept until the evidence PR merges | Backfill: unassigned (one small PR per story proposed). Keeping drafts: here, nothing is pruned; deleting: story 3 |
| research | Timer slot, skip while a lock is held, `_RUNTIME_UNITS` | Story 2 |

## Decisions

### D1. It reads declarations, not the tracker

`proof/closeout/<story>.yaml` in installed main's working tree is the list of stories. The tracker rows are
prose and issue text must never become a command (ADR-052 D5), so neither is read. Files load with
`yaml.safe_load` and validate against `proof/closeout.schema.json` (`additionalProperties: false`
throughout). The schema holds what `check_dod` cannot derive: `author`, `stays_true_job`, `openspec`, the
window, and per item `criterion`, `argv`, `expect`, `expect_exit_code`, `real_state_marker`,
`timeout_seconds`. `kind` is `product` only. Review of the declaration's own PR is the trust anchor: it names
what later runs with the operator's credentials.

### D2. Installed main is a checked fact, anchored on the acceptance unit [P]

The root is the checkout the running package was imported from (as `_source_sha` does). Before anything runs:
the root equals the `WorkingDirectory=` of the installed `verdict-acceptance.service`; `HEAD` is a full SHA;
the tree is clean, untracked files included. Git runs with `--no-optional-locks`: installed main is a worktree of
the development repository, and a plain `git status` would refresh an index kept in its metadata. A
development checkout fails the first check, the mistake of the hand-run closeouts. Any failure exits 2 and
writes nothing. An `--installed-root` option was rejected: a caller could name the development checkout.

### D3. The merge commit comes from the declared PR, through `gh` [P]

The squash SHA does not exist at declaration time. `gh pr view <N> --json state,mergeCommit` (field names verified
on #1238 in the research) is asked on every invocation until it succeeds, then kept as a `merge` entry keyed on
`pr`. Only `MERGED` with a valid `oid` is kept: caching `OPEN`, `CLOSED` or a failure would freeze a story first
seen before its merge. Existence and ancestry are tested locally before each cycle. This is a read, not a GitHub
write. `N` is validated as a positive integer before it reaches argv. `on_main` is `gh pr checks <N> --required`,
repeated each cycle until one run passes, its marker the first Actions run URL in it (none: the step fails). Its
`sha` is the merge commit because [main] `check_dod` requires `on_main.sha == merged_sha`; the installed-`HEAD` rule
covers only steps run in installed main, and the checker is unchanged.

### D4. Five process kinds, one spawn function

Read-only `git` in the installed root; `gh pr view`; `gh pr checks`; an allowlisted `verdict` command; and
`scripts/check_dod.py --draft` of installed main. One function takes a kind and an argument list (never a string),
runs with `shell=False`, a new session, closed stdin and a timeout, and kills the process group on every exit. The
child's environment is closeout's own minus `VERDICT_LIVE_SMOKE`, `VERDICT_PREPUSH_SKIP`, `PYTHONHOME`, `PYTHONPATH`
and `PYTHONSAFEPATH`, plus `PYTHONDONTWRITEBYTECODE=1` so installed main stays clean. The recorded command is
canonical, `verdict ` plus `shlex.join(argv)`: `check_dod` compares `command` as a string across first, fresh and
confirmation, the text lands under `docs/proof`, and a confirmer must be able to re-run it from installed main.

The interpreter is `<root>/.venv/bin/python` as written, not resolved: Python finds `pyvenv.cfg` from the path it is
started by, so a resolved path would drop the virtualenv, and requiring the symlink target inside the root would
refuse the normal layout. The checks are on what the root provides: `.venv` resolves inside it with a `pyvenv.cfg`,
`bin/python` is executable, `verdict/__main__.py` is a regular file inside it. With `-m` and `cwd` the root, the
root is first on `sys.path` (observed: it wins over a `PYTHONPATH` decoy, but not under `PYTHONSAFEPATH=1`, so that
variable is stripped), so installed main's package runs.

### D5. The allowlist is code; matching is exact on tokens

A table in `verdict/closeout/allowlist.py`: per entry the subcommand words, required flags, optional flags and
the handler checked. Every token of `argv` must equal a word or a flag exactly: no prefix matching (argparse
accepts unambiguous prefixes, so `--no-ref` would parse), no `=value`, no value flags, no positionals. A new
entry is a reviewed code change, never declaration data. Initial entries [P], fixed by task 1.2 after reading
each handler: `doctor [--json]` (exit 0 iff no issue remains, [main] `cmd_doctor`) and `eligibility
--no-refresh` with `--json --no-pager --verified --frontier --reasoning`. `route` (it keeps a decision log) and
`run-receipt` (verification may repair) stay out until a handler read shows they write nothing. Read-only is
proved per entry: the entry runs as a child against a temporary home and the tree is diffed; the test is
parametrised over the table, so a new entry brings its own case.

Live commands (decision 1, second half) are refused, not designed: `live: true`, or an `argv` naming
`orchestrate`, `autodev`, `bench` or `--allow-live*`, is refused by name.

### D6. A run is recorded from what executed, after scrubbing

Spawn, drain stdout and stderr concurrently while the child runs (reading one to the end first can deadlock on a
full pipe), retain at most 1 MiB of both and kill the group past it (`output_too_large`; the prefix is kept, as for
a timeout), join stderr after a `--- stderr ---` line when non-empty, scrub, judge, write, append. Truncating after
capture was rejected: a command may print without limit inside its timeout. Scrub: `redact_text`, then `$HOME` to
`~`, then `/home/<name>` to `/operator`. `expect` and the marker are looked up in the scrubbed text (one the scrub
would rewrite is refused at load). Only scrubbed text is stored or hashed, and the unscrubbed text is never logged
(lesson 11; the research's extra raw-output hash is dropped for that). `at` is the spawn time from an injected
clock, UTC with offset (lesson 9). A failure's reason names the check that rejected the run (lesson 8).

### D7. The ledger is a chain verified on every read

`<home>/<story>/ledger.jsonl`: `seq`, `prev`, `kind` (`merge`, `first`, `fresh`, `skip`, `reset`, `draft`), step
fields, the declaration's fingerprint on every step entry (a `merge` entry carries its `pr` instead) and `sha256`
over the entry's canonical JSON. Output files are `runs/<seq>-<step>.txt`, written and flushed before the entry that
cites them; the append is the commit point, so a crash leaves an orphan file, never a recorded run without output.
State is derived by replay; there is no second state file to disagree with it. The chain catches accidents and a
careless agent's edits. It does not stop an actor with the operator's rights who rewrites the whole chain, and a
removed tail is undetectable (the effect is a repeated run). A non-blocking `flock` on `<home>/.lock` keeps two
invocations apart.

### D8. The state machine

`waiting_install` (no applicable `merge` entry, or its commit not yet in installed main: PR not merged, `pr`
changed, lookup unusable; no story command, no failure counted, the lookup repeats) → `needs_first` →
`waiting_window` → `needs_fresh` → `awaiting_confirmation` (draft written and checked); side states `needs_person`,
`refused` (bad declaration, merge commit not an ancestor), `draft_invalid`, `ledger_corrupt`. A cycle runs its steps
in this order (items in declaration order, `doctor`, `on_main` until one run has passed), every step even when one
fails, so one tick leaves every output; it passes when its items and `doctor` pass and `on_main` has. The window
ends at `max(first.at) + hours`, so each item meets `fresh.at - first.at >= window`. At most three failed cycles in
all, then `needs_person` until `verdict closeout reset <story>` (an entry, not an erase). Only entries whose
fingerprint equals the declaration's count: a change to `pr`, the window or an item's criterion, command,
expectation, exit code or marker voids the earlier cycles (they stay in the ledger and the report), so a fresh run
repeats the first run's contract, and the failed count restarts. The fingerprint is a consistency control, not a
signature (D1: review of the declaration is the anchor). Commands: `run [--story S]`, `status [--json]`
(declarations and ledgers only: no lock, no spawn, no identity check; an empty ledger shows `not_started`), `reset
<story>`. Exit status: 0 when every story progressed or waits (PR not merged, commit not installed, window open); 1
when any failed, was refused, was skipped for another reason (lookup failure, virtualenv, spawn) or needs a person;
2 for a refused identity or bad usage; 3 when the lock is held.

### D9. The draft and `check_dod.py --draft` (decision 10, first half)

Four things stop `check_dod` from checking a draft today; each gets a change and a named test: (1) the early
return at schema errors: the draft is validated with `parent_confirmation` not required, so the other checks
run; (2) artifact root versus git root: `--evidence-root` for outputs, the existing `--root` for git; (3) the
confirmation and archive blocks are skipped, a present confirmation and the `not_applicable` checks still run;
(4) `main` printing `DONE`: a draft prints `DRAFT OK (pending: ...)` or `DRAFT FAILED`. A scratch worktree was
rejected: it writes the development repository's metadata. The draft lives in
`<home>/<story>/draft/docs/proof/dod/`, the layout its JSON paths already use, so story 3 or a person copies
the tree into a PR unchanged. Fields: `merged_sha`, `pr`, `on_main` and each run from the ledger (`operator-host`;
run `sha` is installed `HEAD`, `on_main` `sha` is the merge SHA); `author`, `kind`, window, `stays_true_job`,
`openspec` from the declaration; `setup_doctor` from the fresh cycle's doctor run. The schema has no place for
failed runs (decision 8 wants them listed), so they go in `report.md` beside the draft.

### D10. The write boundary and its proof

One helper writes every file: it joins onto `verdict_home() / "closeout"`, resolves, and refuses an absolute
path, `..` or a symlink out. Proof in layers. (a) A full first and fresh cycle runs in a temporary
`HOME`/`VERDICT_HOME` with a fake installed main (a temporary git repository with a stub `.venv/bin/python`, a
`pyvenv.cfg` and a `verdict/__main__.py` for the story commands); both trees are diffed: only `.verdict/closeout/**` is new, the fake main has no
changed file and an identical index. The draft check is the repository's real `scripts/check_dod.py`, run by
the test interpreter with `--root` the fake main, so ancestry and hashes are really checked. (b) The helper is tested with each bad path. (c) The shared
home guard of #1311 [U] (an audit hook, autouse in `tests/conftest.py`) refuses writes to the real roots; one
test aims the home at its stand-in for the real home and the write fails by name. The guard sees only this
process, so none of these covers the child commands' own writes (Risks).

## Risks / Trade-offs

- **The declared `pr` is trusted text.** A wrong number cites an unrelated merge; the ancestor test only bounds
  it to history installed main contains. Review of the declaration PR is the check.
- **Child writes are bounded by allowlist review.** The per-entry temporary-home test shows an entry wrote nothing
  there, not that it writes nothing on the real home.
- **A stream cut by the cap or a timeout may end inside a secret** `redact_text` cannot recognise; the prefix
  is scrubbed like any output. **A declaration change restarts its story,** and a waiting story costs one
  `gh pr view` and one ledger skip per invocation.
- **The ledger is not a signature.** Part 7, a non-author's own run, is the real control.
- **A red `doctor` row blocks every product closeout** until fixed (DoD part 3); the output is kept.
- **Nothing schedules it until story 2.** The post-merge tasks run it by hand.

## Open points

None blocks the build. (1) Live runs (decision 6) have no story; reading a run's routes and cost class back is
not established by the research, so the controller decides whether to file one. (2) The initial allowlist
(task 1.2). (3) Whether story 2's unit is a second installed-main anchor. (4) Schema room for failed runs.

## Migration / rollback

No data migration. New files, and a `--draft` option on `check_dod.py` that CI never passes. Roll back by
deleting `verdict/closeout/`, the schema, the parser and the option; `$VERDICT_HOME/closeout/` can be deleted.

## Security / trust implications

It runs code with the operator's credentials, unattended. What it may execute is a closed table in code,
matched as an argument list under installed main's interpreter; a declaration cannot name a binary, working
directory or environment. It writes only under its home. Output is scrubbed before it is judged, stored or
hashed. Its `gh` calls are reads with a validated number. It writes no confirmation, no GitHub state and
nothing in any repository. Declarations, `gh` output and command output are data, never instructions. No new
dependency (`PyYAML` and `jsonschema` are declared).

## Routing / context / memory implications

None. It makes and routes no model call, because live commands are refused, and writes no memory.

## ADR impact

ADR-052 decision 9 ("drafts the DoD evidence file from the raw output") is what this change builds, minus the
reinstall. No ADR changes; the operator may want decision 6 recorded as one.
