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
    env_clear: [PYTEST_ADDOPTS]   # the runner launches this gate without these variables
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

That includes `unit_targeted`: the tests named by `--targets` (today
`tests/test_proof_pipeline.py`) run in every leg, N times in all, in addition to
their one run in `unit_full`. The single job already runs them twice, in
`unit_targeted` and again in `unit_full`. This repetition is intended, the gate
stays in every leg, and it is outside the exactly-once accounting, which covers
the combined `unit_full` and `unit_timing` gates only (A5).

### D4. How the runner shards a gate

`run.py --mode full --shard K` (K in `1..N`; refused in targeted mode and when
the contract declares no `shards`). The runner **appends** arguments after the
gate's own command, so an unsharded argv is the contract's command, byte for
byte.

| Run | `unit_full` argv | `unit_timing` |
|---|---|---|
| unsharded (local, agents) | `pytest tests/ -q --tb=short` | recorded `skip`, "unit_full runs the timing tests" |
| leg K of N | `pytest tests/ -q --tb=short -m "not timing" --splits N --group K --splitting-algorithm least_duration --junitxml=proof-shard-K/junit-unit_full.xml` | K = 1: `... -m timing -p no:xdist --junitxml=proof-shard-1/junit-unit_timing.xml`; K > 1: recorded `skip` |

- An unsharded run skips `unit_timing` only while a `shard: split` gate whose
  command `unit_timing` extends really runs in that run: selected by the mode,
  its `when` condition met, not skipped for want of `--targets`, and required if
  `unit_timing` is. When no such gate runs, `unit_timing` runs its own command, so
  the `timing` tests never go unrun. `plan_gates` decides this for the runner, the
  aggregate and `live_acceptance.py unsharded`; the checked-in contract plans
  exactly as before. A sharded gate may not take `targets_placeholder`, because
  that rule would skip it where no leg's accounting looks.
- `-m "not timing"` is the only marker deselection the change introduces. The
  12 tests it removes from `unit_full` are exactly the ones `unit_timing` runs.
  `--splits/--group` also deselects, in each leg, what another leg runs. The
  aggregate proves both: nothing is lost by the marker or by the split (D6, A5).
- pytest-split runs after `-m` deselection (`pytest_collection_modifyitems` is
  `trylast`; CI relies on the same order). `least_duration` sorts the items by
  their string form before assigning them, so the partition depends much less on
  collection order than the default `duration_based_chunks`, which cuts the
  collected list in order.
- The `timing` tests run serially by construction. The workflow keeps
  `PYTEST_ADDOPTS="-p xdist -n auto"` on every leg, but the gate `unit_timing`
  declares `env_clear: [PYTEST_ADDOPTS]` and the runner removes those names from
  the environment it launches that gate with (it builds one environment per run
  and hands it to `_run_gate`). No xdist option can reach the timing tests, so no
  test depends on what the workflow sets. `-p no:xdist` stays in the command as a
  second guard (in the toy check of the proposal it ran serially even after
  `-p xdist -n auto`). Every other gate keeps `PYTEST_ADDOPTS`. A runner unit test
  records the environment each gate is launched with (task 1.3). A log search is
  not used as evidence: with `-q` an xdist session prints "bringing up nodes..."
  and dots, with no `[gwN]` prefix (toy check, 2026-11-24), so its absence proves
  nothing.
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
| A5 | Only when A0-A4 hold: reference sets F (all ids collected with no marker) and T (collected with `-m timing`) are collected by the aggregate, independent of the legs. The legs' `unit_full` id sets must have union = F minus T (nothing missing, nothing extra) and be pairwise disjoint, using `check_union`. Leg 1's `unit_timing` ids must equal T, and T must not be empty. This accounting covers `unit_full` and `unit_timing` only; the repetition of `unit_targeted` in every leg is not counted (D3). The aggregate also derives the JUnit (`classname`, `name`) pair of every id in F with the one function A6 uses, and fails when two different ids give the same pair, naming both. |
| A6 | What each leg executed is what it selected. pytest writes every test case's `classname` and `name` into JUnit from the node id by a fixed rule (path to a dotted module, class names joined, parameter id appended to the name). The aggregate derives the same pair from each selected id and requires the multiset of pairs in the leg's JUnit file to equal the multiset derived from its selection: same identities, same number, nothing substituted, no failure or error. A skipped case (JUnit reports skip and xfail as skipped) is not a failure: it is accepted and counted. The rule is not injective (`tests/test_a/test_b.py::test_x` and `tests/test_a.test_b.py::test_x` both give classname `tests.test_a.test_b`, name `test_x`), so equal pairs prove equal ids only because A5 has shown that no two ids in F share a pair and each leg's selection lies inside F (A5 fails any extra id). The sum over legs equals the size of F. Sums of `tests` and `skipped` are recorded in the merged report. |

Why A5 rejects collisions instead of carrying full node ids in the execution
evidence: the JUnit file records `classname` and `name`, not the node id, so full
ids would need a new pytest plugin or `conftest.py` hook in every leg, more code
that runs inside the gate it checks. Rejecting a collision in F adds one check
and weakens none: with no two collected ids sharing a pair, the JUnit comparison
is an exact identity comparison. The price is that a collision between two test
modules fails the aggregate until one is renamed. That is fail-closed, and the
reason names both ids.

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

### D9. The checkers are build deliverables, not text in the tasks

Two checks decide whether the change is acceptable: that nothing is weakened
(before the push) and that the live runs meet the issue's figures (after the
merge). Each is a script with its own tests, built in group 5 of the tasks. The
tasks only run them. Neither script is embedded in the tasks: embedded scripts had
no tests of their own and each review round found a new fault in them.

**`scripts/proof/check_not_weakened.py --base <ref>`** reads the diff
`<ref>...HEAD` and exits 0 only when none of these holds:

- A skip, expected failure, deselect or marker is added to or removed from a real
  test. Every changed Python file under `tests/` (added, modified or deleted) is
  parsed with `ast` at the base and at HEAD. The constructs collected are
  decorators, calls and assignments that name `skip`, `skipif`, `xfail`,
  `importorskip`, a `pytest.mark.<name>` other than `parametrize` (also through
  `from pytest import mark`), `unittest.skip*`, `pytestmark`, `collect_ignore`,
  `collect_ignore_glob`, or the `pytest_deselected` hook. Each is keyed by file,
  enclosing definitions and its `ast.unparse` text, so reformatting does not count.
  Any difference between the two multisets fails (a new file's constructs count as
  added, a deleted file's as removed). Because only parsed code is read, a fixture
  that holds these words as data (`"--deselect"` in an argv list, a toy test
  source in a string) is not flagged. The build confirms the construct list.
- A gate in `proof/contract.yaml` is weakened: a gate of the base is missing, its
  `command` differs, its `required` flag goes from true (or absent) to false, or a
  phase of a mode of the base is missing. Added gates, phases and keys are allowed.
- The `[tool.pytest.ini_options]` table of `pyproject.toml` differs from the base
  (`tomllib`), or `.test_durations` differs (non-goal: no refresh).
- Git fails, or a changed file cannot be read or parsed. That is a failure, never
  a skip.

Exit 0 means nothing weakened, 1 a weakening (every finding is printed, naming
file, scope and construct), 2 a usage, git or parse failure. Its tests build
throwaway git repositories in a temporary directory and, for each rule, show it
failing on a positive case (added and removed) and passing on a case that holds
the same words only as strings.

**`scripts/proof/live_acceptance.py`** has three subcommands, one per live task.
Each prints the numbers it compared and exits 0 only when every assertion holds
(1 an assertion failed, 2 usage or fetch failure):

- `merged-run --run ID --parent-run ID --added N`: the merged report of the run
  has `ok: true` and one `shards` entry per declared leg; the JUnit `tests` summed
  over the leg artifacts equal the count the aggregate's own collector (`ci_shard_union.py
  --emit-ids`, `PYTEST_ADDOPTS` cleared) lists, and equal the parent run's
  `unit_full` total (read from its pytest summary line) plus N. It prints the
  summed `skipped`, to be recorded beside the parent's skips and xfails.
- `timing-leg --run ID`: in the leg-1 artifact, `unit_timing` has status `pass`
  and its JUnit `tests` equal the number of `timing` tests the collector lists,
  which must be above 0.
- `rounds --pre FILE --post FILE --rounds N`: each file lists PR head SHAs, newest
  first. For every head the checker reads the `pull_request` runs and their jobs
  through `gh`. A head counts as a round only when every run on it has finished
  and none was cancelled and, for a post-merge head, the declared number of
  `proof-shard` jobs and the `proof-full` job succeeded. A head that does not
  count is excluded and reported with its reason. Each side uses its first N
  counting heads, and the checker fails if fewer than N remain on either side.
  A round is the earliest `createdAt` to the latest `updatedAt` over its runs. It
  passes when the post-merge median is below 531 s and below the pre-merge
  median, and `proof-full` was the last job to finish in at most half of the
  post-merge rounds. It prints both medians and ranges, each leg's start delay
  (its job's `started_at` minus its run's `createdAt`) and the median over legs,
  and the leg duration spread. The start delay is recorded, never asserted (the
  operator's rule on the shard count uses it).

All GitHub access sits in one small function that the tests replace, so the tests
use synthetic report, JUnit and run files and make no network call.

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
| `unit_full` runs all 13,606 tests, exit 0 | The 13,594 non-timing tests run once each across the legs; the 12 timing tests run once in `unit_timing`; exit 0 in every leg | Union, disjointness, F and T from an independent collection and no two ids sharing a JUnit pair (A5); executed identities equal to the selection (A6). `unit_targeted` repeats its tests in every leg, as the single job already does, outside this count (D3) |
| `unit_full` deselects nothing today | `-m "not timing"` deselects exactly T from `unit_full` (T is run by `unit_timing`, serially); `--splits/--group` deselects per leg what another leg runs | A5: leg 1's ids equal T and T is non-empty; the runner test of the environment `unit_timing` is launched with (no `PYTEST_ADDOPTS`); task 7.2 checks 12 timing tests passed in leg 1 |
| Local `run.py --mode full --no-cache` runs the whole suite | Same argv for every gate; `unit_timing` is recorded `skip` | Test asserts the exact `unit_full` argv; `acceptance_smoke` passes |
| Same environment | Same steps in every leg: Python 3.13, `uv sync` with the same extras, `fetch-depth: 0`, Semgrep tool, `build`, `PYTEST_ADDOPTS` (the runner clears it for `unit_timing` only, D4), `--no-cache`, `--otel`, `--targets` | `tests/test_proof_workflow.py` compares the leg's setup with the recorded steps |
| 34 skipped and 27 xfailed tests (decided by the tests themselves) | Sharding adds no skip and re-marks nothing; the skips and expected failures the tests declare stay as they are. The aggregate accepts them and records summed JUnit `skipped` (JUnit reports xfail as skipped; INFERRED) | The weakening checker (D9, run in task 6.1) exits 0: no skip, expected failure, deselect or marker decorator or call added to or removed from a real test, found by parsing and not by matching words; live skipped total compared with the baseline (task 7.1) |
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
  otherwise and when unsharded; `env_clear` parsing (a list of strings; anything
  else refused); the environment a stub executor records for `unit_timing` has no
  `PYTEST_ADDOPTS` while `unit_full` keeps it and every other variable is kept;
  index out of range exits 2; `classify_failure`.
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
  failures in JUnit; two different collected ids that map to one pair
  (`tests/test_a/test_b.py::test_x` and `tests/test_a.test_b.py::test_x`): the
  aggregate fails and names both, including when a leg's JUnit file records one
  in place of the other; a skipped JUnit case that is accepted and summed;
  `unit_timing` skipped in leg 1 (fails) and in leg 2 (passes);
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
- `tests/test_proof_check_not_weakened.py` (new): throwaway git repositories in a
  temporary directory. For each rule a positive case fails and a string-only case
  passes: a skip, `skipif`, `xfail`, `importorskip`, marker, `pytestmark`,
  `pytest_deselected` call or `collect_ignore` assignment added to or removed
  from a test, and the same words held only in a string, an argv list and a
  triple-quoted toy source; a `parametrize` mark added (passes); a gate command
  changed, a `required` flag set to false, a gate or phase removed (each fails), a gate,
  phase or key added (passes); a `pyproject.toml` pytest setting or
  `.test_durations` changed; a bad base ref and an unparsable changed file (exit 2).
- `tests/test_proof_live_acceptance.py` (new): synthetic report, JUnit and run
  files with the GitHub layer replaced. Each subcommand passes on good data and
  fails on each wrong figure (report not ok, wrong leg count, test totals that
  differ, parent total plus added that differs, `unit_timing` not passed or with
  0 or a different count). For rounds: a head with an unfinished run, a head with
  a cancelled run and a post-merge head whose leg or aggregate failed are each
  excluded and reported; fewer than N counting heads on either side fails; a
  median above 531 s, a post median not below the pre median, and `proof-full`
  last in more than half of the rounds each fail; a good set passes and records the
  start delay without asserting it.

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
- **[Two test modules map to one JUnit identity]** If a pull request adds a test
  whose id maps to the same (`classname`, `name`) pair as another (A5), the
  aggregate fails and names both ids. -> Rename one module; the check is not
  loosened. None is expected: a pair needs a file stem with a dot or a directory
  beside a module of the same name, and `tests/` has neither on 2026-11-24 (by
  file names; A5 is the authority at build time).
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
- Decision 1 adds no task. It only fixes what task 7.3 records: each leg's start
  delay and its median.

1. **Shard count: start at 3, reduce it on queueing evidence.** `modes.full.shards`
   is 3 and the matrix lists 1, 2, 3 (task 1.8). The operator's rule is to
   **reduce it if the median leg starts more than 60 s after the run was created**,
   measured in the live rounds (task 7.3). The start delay of a leg is its job's
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
