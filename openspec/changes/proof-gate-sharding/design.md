# Design

## Context

Current state (see proposal, Why, for the numbers):

- `.github/workflows/proof.yml` has three jobs: `proof-targeted`, `proof-full`
  (`needs: [proof-targeted]`) and `proof-matrix-freshness`. `proof-full` installs
  the `uv.lock` environment (`uv sync --extra dev --extra server --extra
  dashboard`, not `--frozen`), installs Semgrep as an isolated tool and `build`,
  then runs one command with `PYTEST_ADDOPTS="-p xdist -n auto"`:
  `run.py --mode full --no-cache --otel --targets tests/test_proof_pipeline.py`.
- `proof/contract.yaml` is the only gate list ("never invent a parallel gate
  list", `proof/README.md`). In full mode the runner runs every gate in contract
  order, keeps the raw exit code, and sets `report.ok = false` only when a
  `required` gate fails or is blocked. Counts, durations and artefacts are
  recorded, never asserted.
- `unit_full` is `pytest tests/ -q --tb=short`, no marker filter. `acceptance_smoke`
  and `tests/test_proof_pipeline.py` both require a gate with that id. Local use
  (`AGENTS.md`: `run.py --mode full --no-cache`) runs the whole suite serially and
  must keep working.
- CI's `test-shard` job already splits the suite four ways with pytest-split
  (`-m "not timing" --splits 4 --group K`) and the `coverage` job proves the union
  with `scripts/ci_shard_union.py` (`check_union`: missing, extra, duplicates).
  The 12 `timing` tests run serially in `test` and in `test-shard` group 1.
- The proof workflow cancels in-flight runs of the same PR on a new push.

## Goals / Non-Goals

Goals:

- Split the full-suite gate across a declared number of legs inside the proof
  contract, so the same command still runs everywhere.
- Run the `timing` tests serially, once, in one leg.
- Prove, on every run and independently of the legs, that the legs' tests add up
  to the collected set.
- Make the aggregate a check that cannot pass unless every leg did.
- Remove the wait on `proof-targeted`.

Non-goals (design-level boundaries):

- Reusing CI's shard results instead of running tests in the proof (option A in
  the research). That would turn `unit_full` into an attestation of another job's
  run under a different dependency set and with coverage on. It weakens the gate.
- Changing xdist behaviour (`-n auto`, default `load` distribution), the
  dependency resolution (`uv sync`, not `--frozen`), the Python version, or any
  gate command.
- A separate "gates" job so that non-test gates run once (the lean variant). It
  needs gate-selection flags and gives no round benefit while CI-bound.
- Refreshing `.test_durations`, changing CI's `test-shard`, or teaching
  `pr_watcher` to rerun a single leg.
- Applying branch protection. The operator does it (Migration / rollback).
- Retiring `proof-targeted`. The operator decided to keep it (Decided by the
  operator, 2).

## Decisions

### D1. Shard inside the proof contract (option B), not around it

The contract declares the leg count and how each gate is sharded. The legs call
the same entry point as local runs. The alternative, a second gate list in the
workflow, breaks the README rule and lets CI and local runs drift. Option C
(drop only the `needs`) leaves the long pole.

```yaml
# proof/contract.yaml (shape; key names are the build's to confirm)
modes:
  full:
    phases: [format, lint, type, schema, unit_targeted, unit_full, unit_timing,
             security, secrets, structural, build, acceptance, adversarial]
    shards: 3            # decided by the operator, 1: start at 3
gates:
  - id: unit_full
    phase: unit_full
    classify: test
    command: ["pytest", "tests/", "-q", "--tb=short"]   # unchanged
    shard: split         # divided across legs when a leg index is given
  - id: unit_timing      # new, directly after unit_full
    phase: unit_timing
    classify: test
    command: ["pytest", "tests/", "-q", "--tb=short", "-m", "timing", "-p", "no:xdist"]
    shard: leader        # leg 1 only; recorded as skipped everywhere else
```

### D2. The leg count is one declared number; the matrix supplies indices

`modes.full.shards` is the single source for N. The workflow matrix lists leg
indices `1..N` (the `env` context is not available in `strategy`, and
`strategy.job-total` is not used because its docs were not read). Each leg's
runner rejects an index outside `1..N` (usage error, exit 2). The aggregate
rejects a leg count other than N. A matrix edited without the contract, or the
reverse, therefore fails closed. `tests/test_proof_workflow.py` checks that the
matrix equals `1..shards`, so the mismatch is caught before CI.

### D3. Every leg runs every gate; only `unit_full` is divided

Each leg is today's `proof-full` job with the shard flag added: same checkout
(`fetch-depth: 0`), Python 3.13, `uv sync`, Semgrep, `build`, `--no-cache`,
`--otel`, `--targets`. Non-test gates (about 56 s) therefore run N times. That
keeps one entry point and one gate list and cannot skip a gate. It costs runner
minutes (free on a public repository) and multiplies flake exposure of the
non-test gates by N (Risks).

### D4. How the runner shards a gate

`run.py --mode full --shard K` (K in `1..N`; refused in targeted mode and when
the contract declares no `shards`). The runner **appends** arguments after the
gate's own command, so an unsharded argv is the contract's command, byte for
byte.

| Run | `unit_full` argv | `unit_timing` |
|---|---|---|
| unsharded (local, agents) | `pytest tests/ -q --tb=short` | recorded `skip`, "unit_full runs the timing tests" |
| leg K of N | `pytest tests/ -q --tb=short -m "not timing" --splits N --group K --splitting-algorithm least_duration --junitxml=proof-shard-K/junit-unit_full.xml` | K = 1: `... -m timing -p no:xdist --junitxml=proof-shard-1/junit-unit_timing.xml`; K > 1: recorded `skip` |

- `-m "not timing"` is the only marker deselection the change introduces. The
  12 tests it removes from `unit_full` are exactly the ones `unit_timing` runs.
  `--splits/--group` also deselects, in each leg, what another leg runs. The
  aggregate proves both: nothing is lost by the marker or by the split (D6, A5).
- pytest-split runs after `-m` deselection (`pytest_collection_modifyitems` is
  `trylast`; CI relies on the same order). `least_duration` sorts the items by
  their string form before assigning them, so the partition depends much less on
  collection order than the default `duration_based_chunks`, which cuts the
  collected list in order.
- `PYTEST_ADDOPTS="-p xdist -n auto"` stays on the legs and still reaches
  `unit_timing`. In a toy check (proposal, Evidence boundary) `-p no:xdist` after
  it ran serially with no error. The build must prove it on CI: the leg-1 log of
  `unit_timing` shows 12 passed and no `[gwN]` worker prefix.
- Before each sharded pytest gate the runner records which test ids it will
  select, by running the same selection with `--collect-only` through
  `scripts/ci_shard_union.py --emit-ids <file>` (a new mode; the default
  behaviour is unchanged). The collector runs outside xdist and with
  `PYTEST_ADDOPTS` cleared, because an xdist controller collects nothing.
  Both the runner and the aggregate derive the selection arguments from one
  function, so they cannot diverge.
- A leg writes to `proof-shard-K/`: `proof-report-full.json` (today's report plus
  `"shard": {"index": K, "count": N}`, `null` when unsharded, and `"targets"`, the
  `--targets` it was given), `ids-<gate>.json` and `junit-<gate>.xml`. The
  `unit_targeted` command depends on `--targets`, which the workflow (not the
  contract) supplies; recording it lets the aggregate recompute that command.
- `classify_failure` has a hard-coded id map; `unit_timing` is added as `test`.

### D5. Workflow shape

```yaml
jobs:
  proof-targeted: ...        # kept as is (decided by the operator, 2); nothing waits on it
  proof-shard:               # new matrix, no `needs`
    runs-on: ubuntu-latest
    timeout-minutes: 20
    strategy: {fail-fast: false, matrix: {shard: [1, 2, 3]}}   # 1..shards (starting value 3)
    steps: [today's proof-full steps, then
      run.py --mode full --no-cache --otel --targets tests/test_proof_pipeline.py
             --shard ${{ matrix.shard }} --report proof-shard-${{ matrix.shard }}/proof-report-full.json --json,
      upload-artifact (if: always(), name: proof-shard-${{ matrix.shard }},
                       path: proof-shard-${{ matrix.shard }}/, overwrite: true)]
  proof-full:                # the aggregate; same check name as today
    needs: [proof-shard]
    if: always()
    timeout-minutes: 10
    steps: [checkout fetch-depth 0, python 3.13, uv, uv sync (same extras),
            download-artifact@v7 pattern proof-shard-* (continue-on-error),
            uv run python scripts/proof/aggregate.py --reports . --leg-result "${{ needs.proof-shard.result }}"
                         --expected-sha "$GITHUB_SHA" --report proof-report-full.json --json,
            upload-artifact proof-report-full (if: always())]
```

- `if: always()` is required: a skipped required check counts as passing, and a
  skipped dependency may not block a merge (GitHub docs). The aggregate never
  trusts `needs` alone; it tests every leg itself, as `ci-ok` does.
- `overwrite: true`: `upload-artifact@v7` fails on a name that already exists
  and legs upload with `if: always()`, so a re-run leg would otherwise fail its
  own upload.
- The download step can end with one artifact (extracted into the target
  directory, not a subdirectory: download-artifact docs) or none. The aggregate
  therefore finds every `proof-report-full.json` under `--reports` recursively
  and trusts only the `shard` field inside each report, never a directory name.
  Missing legs are reported by the script, so the step is `continue-on-error`.
- Timeouts: a required check must not hang for the 6-hour default. Legs get 20
  minutes (CI's `test-shard`), the aggregate 10 (CI's `coverage`).
- The aggregate installs the same environment as the legs because it collects
  the reference set (D6). `fetch-depth: 0` for parity with the legs.
- `needs.proof-shard.result` for a matrix is passed in as an extra input. It is a
  cheap first guard that names the cause; correctness rests on the report checks,
  so the aggregate is correct even if that value is weaker than assumed.

### D6. The aggregate: what it checks and how it fails

`scripts/proof/aggregate.py` never raises. Every failure, including an unreadable
or non-object file, becomes a reason; the exit code is 1 when there is any.

| Id | Check |
|---|---|
| A0 | The matrix result passed in is `success`. |
| A1 | Reports exist for exactly leg indices `1..N`, N = the contract's `shards`: no missing, duplicate or extra index; each file is a JSON object. |
| A2 | Each report: `schema_version` 1, `mode` `full`, `cache_mode` `no-cache`, same `contract_name`, `runner` `native`, `shard` equal to `{index, count: N}`, `head_sha` a non-empty string equal to the aggregate's own `GITHUB_SHA`. |
| A3 | Each report's gate ids are exactly the contract's full-mode gates. Each recorded `command` equals the command recomputed from the contract for that leg (so a leg cannot drop `-m`, `--splits`, `--group`, or add a deselect). Each `required` flag equals the contract's. Every **required** gate has the status the contract implies for that leg, taken from the same function the runner uses to decide a skip: `pass`, or `skip` only where the contract authorizes it (`unit_timing` in every leg but 1, a gate conditioned on `llm_boundary`, `unit_targeted` when no `--targets` are given). A required gate that is `skip` anywhere else fails, so `unit_timing` skipped in leg 1 fails and skipped in leg 2 passes. The `targets` are the same in every leg and the recomputation uses them (the workflow test pins the value). A failing or skipped **non-required** gate keeps today's meaning: it is listed, and does not fail the aggregate. |
| A4 | Each report's `ok` is true. |
| A5 | Only when A0-A4 hold: reference sets F (all ids collected with no marker) and T (collected with `-m timing`) are collected by the aggregate, independent of the legs. The legs' `unit_full` id sets must have union = F minus T (nothing missing, nothing extra) and be pairwise disjoint, using `check_union`. Leg 1's `unit_timing` ids must equal T, and T must not be empty. |
| A6 | What each leg executed is what it selected. pytest writes every test case's `classname` and `name` into JUnit from the node id by a fixed rule (path to a dotted module, class names joined, parameter id appended to the name). The aggregate derives the same pair from each selected id and requires the multiset of pairs in the leg's JUnit file to equal the multiset derived from its selection: same identities, same number, nothing substituted, no failure or error. The sum over legs equals the size of F. Sums of `tests` and `skipped` are recorded in the merged report. |

The merged `proof-report-full.json` keeps today's top-level keys (`ok`, `mode`,
`cache_mode`, `runner`, `contract_name`, `head_sha`, `gates`, `failures`, `otel`)
and adds `shards` (per-leg index, ok, gate statuses, JUnit counts). `gates` has
one entry per gate id in contract order: `fail` or `blocked` if any leg's entry
was, `skip` only if every leg skipped it, else `pass`; `duration_ms` is the
maximum over legs. `failures[]` is the union of the legs' failures, each with a
`shard` field, plus one entry per aggregate reason (`gate`: `shard_aggregate`,
`classify`: `test`, `required`: true, bounded log slice). `otel.spans` is the
union, each with a `proof.shard` attribute. `ok` is true only when there is no
reason and no required failure. The merged report is written even when legs
failed, so repair tooling still gets `failures[]` (`proof/README.md`, BOD-68).

### D7. Name the aggregate `proof-full`

The check already reports on every PR head, so making it required leaves no
window where a required check is "expected" but never reports, and a revert
restores a job of the same name. Nothing in the repository depends on the name
except fixture literals in `tests/test_ci_policy.py` and `tests/test_pr_watcher.py`.
Legs are `proof-shard (1)`, `(2)`, ...; they are not required.

### D8. Drop the wait on `proof-targeted`

`proof-shard` has no `needs`. The operator decided (2026-11-24) to keep
`proof-targeted`: it stays a job of `proof.yml`, unedited, as an early signal
that runs off the critical path, and no job waits on it. This change does not
retire it (Decided by the operator, 2).

## Differences from the research

The research of 2026-11-24 (`issue-1307.md`, kept in
`.claude/tools-backup/research/` per the issue comment) recommends option B,
N = 3, drop the `proof-targeted` dependency. This design follows it and differs
in five places:

1. **An append rule and a per-gate `shard:` field, not a `{shard}` placeholder.**
   Appending leaves an unsharded argv equal to the contract's command by
   construction, and one field also expresses "leg 1 only". Same README rule: the
   contract is extended, not paralleled.
2. **The leg count comes from the contract** (`modes.full.shards`), not only from
   the workflow matrix, so the aggregate has a number to check against.
3. **Legs record the ids they selected** (`ids-<gate>.json`) so the aggregate can
   run `check_union` on id sets and not only compare junit counts.
4. **The aggregate installs the environment and collects its own reference set.**
   The research estimated the aggregate at about 15 s; with that independence it
   is about 55 s (Estimates). This is why N = 2 loses its margin, and why the
   design does not claim "every N from 2 is under CI's 416 s".
5. **No `.test_durations` refresh.** The research lists it with `least_duration`;
   this design uses only the flag, because a refresh needs a full-suite run that
   belongs to CI, and the live evidence reports the leg spread.

## What the proof checks today, and where it is checked after

| Checked today | Checked after | What proves it |
|---|---|---|
| Every gate of full mode (16 entries in today's report) runs with the contract's commands; each `required` gate exits 0 | Every leg runs every gate; the aggregate compares each recorded command and `required` flag to the contract and requires each gate's status to be the one the contract implies for that leg: `pass`, or an authorized `skip` (A3) | `tests/test_proof_aggregate.py`: altered command, flipped `required`, missing gate each fail |
| `unit_full` runs all 13,606 tests, exit 0 | The 13,594 non-timing tests run once each across the legs; the 12 timing tests run once in `unit_timing`; exit 0 in every leg | Union, disjointness, F and T from an independent collection (A5); executed identities equal to the selection (A6) |
| `unit_full` deselects nothing today | `-m "not timing"` deselects exactly T from `unit_full` (T is run by `unit_timing`, serially); `--splits/--group` deselects per leg what another leg runs | A5: leg 1's ids equal T and T is non-empty; a leg-1 `unit_timing` log with 12 passed and no worker prefix |
| Local `run.py --mode full --no-cache` runs the whole suite | Same argv for every gate; `unit_timing` is recorded `skip` | Test asserts the exact `unit_full` argv; `acceptance_smoke` passes |
| Same environment | Same steps in every leg: Python 3.13, `uv sync` with the same extras, `fetch-depth: 0`, Semgrep tool, `build`, `PYTEST_ADDOPTS`, `--no-cache`, `--otel`, `--targets` | `tests/test_proof_workflow.py` compares the leg's setup with the recorded steps |
| 34 skipped and 27 xfailed tests (decided by the tests themselves) | Nothing re-marks a test. The aggregate records summed JUnit `skipped` (JUnit reports xfail as skipped; INFERRED) | `git diff origin/main...HEAD` shows no removed `skip`, `xfail`, `deselect` or marker line and an empty `pyproject.toml` diff (task 5.1); live total compared with the baseline |
| `structural_risk` fails (not required); `ast_grep` skips with exit 127 | Same `required` flags and statuses in every leg; `ok` unchanged by them | Aggregate test: non-required failure keeps `ok` true |
| A red `proof-full` blocks nothing | Required once the operator applies the command | The aggregate fails closed on every case above |

## Estimates (INFERRED, not measured)

From the research's gate timings: setup about 21 s, non-test gates about 56 s,
`unit_full` 422 s (main push; 360 s on one PR run), 12 timing tests about 15 s
serial, leg imbalance about 15% (CI's four legs ran 126-174 s), the extra
selection pass about 12 s per leg and about 25 s for the aggregate's two
collections.

- Leg, N = 3: 21 + 56 + 141 x 1.15 + 12 = about 250 s; leg 1 about 265 s.
- Aggregate: install about 25 s, two collections about 25 s, checks about 5 s:
  about 55 s. Proof wall about 320 s, against 524 s today.
- N = 2: leg about 330 s (leg 1 about 345 s), proof wall about 400 s, against
  CI's 416 s: about 15 s of margin. N = 4: leg about 210 s, proof wall about 280 s.
- Round = max(proof wall, CI chain 416 s) = about 420 s if nothing queues:
  about 110 s below 531 s. The proof stops being the last job; CI's chain
  (`coverage`, `build`, `ci-ok`) becomes the next pole and is out of scope.

## Test strategy

Every new test is written first and shown red on unchanged code, then green.

- `tests/test_proof_pipeline.py` (extended): `shards` and `shard` parsing and
  refusal of bad values (zero, negative, boolean, string, unknown `shard` word,
  `shards` in a mode without the full phases); exact unsharded argv; leg argv for
  N = 1..5 and every K; `unit_timing` runs only for K = 1 and is recorded `skip`
  otherwise and when unsharded; index out of range exits 2; `classify_failure`.
- `tests/test_ci_shard_union.py` (extended): for generated item lists (seeded
  `random`, no new dependency) and N = 1..5, pytest-split's `least_duration`
  groups are pairwise disjoint and their union is the input; `--emit-ids` writes
  the ids and ignores `PYTEST_ADDOPTS="-p xdist -n auto"`.
- `tests/test_proof_aggregate.py` (new): the passing case, then one test per
  failure, each of which must go red when its check is removed: failed,
  cancelled-and-missing, and skipped legs; absent report; N - 1 and N + 1 legs;
  duplicate and out-of-range index; count different from the contract; mixed
  `head_sha`; `head_sha` different from the run's commit; wrong `mode`,
  `cache_mode` or `contract_name`; altered command (no `-m`, no `--splits`, extra
  `--deselect`); flipped `required`; missing gate; `targets` different between
  legs; a `llm_boundary` gate that ran; `unit_timing` absent in leg 1
  or run in leg 2; test missing from every leg; test in two legs; unexpected
  test; timing ids different from T; empty T; JUnit identities different from the
  selection (one id substituted, count unchanged), and a different count;
  failures in JUnit; `unit_timing` skipped in leg 1 (fails) and in leg 2 (passes);
  a toy-tree test in which real pytest writes a JUnit file and the derived
  (`classname`, `name`) pairs equal the file's, including a nested class, a
  subdirectory and parameter ids with brackets, spaces and `::`; missing, malformed or non-object report, ids file or JUnit
  (lesson 6: each also asserts that nothing else ran); matrix result not
  `success`; the merged report shape; non-required failure keeps `ok` true.
- `tests/test_proof_workflow.py` (new): reads `proof.yml` and the contract.
  Matrix equals `1..shards`; `proof-shard` has no `needs` and a `timeout-minutes`;
  `proof-full` has `needs: [proof-shard]`, `if: always()` and a timeout; leg
  upload uses `overwrite: true`; the leg's setup steps match the recorded list.
  It also asserts that `proof-targeted` is still a job of the workflow and that
  no job lists it in `needs` (the operator's decision 2), and nothing else about
  it.

## Risks / Trade-offs

- **[Legs queue behind CI jobs]** The proof starts N + 2 jobs (legs, `proof-targeted`,
  `proof-matrix-freshness`) instead of 2-3, against a peak of 18-20 per PR head
  and an unverified cap (Free 20, Pro 40). Legs could delay CI's own shards and
  keep the round where it is. -> Decided by the operator, 1: start at 3 and
  reduce it if the median leg starts more than 60 s after the run was created;
  measured by the live acceptance (per-job start delay).
- **[Leg imbalance]** `.test_durations` (2026-11-20) covers 12,850 of 13,606 ids.
  -> `least_duration` balances by name and duration; the live evidence reports
  the leg spread. A refresh is not part of this change.
- **[Flake exposure x N]** Non-test gates run N times and each must pass.
  -> Accepted; the failure names the leg and the gate. Not weakened.
- **[JUnit identities stricter than expected]** If the pairs recorded for a
  healthy leg differ from the pairs derived from its selection (a parameter id
  that pytest mangles in a way the toy-tree test did not cover), A6 fails
  closed. -> The build stops and reports; it does not loosen the check
  (lesson 12).
- **[A dead leg looks like a code failure]** `classify_ci` treats a required
  `FAILURE` named `proof-full` as `CODE_FAILURE`, so a leg lost to the runner
  produces a repair record, not a rerun; `rerun_check` reruns one job and its
  dependents. `ci-ok` behaves the same today. -> The aggregate's message names
  the leg and its result; a person re-runs failed jobs. Out of scope.
- **[Re-run of failed jobs]** Whether the artifacts of legs that passed in
  attempt 1 stay downloadable is undocumented (INFERRED). `GITHUB_SHA` is
  unchanged by a re-run, and dependents re-run. -> If they vanish the aggregate
  fails closed with "leg missing"; "Re-run all jobs" fixes it.
- **[Cancelled by a new push]** The existing concurrency group cancels the run;
  a cancelled required check is not a pass (success, skipped and neutral are).
  -> No change.
- **[Reference collection shares code with the legs]** Both use one collector.
  -> It runs in another job, on another machine, at another time; this equals
  what `coverage` does today for CI's shards.
- **[Required check blocks merges on a flaky proof]** That is the operator's
  decision (2026-11-24) and is applied by the operator only after the aggregate
  has been green on main.

## Security / trust implications

- The proof is defined by the PR's own checkout (contract, runner, aggregate,
  workflow), exactly as today. It is not a security boundary against the PR's
  author; review and branch protection are. The aggregate adds no new trust: it
  reads only artifacts of its own workflow run (download-artifact defaults to the
  current run) and trusts the `GITHUB_SHA` of its own job.
- JUnit files are parsed with `defusedxml` (already a dependency); a malformed
  file is a failure reason.
- No secrets are added. Permissions stay `contents: read`. Log slices stay
  bounded (4,000 characters) as today; the aggregate writes ids and counts, no
  environment or command output beyond the legs' existing slices.
- Artifacts keep 30-day retention. Id files are an estimated 400 KB per leg.

## Routing / context / memory implications

None for routing, admission, context packs or memory: this is repository tooling
for CI. One consumer is affected once the operator applies the required check:
`verdict/pr_watcher.py` and `ci_policy.classify_ci` act on `required` checks, so a
failed `proof-full` yields `CODE_FAILURE` and a cancelled one `CANCELLED` (rerun
of that job). The lint job's approval of bot-commit runs approves every
`action_required` run of that commit, including the Proof workflow, so the
required check cannot stall in `action_required` after an autofix commit.

## Migration / rollback

Rollout:

1. Merge the build PR. `proof-full` is already a name that reports on every PR
   head, so nothing changes for branch protection.
2. The aggregate is green on main (at least one main push run, and the PR runs
   after it).
3. The operator applies the command below. No task applies it.

```bash
# Apply. Sends the full list: the docs do not say whether PATCH replaces or merges
# `checks`, so this is correct either way. 15368 is the GitHub Actions app id that
# the two existing checks are bound to (read on 2026-11-24).
gh api -X PATCH "repos/{owner}/{repo}/branches/main/protection/required_status_checks" --input - <<'JSON'
{"strict": false,
 "checks": [
   {"context": "ci-ok", "app_id": 15368},
   {"context": "osv-security / osv-scan", "app_id": 15368},
   {"context": "proof-full", "app_id": 15368}
 ]}
JSON

# Read back. Expect exactly those three contexts, strict false.
gh api "repos/{owner}/{repo}/branches/main/protection/required_status_checks" \
  --jq '{strict, checks: [.checks[] | {context, app_id}]}'

# Rollback: the same PATCH with only the first two checks.
```

Rollback of the code: revert the build PR. The old single job has the same name,
so a required `proof-full` keeps reporting. Reports written by the new aggregate
keep today's keys, so readers of `proof-report-full` need no change. No data
migration.

## ADR impact

No ADR. This is repository tooling inside the existing proof contract (BOD-89).
The required-check decision is recorded in the operator's 2026-11-24 comment on
#1307; it changes merge policy only when the operator applies the command.

## Decided by the operator (2026-11-24)

The operator decided both questions that the first draft of this design raised, in
the newest operator-decisions comment on #1307. Neither changes a design decision
or an estimate above. Two things follow in the checks and tasks, and nothing else:

- Decision 2 states the kept `proof-targeted` job as one assertion: it is still a
  job of `proof.yml` and no job lists it in `needs`. It appears in the spec delta
  (Legs start without waiting), in task 3.1 and in the Test strategy; task 3.2
  now says to leave `proof-targeted` as it is.
- Decision 1 adds no task. It only fixes what task 6.3 records: each leg's start
  delay and its median.

1. **Shard count: start at 3, reduce it on queueing evidence.** `modes.full.shards`
   is 3 and the matrix lists 1, 2, 3 (task 1.8). The operator's rule is to
   **reduce it if the median leg starts more than 60 s after the run was created**,
   measured in the live rounds (task 6.3). The start delay of a leg is its job's
   `started_at` minus the creation time of its run. N = 3 is the safer start
   because this design's aggregate does its own collection, which leaves N = 2
   about 15 s of margin under CI's 416 s (Estimates, INFERRED). Changing N is two
   numbers (`modes.full.shards`, the matrix list), checked by
   `tests/test_proof_workflow.py` and by the aggregate. A reduction follows the
   measurement and is its own small change, not a task here.
   - Evidence that would change this decision: a median leg start delay above
     60 s in the live rounds. The plan tier and the concurrent-job cap are NOT
     AVAILABLE, so the measured delay decides, not an assumption about the cap.
2. **`proof-targeted` is kept, with the wait on it dropped.** It remains an early
   signal (43-46 s) off the critical path and is not retired in this change (D8).
   For any later change that considers retiring it: that would free one job slot
   at the start of a round and remove a check that every leg already repeats (the
   targeted gates are a subset of full mode).
