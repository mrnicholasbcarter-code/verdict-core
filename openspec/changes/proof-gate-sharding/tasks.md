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

- [ ] 2.1 Failing tests for presence and identity (design checks A0-A2, A4): leg job result not success; failed, cancelled-and-missing and skipped legs; absent report; one leg too few and one too many; duplicate and out-of-range index; count different from the contract; mixed `head_sha`; `head_sha` different from the expected commit or missing; wrong `mode`, `cache_mode`, `contract_name`, `schema_version` other than 1, `runner` other than `native`; `ok: false`; a report that is not valid JSON, is not an object, or has a wrong-typed field (`shard`, `gates`, `failures`, `otel`, or a gate or failure entry that is not an object): each fails the aggregate with a reason naming the file and raises nothing. Build the leg directories as fixtures in a temp directory. Files: `tests/test_proof_aggregate.py`. Verify: the tests fail before 2.2.
- [ ] 2.2 Implement A0-A2 and A4: discover every `proof-report-full.json` under the reports directory recursively and trust only the `shard` field; never raise; collect reasons; exit 1 on any. Files: `scripts/proof/aggregate.py`. Verify: the 2.1 tests pass.
- [ ] 2.3 Failing tests for gates (A3): altered `unit_full` command (no `-m`, no `--splits`, extra `--deselect`), flipped `required`, missing and extra gate, `unit_timing` absent in leg 1 or run in leg 2, required gate not `pass`, `unit_timing` skipped in leg 1 (fails) and in leg 2 (passes), `unit_targeted` skipped with no `targets` (passes), `targets` different between legs or a `unit_targeted` command that does not match them, a `llm_boundary` gate that ran, and a failing or skipped non-required gate that keeps `ok` true. Files: `tests/test_proof_aggregate.py`. Verify: the tests fail before 2.4.
- [ ] 2.4 Implement A3 by recomputing each leg's expected commands from the contract and the leg's recorded `targets` with the function from task 1.4. Files: `scripts/proof/aggregate.py`. Verify: the 2.3 tests pass.
- [ ] 2.5 Failing tests for accounting (A5, A6): a test missing from every leg, in two legs, or unexpected; timing ids different from T or T empty; JUnit identities different from the selection with the count unchanged (one id substituted), and a different count; JUnit failures or errors; a skipped JUnit case that is accepted and summed; two different collected ids that map to one (`classname`, `name`) pair (`tests/test_a/test_b.py::test_x` and `tests/test_a.test_b.py::test_x`): the aggregate fails and names both ids, including when a leg's JUnit file records one in place of the other; a toy-tree test where real pytest writes a JUnit file and the derived pairs equal the file's (nested class, subdirectory, parameter ids with brackets, spaces and `::`); sums recorded; missing, malformed, non-object or wrong-typed ids file or JUnit file (each asserts the aggregate raises nothing). Use a stub collector so no real suite is collected. Files: `tests/test_proof_aggregate.py`. Verify: the tests fail before 2.6.
- [ ] 2.6 Implement A5 and A6: collect F and T once through the `--emit-ids` mode, compare with `check_union`, derive each id's (`classname`, `name`) pair with one function and fail, naming both ids, when two ids in F give the same pair, parse JUnit with `defusedxml` and compare its pair multiset with the one derived from each leg's selected ids (a skipped case is accepted and counted), run these checks only when A0-A4 hold. Files: `scripts/proof/aggregate.py`. Verify: the 2.5 tests pass.
- [ ] 2.7 Failing tests, then implement the merged report: today's top-level keys, one entry per gate id (worst status, maximum duration), `shards`, `failures` with a `shard` field plus one `shard_aggregate` entry per reason, `otel.spans` with `proof.shard`, written even when legs failed and when a leg file is malformed or wrong-typed (it adds a reason and none of that file's content); `--report` and `--json` as in `run.py`. Files: `tests/test_proof_aggregate.py`, `scripts/proof/aggregate.py`. Verify: `uv run --frozen python -m pytest -q tests/test_proof_aggregate.py` is green and `uv run --frozen python scripts/check_reachability.py --ratchet-base origin/main` reports nothing new.

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

- [ ] 5.1 Show that nothing is weakened and no skip or marker is added, with one script that exits non-zero on any such change and on any git error (a bare `grep` exits 0 on a match and 1 on none, so it cannot serve as the check). Run it from the worktree as `bash <file>`; paste its output and exit code in the PR body. A hit on an added line is reported in the PR body, never suppressed. Files: none changed. Verify: it prints `nothing weakened` and exits 0; to show it can fail, run it once in a throwaway detached worktree whose last commit deletes a `skip` marker line from a test and once with a commit that adds `@pytest.mark.skip` to a test, and keep each `FAIL` line and exit 1.

```bash
set -o pipefail
removed() {  # $1: lower-case ERE a removed line must not match ('' = any removed line); rest: paths
  pat=$1; shift
  git diff -U0 --no-color origin/main...HEAD -- "$@" | awk -v pat="$pat" '
    /^diff --git/ { head = 1; next }
    /^@@/ { head = 0; next }
    !head && /^-/ && (pat == "" || tolower($0) ~ pat) { print; bad = 1 }
    END { exit bad + 0 }'
}
added() {  # $1: ERE an added line must not match (parametrize is ignored); rest: paths
  pat=$1; shift
  git diff -U0 --no-color origin/main...HEAD -- "$@" | awk -v pat="$pat" '
    /^diff --git/ { head = 1; next }
    /^@@/ { head = 0; next }
    !head && /^[+]/ { line = $0; gsub(/(pytest[.]|@)?mark[.]parametrize/, "", line); if (line ~ pat) { print; bad = 1 } }
    END { exit bad + 0 }'
}
removed 'skip|xfail|deselect|mark' tests || { echo "FAIL: tests lost a skip/xfail/deselect/mark line, or git failed"; exit 1; }
added 'pytest[.](skip|xfail|importorskip|mark[.])|unittest[.]skip|@mark[.]|pytestmark|--deselect' tests || { echo "FAIL: tests gained a skip, xfail, deselect or marker line, or git failed"; exit 1; }
removed '' proof/contract.yaml || { echo "FAIL: proof/contract.yaml lost a line, or git failed"; exit 1; }
git diff --quiet origin/main...HEAD -- pyproject.toml .test_durations || { echo "FAIL: pyproject.toml or .test_durations changed, or git failed"; exit 1; }
echo "nothing weakened"
```
- [ ] 5.2 `uv run --frozen ruff check . && uv run --frozen ruff format --check .`, `uv run --frozen mypy verdict --strict`, `uv run --frozen python scripts/verify_proof_matrix.py`, `uv run --frozen python scripts/check_lint_gates.py`, `git diff --check`. Files: none changed. Verify: all exit 0.
- [ ] 5.3 Run the focused tests for the changed files: `uv run --frozen python -m pytest -q -n 2 tests/test_proof_pipeline.py tests/test_proof_aggregate.py tests/test_proof_workflow.py tests/test_ci_shard_union.py tests/test_proof_matrix_doc.py`. Files: none changed. Verify: all pass.

## 6. Live acceptance

Verification for the group: each item runs on merged main and its raw output (commands, run URLs, dates) is kept for the evidence file that AGENTS.md's definition of done requires. Items 6.1 to 6.3 each end in a checker that exits non-zero when its condition fails; keep the checker's raw output with the downloads. Building that file and archiving the change follow AGENTS.md and are not tasks here. The branch-protection command is the operator's, after 6.1; no task applies it.

- [ ] (post-merge) 6.1 On the first main push run of the Proof workflow after the merge, the `proof-full` aggregate is green, the declared number of `proof-shard` legs ran, and the downloaded merged report (`gh run download <run-id> -n proof-report-full`) has `ok: true` and one `shards` entry per leg. Its summed JUnit `tests` equals the independently collected count and, against the main push run of the merge commit's parent, differs only by the tests the merged PR added (counted from its diff). Record the summed `skipped` next to that run's 34 skipped plus 27 xfailed and explain any difference.

```bash
# From the root of a clean checkout of merged main, as `bash <file>`; the checker exits non-zero on any failed assertion.
RUN=<run-id>; PARENT_RUN=<run-id of the main push run of the merge commit's parent>
ADDED=<number of tests the merged PR added, counted from its diff>
D=$(mktemp -d)
gh run download "$RUN" -D "$D/merged" -n proof-report-full &&
gh run download "$RUN" -D "$D/legs" -p 'proof-shard-*' &&
gh run download "$PARENT_RUN" -D "$D/parent" -n proof-report-full || exit 1
COLLECTED=$(env -u PYTEST_ADDOPTS uv run --frozen python -m pytest tests/ --collect-only -q | sed -nE 's#^[= ]*([0-9]+)(/[0-9]+)? tests? collected.*#\1#p')
uv run --frozen python -I - "$D" "$COLLECTED" "$ADDED" <<'EOF'
import json, re, sys
from pathlib import Path
import yaml
from defusedxml import ElementTree as ET

d, collected, added = Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
merged = json.loads(next((d / "merged").rglob("proof-report-full.json")).read_text())
parent = json.loads(next((d / "parent").rglob("proof-report-full.json")).read_text())
legs = int(yaml.safe_load(Path("proof/contract.yaml").read_text())["modes"]["full"]["shards"])
tests = skipped = 0
for junit in (d / "legs").rglob("junit-unit_*.xml"):
    for suite in ET.parse(junit).getroot().iter("testsuite"):
        tests += int(suite.get("tests"))
        skipped += int(suite.get("skipped"))
summary = next(g["log_slice"] for g in parent["gates"] if g["id"] == "unit_full").strip().splitlines()[-1]
parent_tests = sum(int(n) for n, w in re.findall(r"(\d+) (passed|failed|skipped|xfailed|xpassed|errors?)", summary))
assert merged["ok"] is True, "merged report is not ok"
assert len(merged["shards"]) == legs, f"{len(merged['shards'])} shard entries, contract declares {legs}"
assert tests == collected, f"JUnit tests {tests} != independently collected {collected}"
assert tests == parent_tests + added, f"JUnit tests {tests} != parent {parent_tests} + added {added}"
print(f"ok: tests={tests} skipped={skipped} (record beside the parent's 34 skipped + 27 xfailed)")
EOF
```
- [ ] (post-merge) 6.2 In the leg-1 artifact of that run (`gh run download <run-id> -n proof-shard-1`), the `unit_timing` gate passed with as many tests as `pytest tests/ --collect-only -q -m timing` lists (12 on 2026-11-24), and its log slice in `proof-report-full.json` shows no `[gw` worker prefix.

```bash
# From the root of a clean checkout of merged main, as `bash <file>`.
RUN=<run-id>; D=$(mktemp -d)
gh run download "$RUN" -D "$D" -n proof-shard-1 || exit 1
EXPECTED=$(env -u PYTEST_ADDOPTS uv run --frozen python -m pytest tests/ --collect-only -q -m timing | sed -nE 's#^[= ]*([0-9]+)(/[0-9]+)? tests? collected.*#\1#p')
uv run --frozen python -I - "$D" "$EXPECTED" <<'EOF'
import json, sys
from pathlib import Path
from defusedxml import ElementTree as ET

d, expected = Path(sys.argv[1]), int(sys.argv[2])
report = json.loads(next(d.rglob("proof-report-full.json")).read_text())
gate = next(g for g in report["gates"] if g["id"] == "unit_timing")
suites = ET.parse(next(d.rglob("junit-unit_timing.xml"))).getroot().iter("testsuite")
tests = sum(int(s.get("tests")) for s in suites)
assert gate["status"] == "pass", f"unit_timing status {gate['status']}"
assert tests == expected > 0, f"{tests} timing tests ran, {expected} collected"
assert "[gw" not in gate["log_slice"], "worker prefix in the unit_timing log slice"
print(f"ok: {tests} timing tests, serial")
EOF
```
- [ ] (post-merge) 6.3 Measure at least 10 pull-request rounds after the merge, and the same number of rounds before it, with one command. A round is the earliest `createdAt` to the latest `updatedAt` of the `pull_request` runs on one PR head (`gh run list --commit <sha> --event pull_request --limit 100 --json databaseId,workflowName,createdAt,updatedAt,conclusion`; the event filter keeps push and manual runs on the same commit out of the round, and a count of 100 is far above the runs per head); which job finished last comes from `gh api repos/{owner}/{repo}/actions/runs/<run-id>/jobs`. Record both medians and ranges beside the research's 531 s (which it derived from job times), each round's run URLs and per-job durations, each leg's start delay (its job's `started_at` minus its run's `createdAt`) and the median over all legs of all measured post-merge rounds, the leg duration spread, and how often `proof-full` was the last job to finish. Passes when the post-merge median is below 531 s and below the same-command pre-merge median, and `proof-full` was last in at most half of the rounds. The median start delay is the figure the operator's rule on the shard count uses (design, "Decided by the operator", 1): above 60 s, the count is reduced by a later change. This task records it and does not pass or fail on it.

```bash
# pre-heads.txt and post-heads.txt: one PR head SHA per line, the same number in each, at least 10. As `bash <file>`.
D=$(mktemp -d)
fetch() {  # $1: file of PR head SHAs; $2: output folder
  mkdir -p "$2"
  while read -r sha; do
    gh run list --commit "$sha" --event pull_request --limit 100 \
      --json databaseId,workflowName,createdAt,updatedAt,conclusion > "$2/$sha.runs.json" || return 1
    for id in $(jq -r '.[].databaseId' "$2/$sha.runs.json"); do
      gh api --paginate "repos/{owner}/{repo}/actions/runs/$id/jobs" \
        --jq '.jobs[] | {name, started_at, completed_at}' > "$2/$sha.$id.jobs.ndjson" || return 1
    done
  done < "$1"
}
fetch pre-heads.txt "$D/pre" && fetch post-heads.txt "$D/post" || exit 1
python3 -I - "$D/pre" "$D/post" <<'EOF'
import json, statistics, sys
from datetime import datetime
from pathlib import Path

def ts(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))

def measure(folder):
    rounds, full_last, delays, durations = [], [], [], []
    for runs_file in sorted(Path(folder).glob("*.runs.json")):
        sha = runs_file.name.split(".")[0]
        runs = json.loads(runs_file.read_text())
        assert runs, f"{sha}: no pull_request runs"
        rounds.append((max(ts(r["updatedAt"]) for r in runs) - min(ts(r["createdAt"]) for r in runs)).total_seconds())
        done = []
        for run in runs:
            jobs_file = Path(folder) / f"{sha}.{run['databaseId']}.jobs.ndjson"
            for line in jobs_file.read_text().splitlines():
                job = json.loads(line)
                if not job["completed_at"]:
                    continue
                done.append(job)
                if job["name"].startswith("proof-shard"):
                    delays.append((ts(job["started_at"]) - ts(run["createdAt"])).total_seconds())
                    durations.append((ts(job["completed_at"]) - ts(job["started_at"])).total_seconds())
        full_last.append(max(done, key=lambda j: ts(j["completed_at"]))["name"] == "proof-full")
    return rounds, full_last, delays, durations

pre, post = measure(sys.argv[1]), measure(sys.argv[2])
assert len(post[0]) >= 10 and len(pre[0]) == len(post[0]), f"rounds: pre {len(pre[0])}, post {len(post[0])}; need the same number, at least 10"
assert post[2], "no proof-shard job found after the merge"
pre_median, post_median = statistics.median(pre[0]), statistics.median(post[0])
print(f"pre  median {pre_median:.0f}s, range {min(pre[0]):.0f}-{max(pre[0]):.0f}s")
print(f"post median {post_median:.0f}s, range {min(post[0]):.0f}-{max(post[0]):.0f}s")
print(f"post proof-full last in {sum(post[1])} of {len(post[1])} rounds")
print(f"post leg start delay median {statistics.median(post[2]):.0f}s (recorded, not asserted); leg duration {min(post[3]):.0f}-{max(post[3]):.0f}s")
assert post_median < 531, f"post-merge median {post_median:.0f}s is not below 531s"
assert post_median < pre_median, f"post-merge median {post_median:.0f}s is not below pre-merge {pre_median:.0f}s"
assert sum(post[1]) * 2 <= len(post[1]), "proof-full was last in more than half of the rounds"
print("ok")
EOF
```
- [ ] (post-merge) 6.4 On the operator's machine, from merged main, `uv run python scripts/proof/run.py --mode full --no-cache --report /tmp/proof-unsharded.json` passes, and the report has `shard: null`, `unit_timing` recorded as skipped and a `unit_full` that ran the unsharded command. Run it on a quiet machine: it is a whole-suite run of several minutes.
