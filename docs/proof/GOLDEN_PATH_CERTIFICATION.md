# Orchestration golden-path certification

- **Branch:** `feat/interview-golden-path` @ `bafdfb5` (historical branch name). The fresh clone was made from this commit.
- **Date:** 2026-09-24
- **Host:** 4 CPU, 8 GB RAM. OmniRoute v3.8.50 on 127.0.0.1:20128 with the admission and
  limiter settings from [the runbook](../guides/orchestration-golden-path.md#prerequisites).
- **Evidence directory:** `~/.verdict/evidence/golden-path/`. It holds event logs, receipts,
  per-attempt records and console transcripts. Credentials were scanned out and none were found.

## Fresh-clone gates (clean `git clone`, new `uv` venv, `pip install -e ".[dev,server]"`)

| Gate | Result |
|------|--------|
| install | pass |
| `verdict --help` lists orchestrate, watch, run-receipt, supervise, eligibility | pass |
| `verdict quickstart --non-interactive --dry-run` | exit 0 |
| `verdict doctor --json` | `issues_found`: `missing_mcp_config` only. Origin/main `72cb364` shows the same result on this host, so it is an environment issue, not a regression. |
| `ruff check .` | pass |
| `mypy --strict verdict/` | pass (206 files) |
| `pytest` (full suite, `env -u LLMGATE_AUTH_TOKEN`) | **2907 passed**, 0 failed |
| live `verdict orchestrate` from the clone (certlive) | **COMPLETE**. Integration ref: 30 tests pass. |

`LLMGATE_AUTH_TOKEN` is set in the operator shell. It also breaks
`tests/integration/test_live_gateway.py` on `origin/main`, so the suite runs without it.

## Scenario matrix (live, real `cc/*` and `cx/*` capacity; faults injected and tagged)

| # | Scenario | Run | Result |
|---|----------|-----|--------|
| A | Claude worker success | live2, live9, certlive | 3 parallel nodes on distinct `cc/*` routes, VALIDATED |
| B | Claude quota -> same node to another eligible model | live9 (route quota), live7 (account quota) | sonnet-4-6 -> sonnet-5 -> haiku (live9). Provider `cc` cooled until the parsed reset (live7). |
| C | Codex quota -> same node to Claude | live12 (worker), live11 (planner) | `cx/gpt-5.5` quota -> provider `cx` cooled -> the same node moved to `cc/claude-sonnet-4-6` |
| D | Admitted but no final answer | live9, live12 | `no_final_answer` -> route cooldown -> reassigned |
| E | 429 with cooldown/reset | live9 | `rate_limited` -> provider cooldown until the Retry-After time -> sibling moved to `cx/gpt-5.5` |
| F | 401 / 402 / 403 | live8 (401), live11 (402), live12 (403) | provider- or route-scoped cooldown, then reassigned or fail-closed |
| G | timeout / transport / 5xx | certlive (timeout), live12 (5xx, transport) | reassigned. The timeout is enforced by process-group kill. |
| H | Pool exhaustion -> explicit FAIL_CLOSED | live7, live11 | `pool_exhausted -> FAIL_CLOSED` with ranked reasons, dependents BLOCKED, run BLOCKED, receipt written, no hang |
| I | Controller quota/stall detected, not hanging | live7/9/11 (planner quota -> replacement), live10 (supervisor: hung controller -> STALLED -> killed -> resumed) | COMPLETE after resume |
| J | Complex goal decomposed and run concurrently | live6, live9, live12, certlive | frontier planner -> 3-4 nodes / 2-3 layers -> concurrent workers -> integration -> OCR PASS |

In every COMPLETE run:

- the receipt integrity check passes (the event-log digest is recomputed);
- the reviewer excluded every implementer route;
- the controller re-ran the full test suite on the integration commit.

## Not certified here

- A merge to `main` and CI on the merged commit. Merging is not authorized.
- The remediation/re-review loop after a blocking OCR finding. A blocking finding stops the run
  as BLOCKED.
- Adaptive concurrency (BOD-157), and a critic pass per node.
