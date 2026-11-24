# Tasks

This change is design only: nothing below is built yet, so every task is open and
the pull request carries the `wip` label. Build tasks are never marked
`(post-merge)`; that marker is only for live checks that need merged main.

Rules for every test task: write the test first, run it on the unchanged code and
keep the red output, then make it green and keep that output for the PR body.
Each test must go red when the check it covers is removed. Run focused tests with
`-n 2` at most. Do not run the full suite; CI on the exact head is the full suite.

The leg count is "the declared number" everywhere below. Only task 1.8 sets the
starting value (3, decided by the operator on 2026-11-24; design, "Decided by the
operator", 1). The `proof-targeted` job is kept as it is, with no wait on it
(decided by the same section, 2): no task edits or removes it.

## 1. Contract and runner (an unsharded run is unchanged)

Verification for the group: `uv run --frozen python -m pytest -q -n 2 tests/test_proof_pipeline.py tests/test_ci_shard_union.py` is green, and the unsharded `unit_full` argv equals today's command exactly.

- [ ] 1.1 Failing tests for contract parsing: `shards` (positive integer; zero, negative, boolean, string and a non-full mode are refused), per-gate `shard` (`split` or `leader`; any other word is refused), `ProofContract` exposes the declared count. Files: `tests/test_proof_pipeline.py`. Verify: `uv run --frozen python -m pytest -q tests/test_proof_pipeline.py -k shard` fails before 1.2.
- [ ] 1.2 Parse `shards` and `shard` in the contract loader; refuse bad values with `ContractError`. Files: `scripts/proof/contract.py`. Verify: the 1.1 tests pass and `uv run --frozen ruff check scripts/proof` exits 0.
- [ ] 1.3 Failing tests for expansion: unsharded argv of `unit_full` equals `["pytest", "tests/", "-q", "--tb=short"]`; leg argv for N = 1..5 and every K holds `-m`, `not timing`, `--splits N`, `--group K`, `--splitting-algorithm least_duration`, `--junitxml=proof-shard-K/junit-unit_full.xml`; `unit_timing` runs only for K = 1, is recorded `skip` for K > 1 and when unsharded; an index outside `1..N`, a contract without `shards`, and `--shard` in targeted mode exit 2 before any gate; the report carries `shard` (`null` unsharded) and the `targets` it was given; `classify_failure("unit_timing")` is `test`. Files: `tests/test_proof_pipeline.py`. Verify: `uv run --frozen python -m pytest -q tests/test_proof_pipeline.py -k "shard or timing"` fails before 1.4.
- [ ] 1.4 Add `--shard K` to the entry point and the append rule, skip recording, the `shard` report field and the `unit_timing` id in the failure map. One function builds a gate's selection arguments (also used by the aggregate). Files: `scripts/proof/run.py`, `scripts/proof/runner.py`. Verify: the 1.3 tests pass.
- [ ] 1.5 Failing tests: `ci_shard_union.py --emit-ids` writes the selected ids as JSON, the existing default mode is unchanged (existing tests untouched and green), and the collector ignores `PYTEST_ADDOPTS="-p xdist -n auto"` (a controller collects nothing under xdist); for generated item lists (seeded `random`, no new dependency) and N = 1..5, the `least_duration` groups are pairwise disjoint with the input as their union. Files: `tests/test_ci_shard_union.py`. Verify: the new tests fail before 1.6; `uv run --frozen python -m pytest -q tests/test_ci_shard_union.py` afterwards.
- [ ] 1.6 Add the `--emit-ids` mode to the collector and make the runner record each sharded pytest gate's selection in `proof-shard-K/ids-<gate>.json` before it runs the gate. Files: `scripts/ci_shard_union.py`, `scripts/proof/runner.py`. Verify: the 1.5 tests pass and a runner test shows the ids file is written next to the report.
- [ ] 1.7 Failing test that loads the checked-in contract and asserts: `shards` is the starting value (3); `unit_full` keeps command `["pytest", "tests/", "-q", "--tb=short"]` and `required` true and has `shard: split`; `unit_timing` is the gate directly after `unit_full` with phase `unit_timing`, classify `test`, command `["pytest", "tests/", "-q", "--tb=short", "-m", "timing", "-p", "no:xdist"]`, `shard: leader` and `required` true; `unit_timing` is in the full phases and not in the targeted ones; every other gate's command and `required` flag equal the values on origin/main (listed in the test). Files: `tests/test_proof_pipeline.py`. Verify: the test fails before 1.8.
- [ ] 1.8 Declare `shards: 3` in the full mode, add `unit_timing` to the full phases and the gate above directly after `unit_full`, and add `shard: split` to `unit_full`. Change no existing command or `required` flag. Files: `proof/contract.yaml`. Verify: the 1.7 test passes, `uv run --frozen python scripts/proof/acceptance_smoke.py` prints `acceptance_smoke: ok`, `uv run --frozen python -m pytest -q tests/test_proof_pipeline.py` is green, and the contract line of the task 5.1 script (`removed '' proof/contract.yaml`) exits 0.

## 2. Aggregate

Verification for the group: `uv run --frozen python -m pytest -q -n 2 tests/test_proof_aggregate.py` is green; every failure test goes red when its check is removed.

- [ ] 2.1 Failing tests for presence and identity (design checks A0-A2, A4): leg job result not success; failed, cancelled-and-missing and skipped legs; absent report; one leg too few and one too many; duplicate and out-of-range index; count different from the contract; mixed `head_sha`; `head_sha` different from the expected commit or missing; wrong `mode`, `cache_mode`, `contract_name`; `ok: false`. Build the leg directories as fixtures in a temp directory. Files: `tests/test_proof_aggregate.py`. Verify: the tests fail before 2.2.
- [ ] 2.2 Implement A0-A2 and A4: discover every `proof-report-full.json` under the reports directory recursively and trust only the `shard` field; never raise; collect reasons; exit 1 on any. Files: `scripts/proof/aggregate.py`. Verify: the 2.1 tests pass.
- [ ] 2.3 Failing tests for gates (A3): altered `unit_full` command (no `-m`, no `--splits`, extra `--deselect`), flipped `required`, missing and extra gate, `unit_timing` absent in leg 1 or run in leg 2, required gate not `pass`, `unit_timing` skipped in leg 1 (fails) and in leg 2 (passes), `unit_targeted` skipped with no `targets` (passes), `targets` different between legs or a `unit_targeted` command that does not match them, a `llm_boundary` gate that ran, and a failing or skipped non-required gate that keeps `ok` true. Files: `tests/test_proof_aggregate.py`. Verify: the tests fail before 2.4.
- [ ] 2.4 Implement A3 by recomputing each leg's expected commands from the contract and the leg's recorded `targets` with the function from task 1.4. Files: `scripts/proof/aggregate.py`. Verify: the 2.3 tests pass.
- [ ] 2.5 Failing tests for accounting (A5, A6): a test missing from every leg, in two legs, or unexpected; timing ids different from T or T empty; JUnit identities different from the selection with the count unchanged (one id substituted), and a different count; JUnit failures or errors; a skipped JUnit case that is accepted and summed; two different collected ids that map to one (`classname`, `name`) pair (`tests/test_a/test_b.py::test_x` and `tests/test_a.test_b.py::test_x`): the aggregate fails and names both ids, including when a leg's JUnit file records one in place of the other; a toy-tree test where real pytest writes a JUnit file and the derived pairs equal the file's (nested class, subdirectory, parameter ids with brackets, spaces and `::`); sums recorded; missing, malformed, non-object or wrong-typed ids file or JUnit file (each asserts the aggregate raises nothing). Use a stub collector so no real suite is collected. Files: `tests/test_proof_aggregate.py`. Verify: the tests fail before 2.6.
- [ ] 2.6 Implement A5 and A6: collect F and T once through the `--emit-ids` mode, compare with `check_union`, derive each id's (`classname`, `name`) pair with one function and fail, naming both ids, when two ids in F give the same pair, parse JUnit with `defusedxml` and compare its pair multiset with the one derived from each leg's selected ids (a skipped case is accepted and counted), run these checks only when A0-A4 hold. Files: `scripts/proof/aggregate.py`. Verify: the 2.5 tests pass.
- [ ] 2.7 Failing tests, then implement the merged report: today's top-level keys, one entry per gate id (worst status, maximum duration), `shards`, `failures` with a `shard` field plus one `shard_aggregate` entry per reason, `otel.spans` with `proof.shard`, written even when legs failed; `--report` and `--json` as in `run.py`. Files: `tests/test_proof_aggregate.py`, `scripts/proof/aggregate.py`. Verify: `uv run --frozen python -m pytest -q tests/test_proof_aggregate.py` is green and `uv run --frozen python scripts/check_reachability.py --ratchet-base origin/main` reports nothing new.

## 3. Workflow

Verification for the group: `uv run --frozen python -m pytest -q tests/test_proof_workflow.py` is green and `uv run --frozen python scripts/check_lint_gates.py` passes.

- [ ] 3.1 Failing tests that read `proof.yml` and the contract: the matrix equals `1..shards`; the leg job has no `needs` and a `timeout-minutes`; the aggregate job is named `proof-full`, has `needs` on the leg job, `if: always()` and a timeout; the leg upload uses `overwrite: true` and `if: always()`; the leg's setup steps (Python 3.13, `uv sync` with the same extras, `fetch-depth: 0`, Semgrep tool, `build`, `PYTEST_ADDOPTS`, `--no-cache`, `--otel`, `--targets`) match the list recorded today; `proof-targeted` is still a job of the workflow and no job lists it in `needs`; no script or workflow writes the branch protection endpoint (`required_status_checks`). Files: `tests/test_proof_workflow.py`. Verify: the tests fail before 3.2.
- [ ] 3.2 Replace the `proof-full` job with the `proof-shard` matrix and the `proof-full` aggregate as designed (D5); leave every other job, `proof-targeted` included, as it is. Files: `.github/workflows/proof.yml`. Verify: the 3.1 tests pass; `uv run --frozen python -c "import yaml; yaml.safe_load(open('.github/workflows/proof.yml'))"` exits 0.

## 4. Documentation

Verification for the group: `uv run --frozen python scripts/check_doc_links.py` exits 0 and `uv run --frozen python -m pytest -q tests/test_proof_workflow.py` is green.

- [ ] 4.1 Failing test: `proof/README.md` names the check `proof-full`, holds the apply, read-back and rollback commands of the design, and says they are applied by the operator after the aggregate has been green on main. Files: `tests/test_proof_workflow.py`. Verify: the test fails before 4.2.
- [ ] 4.2 Document in `proof/README.md`: legs and `--shard`, the `unit_timing` gate, what the aggregate checks (A0-A6), the merged report's `shards` key and `shard_aggregate` failures, that local runs are unsharded, "Re-run all jobs" when a leg is missing, and the operator commands. Files: `proof/README.md`. Verify: the 4.1 test passes and `uv run --frozen python scripts/check_doc_links.py` exits 0.

## 5. Gates before the push

Verification for the group: every command below exits 0 and the PR body carries the red-then-green output.

- [ ] 5.1 Show that nothing is weakened, with one script that exits non-zero on any weakening and on any git error (a bare `grep` exits 0 on a match and 1 on none, so it cannot serve as the check). Run it from the worktree as `bash <file>`; paste its output and exit code in the PR body. Files: none changed. Verify: it prints `nothing weakened` and exits 0; to show it can fail, run it once in a throwaway detached worktree whose last commit deletes a `skip` marker line from a test, and keep the `FAIL` line and exit 1.

  ```bash
  set -o pipefail
  removed() {  # $1: lower-case ERE a removed line must match ('' = any removed line); rest: paths
    pat=$1; shift
    git diff -U0 --no-color origin/main...HEAD -- "$@" | awk -v pat="$pat" '
      /^diff --git/ { head = 1; next }
      /^@@/ { head = 0; next }
      !head && /^-/ && (pat == "" || tolower($0) ~ pat) { print; bad = 1 }
      END { exit bad + 0 }'
  }
  removed 'skip|xfail|deselect|mark' tests || { echo "FAIL: tests lost a skip/xfail/deselect/mark line, or git failed"; exit 1; }
  removed '' proof/contract.yaml || { echo "FAIL: proof/contract.yaml lost a line, or git failed"; exit 1; }
  git diff --quiet origin/main...HEAD -- pyproject.toml .test_durations || { echo "FAIL: pyproject.toml or .test_durations changed, or git failed"; exit 1; }
  echo "nothing weakened"
  ```
- [ ] 5.2 `uv run --frozen ruff check . && uv run --frozen ruff format --check .`, `uv run --frozen mypy verdict --strict`, `uv run --frozen python scripts/verify_proof_matrix.py`, `uv run --frozen python scripts/check_lint_gates.py`, `git diff --check`. Files: none changed. Verify: all exit 0.
- [ ] 5.3 Run the focused tests for the changed files: `uv run --frozen python -m pytest -q -n 2 tests/test_proof_pipeline.py tests/test_proof_aggregate.py tests/test_proof_workflow.py tests/test_ci_shard_union.py tests/test_proof_matrix_doc.py`. Files: none changed. Verify: all pass.

## 6. Live acceptance

Verification for the group: each item runs on merged main and its raw output (commands, run URLs, dates) is kept for the evidence file that AGENTS.md's definition of done requires. Building that file and archiving the change follow AGENTS.md and are not tasks here. The branch-protection command is the operator's, after 6.1; no task applies it.

- [ ] (post-merge) 6.1 On the first main push run of the Proof workflow after the merge, the `proof-full` aggregate is green, the declared number of `proof-shard` legs ran, and the downloaded merged report (`gh run download <run-id> -n proof-report-full`) has `ok: true` and one `shards` entry per leg. Its summed JUnit `tests` equals the independently collected count and, against the main push run of the merge commit's parent, differs only by the tests the merged PR added (counted from its diff). Record the summed `skipped` next to that run's 34 skipped plus 27 xfailed and explain any difference.
- [ ] (post-merge) 6.2 In the leg-1 artifact of that run (`gh run download <run-id> -n proof-shard-1`), the `unit_timing` gate passed with as many tests as `pytest tests/ --collect-only -q -m timing` lists (12 on 2026-11-24), and its log slice in `proof-report-full.json` shows no `[gw` worker prefix.
- [ ] (post-merge) 6.3 Measure at least 10 pull-request rounds after the merge, and the same number of rounds before it, with one command. A round is the earliest `createdAt` to the latest `updatedAt` of the `pull_request` runs on one PR head (`gh run list --commit <sha> --event pull_request --limit 100 --json databaseId,workflowName,createdAt,updatedAt,conclusion`; the event filter keeps push and manual runs on the same commit out of the round, and a count of 100 is far above the runs per head); which job finished last comes from `gh api repos/{owner}/{repo}/actions/runs/<run-id>/jobs`. Record both medians and ranges beside the research's 531 s (which it derived from job times), each round's run URLs and per-job durations, each leg's start delay (its job's `started_at` minus its run's `createdAt`) and the median over all legs of all measured post-merge rounds, the leg duration spread, and how often `proof-full` was the last job to finish. Passes when the post-merge median is below 531 s and below the same-command pre-merge median, and `proof-full` was last in at most half of the rounds. The median start delay is the figure the operator's rule on the shard count uses (design, "Decided by the operator", 1): above 60 s, the count is reduced by a later change. This task records it and does not pass or fail on it.
- [ ] (post-merge) 6.4 On the operator's machine, from merged main, `uv run python scripts/proof/run.py --mode full --no-cache --report /tmp/proof-unsharded.json` passes, and the report has `shard: null`, `unit_timing` recorded as skipped and a `unit_full` that ran the unsharded command. Run it on a quiet machine: it is a whole-suite run of several minutes.
