## Why

Issue #1307. A pull request round (every workflow on the PR head) has a median
of 531 s. The `proof-full` job is 464 s of it, and it waits 45-55 s for
`proof-targeted` before it starts. Inside `proof-full`, the `unit_full` gate runs
13,606 tests in one process group. That is exactly the 13,594 non-timing tests
the four CI test shards already run, plus the 12 `timing` tests.

Three facts shape the fix:

- **The proof is not a required check.** Branch protection requires only `ci-ok`
  and `osv-security / osv-scan`. A red or cancelled proof job blocks nothing and
  `verdict/pr_watcher.py` never acts on it. The operator decided (2026-11-24)
  that the proof aggregate becomes required.
- **The timing tests run in parallel with everything else in the proof.** The
  `unit_full` gate has no marker filter and the workflow sets
  `PYTEST_ADDOPTS="-p xdist -n auto"`. The `timing` marker says "CI runs these
  serially". That is the root cause behind #1150 and #1295.
- **The saving is about 2 minutes per round, not 8.** CI's own chain (test
  shards, then `coverage`, then `build`, then `ci-ok`) takes a median of 416 s.
  Once the proof is faster than that, the round is bounded by CI. Expected
  round: about 420 s, a median of about 110 s (about 21%) below 531 s.

How this advances the mission (ETHOS): a shorter round means more story builds
per hour for `verdict orchestrate`, and a required proof check means a red proof
can no longer be merged or ignored.

## What Changes

- **Proof contract.** `proof/contract.yaml` declares how many legs the full proof
  has (`modes.full.shards`) and, per gate, how it is sharded. `unit_full` is
  split across the legs without the `timing` tests. A new gate `unit_timing`
  runs the `timing` tests serially in leg 1 only. Every other gate runs in every
  leg with its command and `required` flag unchanged. An unsharded run (`run.py
  --mode full`, the local and agent entry point) runs the same commands as today.
- **Runner.** `scripts/proof/run.py --shard K` runs leg K. It records the shard
  in the report and writes, per leg, the node ids it selected and a JUnit file.
- **Aggregate.** New `scripts/proof/aggregate.py` reads every leg's report and
  fails closed unless every leg ran, passed, ran the contract's commands, and the
  legs' tests add up to an independently collected set (union equal, pairwise
  disjoint, counts equal). It writes a merged `proof-report-full.json` in
  today's shape plus a `shards` key.
- **Workflow.** In `.github/workflows/proof.yml` the single `proof-full` job
  becomes a matrix of legs (`proof-shard`) with no wait on `proof-targeted`, and
  an aggregate job named `proof-full` (`if: always()`). Legs and the aggregate
  get timeouts. The check name `proof-full` is kept so that it already reports on
  every PR head.
- **Docs.** `proof/README.md` describes the shards, the aggregate and the exact
  branch-protection commands that the operator applies.
- **Not changed:** no gate command, no `required` flag, no test file other than
  additions, no marker, no `pyproject.toml`, no `.test_durations`. The
  `proof-targeted` job is kept as it is (the operator's decision of 2026-11-24);
  only the wait on it goes. Branch protection is not touched by any task
  (operator step, see design).

## Capabilities

### New Capabilities

- `proof-gate-sharding`: the full proof runs as a declared number of legs plus a
  fail-closed aggregate, without weakening, skipping, deselecting or re-marking
  anything the proof checks today.

### Modified Capabilities

None. No existing capability spec covers the proof pipeline.

## Impact

- `proof/contract.yaml`, `scripts/proof/{contract,runner,run}.py`,
  `scripts/proof/aggregate.py` (new), `scripts/ci_shard_union.py` (a mode that
  writes node ids; default behaviour unchanged).
- `.github/workflows/proof.yml`.
- `tests/test_proof_pipeline.py`, `tests/test_proof_aggregate.py` (new),
  `tests/test_proof_workflow.py` (new), `tests/test_ci_shard_union.py`.
- `proof/README.md`.
- CI concurrency: the proof workflow starts N legs (starting value 3) instead of
  one full job, so N more jobs run at the start of a round. Peak was
  already 18-20 jobs per PR head. Whether legs queue is measured in the live
  rounds. The operator decided (2026-11-24) to start at 3 and to reduce the count
  if the median leg starts more than 60 s after the run was created (design,
  "Decided by the operator").
- After merge, the operator applies one `gh api` command to make `proof-full` a
  required check. No task applies it.

## Evidence boundary

KNOWN (read from the repository, the GitHub API or the docs on 2026-11-24):

- Branch protection requires `ci-ok` and `osv-security / osv-scan`, both bound to
  app id 15368 (`gh api .../branches/main/protection/required_status_checks`).
- `proof-full` today: `needs: [proof-targeted]`, one step running `run.py --mode
  full --no-cache --otel --targets tests/test_proof_pipeline.py`, with
  `PYTEST_ADDOPTS="-p xdist -n auto"`.
- A pull request run records the PR merge commit as `head_sha` (report of run
  45312715275: `baba3be2...`, a merge of PR head `8cb21e28...` into `c51cd994`).
  `GITHUB_SHA` is unchanged by a re-run (docs).
- A skipped job counts as passing for a required check, so the aggregate must use
  `if: always()` and test every leg itself (GitHub docs, "troubleshooting required
  status checks"). `always()` is true "even when canceled" (documented for steps; the docs
  say nothing on a job-level `if`, and a cancelled aggregate is a non-success
  conclusion either way). `upload-artifact@v7` fails on an existing artifact name unless
  `overwrite: true`. The `env` context is not available in `strategy`.
- The 12 `timing` tests, CI's 4 shards (3,281 + 2,720 + 4,176 + 3,417 = 13,594
  non-timing tests) and the baseline medians over 28 PR rounds come from the
  research of 2026-11-24: round 531 s, `ci-ok` 416 s, `proof-full` 464 s,
  `unit_full` 422 s in the main-push report (13,545 passed, 34 skipped, 27
  xfailed). A PR run measured on 2026-11-24 had `unit_full` at 360 s.
  These baselines are `gh run` measurements, not a `docs/proof/` receipt; the
  expected values are estimates. The live acceptance (task 6.3) produces the
  receipt that the evidence file records.
- `classify_ci` treats a required `FAILURE` whose name has no infrastructure word
  as `CODE_FAILURE`, and `CANCELLED` as retryable; `rerun_check` reruns one job.
- The lint job's approval of bot-commit runs approves every `action_required` run
  of that commit, including the Proof workflow.
- On pytest 9.1.1 and xdist 3.8.0 (Python 3.10, a three-test toy file),
  `PYTEST_ADDOPTS="-p xdist -n auto"` plus `-p no:xdist -m timing` runs serially
  with no error.

INFERRED:

- Leg, aggregate and round durations (design, "Estimates"). They are derived from
  the research's gate timings, not measured.
- Whether legs queue behind CI jobs (peak 18-20 already; plan tier unknown).
- Whether the JUnit pairs of a healthy leg always equal the pairs derived from
  its selected ids, for every parameter id in the suite, and whether the xdist
  controller collects nothing (so the collector must run outside xdist).
- Whether artifacts of legs that passed in attempt 1 stay downloadable after
  "Re-run failed jobs".

NOT AVAILABLE:

- The repository's GitHub plan tier and concurrent-job limit.
- GitHub's documented answer on whether `PATCH .../required_status_checks`
  replaces or merges `checks`. The command in the design sends the full list.
- Any run of the new workflow: nothing here has run. The design PR changes
  OpenSpec files only.

## Acceptance criteria

Issue #1307's acceptance check, line by line, and how this change meets it:

| Issue line | Here |
|---|---|
| Test: the union of the shards equals today's set (same count, no duplicates, none dropped); a test in `tests/` proves the split is complete for an arbitrary collected set | Kept as written. The aggregate's union check against an independently collected set (design, A5-A6), and a generated-item-list test of the split (task 1.5) |
| Test: a failing, cancelled or missing shard fails the aggregate | Kept as written (tasks 2.1, 2.3, 2.5) |
| Live: on this PR and on main, wall time from run start to the last **required** check drops below the current `proof-full` time; record both run URLs and per-job durations | **Reworded, approved by the operator on 2026-11-24.** "Last required check" is already about 416 s (`ci-ok`), below `proof-full`'s 464 s, so it is true today. Measured instead as the last job of any workflow on the PR head. On this PR: the build PR's own Proof run (URL and per-job durations in the PR body). On main: tasks 6.1 and 6.3 |
| Gates: proof matrix, `run.py --mode full --no-cache`, lint gates and the full suite pass; no test is skipped, deselected or re-marked | Proof matrix, lint gates and focused tests: task 5.2-5.3. The full suite and the sharded proof: CI on the exact head. The unsharded `run.py --mode full --no-cache` is a whole-suite run the build must not start on the shared machine: task 6.4, from merged main on the operator's setup. "Not skipped, deselected or re-marked": design, "What the proof checks today", and task 5.1. The one marker deselection (`-m "not timing"`) is run by `unit_timing` |
| Outcome: required-check names keep working | `ci-ok` and `osv-security / osv-scan` are untouched; the aggregate keeps the name `proof-full` |
| Outcome: "(or reuses the sharded test results where the proof contract allows it)" | Not taken: it makes `unit_full` an attestation of another job's run (design, Non-goals) |

Criteria of this change:

- Every test collected by `pytest tests/` runs exactly once per proof run: in one
  `unit_full` leg, or, for the `timing` tests, in `unit_timing` in leg 1.
- The aggregate fails closed on each of: a failed, cancelled, skipped or missing
  leg; a wrong or duplicated leg index; a leg count different from the declared
  one; a mixed or wrong commit; a command or `required` flag different from the
  contract; a missing, duplicated or unexpected test; a JUnit record that differs
  from the selected tests (a substituted test with the same count included); and an unreadable or malformed leg file.
- An unsharded `run.py --mode full` runs the same argv for every gate as today.
- Nothing the proof checks today is weakened, skipped or re-marked, and no test is
  deselected except by the marker and split selections, whose combined effect is
  that every collected test runs once.
- The `timing` tests run serially in one leg.
- **Live, reworded:** over at least 10 pull-request rounds after merge, the
  median time from the first run's creation to the last job's completion, across
  every workflow on the PR head, is below the 531 s baseline and below the median
  of the same command run on pre-merge heads (expected about 420 s), and
  `proof-full` is the last job to finish in at most half of the rounds.
- `openspec validate proof-gate-sharding --strict` and the vendored validator
  pass.
