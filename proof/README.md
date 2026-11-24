# BOD-89 — Canonical local / agent / CI proof pipeline

## Entrypoint (same DAG everywhere)

```bash
# Story worktree — targeted tests first
python scripts/proof/run.py --mode targeted --targets tests/test_foo.py

# Before PR / merge — full required regression (clean final proof)
python scripts/proof/run.py --mode full --no-cache --report .verdict/proof-report.json

# LLM/tool boundary stories only
python scripts/proof/run.py --mode full --llm-boundary --otel
```

GitHub Actions (`.github/workflows/proof.yml`) invokes the **same** command; the
full proof there adds `--shard K` to run it as legs (see "Sharded full proof").

## Why native runner (not Dagger)

Dagger was evaluated as a candidate. This repo already pins tool versions via
`uv.lock` / `package-lock.json` and has mature native gates (ruff, mypy, pytest,
bandit, secrets scan, build). A stdlib Python DAG runner:

- preserves **raw exit status** from each gate
- emits **machine-readable failures** for CI-fixer hydration
- avoids a second CI runtime / mandatory paid SaaS
- keeps BOD-89 as the proof engine beneath BOD-68, not a divergent CI system

The contract field `runner: native` documents this choice. A future Dagger
adapter may implement the same contract without changing the entrypoint.

## Contract

`proof/contract.yaml` defines modes, phases, and gate commands. Gates classify
as format / lint / type / schema / test / security / secret / build /
acceptance / structural / adversarial.

| Concern | Gate |
| --- | --- |
| Semgrep CE | `security_semgrep` (required in full) |
| Secrets (local-only) | `secrets` via `scripts/proof/secrets_scan.py` |
| ast-grep | `ast_grep` (optional if binary present) |
| Promptfoo | `promptfoo` only with `--llm-boundary` |
| OTel metadata | `--otel` adds compatible spans to the report |

## Machine-readable failure shape

```json
{
  "gate": "lint",
  "command": ["ruff", "check", "."],
  "file": "tests/test_example.py",
  "test": "test_broken",
  "exit_code": 7,
  "log_slice": "...",
  "classify": "lint"
}
```

## BOD-68 consumption

BOD-68 (PR/CI/merge automation) should:

1. Run `python scripts/proof/run.py --mode targeted ...` during LOCAL_VALIDATION
2. Run `python scripts/proof/run.py --mode full --no-cache --report <path>` before
   PR open / merge
3. Hydrate CI-fixer from `failures[]` (bounded log slices), not full CI logs
4. Treat `ok: false` or non-zero exit as proof incomplete — do not open PRs
5. Never invent a parallel gate list; extend `proof/contract.yaml` instead

## Sharded full proof (CI)

`modes.full.shards` in `proof/contract.yaml` declares how many legs the full proof
runs as in CI (starting value 3). The workflow job `proof-shard` is a matrix over
`1..shards`; `tests/test_proof_workflow.py` fails when the matrix and the contract
disagree. Local and agent runs are unsharded.

```bash
# One leg of the full proof (CI runs this once per leg)
python scripts/proof/run.py --mode full --no-cache --shard 2 --report proof-shard-2/proof-report-full.json
```

- **Every leg runs every gate** with the contract's command and `required` flag.
  Only `unit_full` is divided: the runner appends `-m "not timing" --splits N
  --group K --splitting-algorithm least_duration --junitxml=...` after the gate's
  own, unchanged command. `unit_targeted` runs in every leg, as the single job did.
- **`unit_timing`** runs the `timing` tests serially, once, in leg 1 (`-p no:xdist`).
  The runner launches it without `PYTEST_ADDOPTS` (the gate's `env_clear`), so the
  workflow's `-p xdist -n auto` cannot reach it. In every other leg, and in an
  unsharded run, it is recorded as skipped: an unsharded `unit_full` runs the
  timing tests itself.
- **Local runs are unsharded.** `python scripts/proof/run.py --mode full --no-cache`
  runs `pytest tests/ -q --tb=short` exactly as before, records `shard: null` and
  `unit_timing` as skipped. `--shard` is refused (exit 2, before any gate) in
  targeted mode, when the contract declares no `shards`, and for an index outside
  `1..shards`.
- **A leg writes `proof-shard-K/`**: `proof-report-full.json` (today's report plus
  `shard` and `targets`), `ids-<gate>.json` (the test ids the gate selects, collected
  before it runs with `scripts/ci_shard_union.py --emit-ids`; a failed collection
  fails the gate instead of running it) and `junit-<gate>.xml`.

### The aggregate: `proof-full`

The job `proof-full` (`needs: [proof-shard]`, `if: always()`) runs
`scripts/proof/aggregate.py`. It keeps the check name that reports on every pull
request head today, and it passes only when every check holds:

| Id | Check |
| --- | --- |
| A0 | The `proof-shard` job result is `success`. |
| A1 | Reports exist for exactly legs `1..shards`: none missing, repeated or outside the range. |
| A2 | Each report is a full-mode, no-cache run of this contract, for its leg, on the commit the aggregate runs on. |
| A3 | Each gate has the contract's command and `required` flag for that leg, and the status the contract implies: `pass` for a required gate that runs; `skip` only where the contract authorizes it (`unit_timing` outside leg 1, an `llm_boundary` gate, `unit_targeted` without `--targets`). A failing non-required gate is listed and does not fail the proof. |
| A4 | Each report says `ok`. |
| A5 | Only when A0-A4 hold: the legs' `unit_full` selections equal an independently collected set minus the timing tests, with no test in two legs; leg 1's `unit_timing` selection equals the collected timing tests, which are not empty; no two collected ids share one JUnit `classname` and `name`. |
| A6 | Each leg's JUnit file records exactly the tests it selected, no failure or error. Skipped cases are accepted and counted. |

Any missing, unreadable or wrongly typed input is a reason, never a crash. The
merged `proof-report-full.json` keeps today's keys and adds `shards` (per leg:
index, `ok`, gate statuses, JUnit counts). Each reason is also a `failures[]` entry
with `gate: shard_aggregate`, and every leg failure carries its `shard`. The merged
report is written even when legs failed.

If one leg is lost to the runner, the aggregate fails and names it ("leg K: report
is missing"). Use "Re-run all jobs" so that the legs that passed upload again.
If A5 reports two ids sharing a JUnit identity, rename one test module; the check is
not loosened.

To change the number of legs, change `modes.full.shards` and the matrix list in
`proof.yml` together, as one small change. The rule: reduce it when the median leg
starts more than 60 seconds after its run was created (measured by
`scripts/proof/live_acceptance.py rounds`).

### Making `proof-full` a required check (operator step)

No task, workflow or script applies branch protection. The operator applies the
command below only after the aggregate has been green on main (at least one main push
run, and the pull request runs after it). `15368` is the GitHub Actions app id that
the two existing required checks are bound to. The command sends the full list
because the API docs do not say whether `PATCH` replaces or merges `checks`.

```bash
gh api -X PATCH "repos/{owner}/{repo}/branches/main/protection/required_status_checks" --input - <<'JSON'
{"strict": false,
 "checks": [
   {"context": "ci-ok", "app_id": 15368},
   {"context": "osv-security / osv-scan", "app_id": 15368},
   {"context": "proof-full", "app_id": 15368}
 ]}
JSON
```

Read it back. Expect exactly those three contexts and `strict` false:

```bash
gh api "repos/{owner}/{repo}/branches/main/protection/required_status_checks" \
  --jq '{strict, checks: [.checks[] | {context, app_id}]}'
```

### Rollback

The same `PATCH` with only the first two checks:

```bash
gh api -X PATCH "repos/{owner}/{repo}/branches/main/protection/required_status_checks" --input - <<'JSON'
{"strict": false,
 "checks": [
   {"context": "ci-ok", "app_id": 15368},
   {"context": "osv-security / osv-scan", "app_id": 15368}
 ]}
JSON
```

Rolling back the code is a revert of the change that introduced the legs: the old
single job had the same name, so a required `proof-full` keeps reporting.
