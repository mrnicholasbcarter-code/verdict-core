<div align="center">

# Verdict

Cheapest qualified model, named reason for every drop, signed receipt for every decision.

Verdict is a fail-closed control plane for LLM-powered workflows. Hard eligibility gates run before advisory ranking — a model that fails any gate cannot be re-admitted by a downstream score.

[![CI](https://github.com/mrnicholasbcarter-code/verdict-core/actions/workflows/ci.yml/badge.svg)](https://github.com/mrnicholasbcarter-code/verdict-core/actions/workflows/ci.yml)
[![Security](https://github.com/mrnicholasbcarter-code/verdict-core/actions/workflows/security.yml/badge.svg)](https://github.com/mrnicholasbcarter-code/verdict-core/actions/workflows/security.yml)
[![coverage gate 70%](https://img.shields.io/badge/coverage%20gate-70%25-blue.svg)](.github/workflows/ci.yml)
[![version 0.2.0](https://img.shields.io/badge/version-0.2.0-blue.svg)](pyproject.toml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![MIT license](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

[Demo](#30-second-demo) · [How it works](#how-it-works) · [Proof](#proof) · [Install](#install) · [Commands](#commands) · [Docs](#documentation) · [Limits](#limits)

</div>

## 30-second demo

Three commands. No credentials required for the first two.

```bash
# Home screen — lists all commands
verdict

# Full orchestration run with injected chaos (quota exhaustion + rate limit + no-final-answer)
verdict orchestrate "Add a tested textkit.stats feature" \
  --inject "cc/claude-sonnet-4-6=quota,no_final" \
  --inject "cc/claude-sonnet-5=no_final" \
  --inject "cc/*=rate_limit"

# Inspect the signed receipt from the last run
verdict run-receipt <run-id>
```

Live output from scenario-J (`~/.verdict/evidence/golden-path/live9-tui-140col.txt`, truncated to 25 lines):

```
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ VERDICT  autonomous control plane                                                                                                        ┃
┃ COMPLETE  |  nodes 4  running 0  validated 4  failed 0  reassignments 3  cooldowns 3                                                     ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
╭────────────────────────────────────────────────────────────────── GOAL ──────────────────────────────────────────────────────────────────╮
│ Add a tested 'textkit.stats' feature to this repo: a module textkit/stats.py providing reading_time(text, wpm=200) -> int minutes ...   │
╰──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
╭──────────────────────────── CONTROLLER ────────────────────────────╮╭──────────────────────────── PLAN / DAG ────────────────────────────╮
│ frontier controller: cc/claude-fable-5 [HEALTHY]                   ││ topology: PARALLEL_WORK_UNITS                                      │
│ PLANNING frontier decomposition on cc/claude-fable-5               ││ max parallel: 2                                                    │
│ HEALTHY plan produced [cc/claude-fable-5]                          ││ L0: textkit-pkg-init                                               │
╰────────────────────────────────────────────────────────────────────╯│ L1: case-module, stats-module                                      │
                                                                      │ L2: integrate-suite                                                │
                                                                      ╰────────────────────────────────────────────────────────────────────╯
╭───────────────────────────────────────────────────────────────── SELECT ─────────────────────────────────────────────────────────────────╮
│ ladder: DISCOVERED 85 > ENTITLED 16 > HEALTHY 5 > AVAILABLE 1 > ELIGIBLE 1                                                               │
│ textkit-pkg-init -> cc/claude-haiku-4-5-20251001  [subscription #0]                                                                      │
│ stats-module -> cx/gpt-5.5  [subscription #2]                                                                                            │
│ case-module -> cc/claude-haiku-4-5-20251001  [subscription #0]                                                                           │
╰──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
╭──────────────────────────────────────────────────────────────── WORKERS ─────────────────────────────────────────────────────────────────╮
│ textkit-pkg-init   ✓ VALIDATED   haiku-4-5   cc   3   19s   sonnet-4-6✗ → sonnet-5✗ → haiku-4-5✓                                        │
│ stats-module       ✓ VALIDATED   gpt-5.5     cx   2   29s   haiku-4-5✗ → gpt-5.5✓                                                       │
│ case-module        ✓ VALIDATED   haiku-4-5   cc   1   49s   haiku-4-5✓                                                                   │
│ integrate-suite    ✓ VALIDATED   -           -    1    0s   merge✓                                                                       │
╰──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
```

## How it works

A single `verdict orchestrate` call runs the full pipeline:

```
goal
 └─ CONTROLLER  frontier planner decomposes into a WorkGraph (DAG)
     └─ PLAN / DAG  topology chosen deterministically (SOLO / WORKER_CRITIC / PARALLEL_WORK_UNITS)
         └─ SELECT  per-node eligibility ladder
             │  DISCOVERED → ENTITLED → HEALTHY → AVAILABLE → TASK_ELIGIBLE → SELECTED
             └─ WORKERS  parallel execution; each node gets its own worktree + route
                 └─ RECOVERY  quota / rate-limit / timeout → same-node reroute → pool exhaustion → FAIL_CLOSED
                     └─ VERIFY  ownership check + per-node tests + integration barrier
                         └─ REVIEW  independent OCR (open-code-review); reviewer excluded from all implementer routes
                             └─ RECEIPT  signed event-log digest; replayable
```

Three-role split:

| Role | Responsibility |
|---|---|
| **Verdict** | plans, selects models, recovers from faults, verifies, writes receipts |
| **Prime Agent harness** | runs worker processes (`prime-agent -p --model <exact route>`); does not select models |
| **OmniRoute transport** | provides `/v1/models` inventory and `/v1/chat/completions` execution; is not a metadata source of truth |

Four properties hold by construction:

- **Paid is never chosen while a cheaper qualified candidate remains.** `RouteSelection` raises on construction if this is violated.
- **Every dropped candidate carries a named reason** — `policy`, `health`, `capability`, `quota`, `stale`, `opaque_mix`, `cost`, or `unclassified`.
- **Opaque `auto/*` references are not candidates.** They resolve to an unknown model at call time and are dropped.
- **An unreachable surface produces `blocked`, not a pass.** Fixture data cannot satisfy a live proof.

Orchestration is specified in [ADR-036](docs/adr/ADR-036-goal-to-receipt-orchestration.md). ADR-023 (governed swarm supervision) is superseded.

## Proof

| Evidence | Location |
|---|---|
| 2907 tests, 0 failed (CI) | [`docs/proof/INTERVIEW_GOLDEN_PATH_CERTIFICATION.md`](docs/proof/INTERVIEW_GOLDEN_PATH_CERTIFICATION.md) |
| Scenario matrix A–J (live, faults injected) | [`docs/proof/INTERVIEW_GOLDEN_PATH_CERTIFICATION.md`](docs/proof/INTERVIEW_GOLDEN_PATH_CERTIFICATION.md) |
| Evidence index | [`docs/proof/EVIDENCE_INDEX.md`](docs/proof/EVIDENCE_INDEX.md) |
| Claims audit | [`docs/proof/CLAIMS_AUDIT_2026-09-06.md`](docs/proof/CLAIMS_AUDIT_2026-09-06.md) |
| v0.3.0 boundary | [`docs/proof/RELEASE_BOUNDARY_0.3.0.md`](docs/proof/RELEASE_BOUNDARY_0.3.0.md) |

## Install

```bash
pip install verdict-core          # Python 3.10+
```

Development checkout:

```bash
git clone https://github.com/mrnicholasbcarter-code/verdict-core.git
cd verdict-core
uv sync --extra dev
```

## Quickstart

```bash
verdict setup                     # interactive config wizard
verdict models                    # qualified catalog with named drop reasons
verdict simulate "refactor auth"  # forecast tokens, cost, model — no paid call
verdict route "refactor auth"     # live route (requires gateway)
```

Credential-free fixture (no gateway, no key):

```bash
verdict quickstart --non-interactive --dry-run
```

## Commands

`verdict <command>` — or `uv run python -m verdict <command>` from a checkout. Full flags via `--help`.

**Run**

| Command | Purpose |
|---|---|
| `verdict` | Home screen — lists all commands |
| `orchestrate` | Goal → frontier plan → DAG → eligibility → parallel workers → recovery → review → receipt |
| `supervise` | Supervise an orchestration controller |
| `watch` | Live TUI view of a running orchestration |
| `run-receipt` | Show and verify an orchestration run receipt |
| `eligibility` | Show the DISCOVERED → … → SELECTED ladder for a route |
| `route` / `run` | Route a single prompt |
| `simulate` | Forecast tokens, cost, risk, model — no paid call |
| `compare` | Direct frontier call vs. Verdict route side-by-side |

**Evidence**

| Command | Purpose |
|---|---|
| `replay` | Reload a recorded execution session |
| `failover-proof` | Offline forced-failover and replay proof |
| `receipt` | Inspect durable `RoutingReceiptV1` records |
| `stats` | Routing analytics |
| `benchmark` | Reproducible local benchmark harness |
| `certify` | Emit runtime certification passport JSON |

**Models**

| Command | Purpose |
|---|---|
| `models` | Qualified catalog with named drop reasons |
| `inspect` | Inspect one model's catalog record |
| `probe` | 1-token liveness probe |
| `detect` | Detect available providers |
| `catalog` | Qualify and snapshot the OmniRoute catalog |
| `metadata` | Refresh and inspect the independent model metadata store |

**Setup**

| Command | Purpose |
|---|---|
| `setup` | Interactive setup wizard |
| `doctor` | Scan and repair config / connectivity |
| `check` | Validate config file syntax |
| `quickstart` | Credential-free deterministic demo |
| `compat` | Cross-repo contract compatibility gate (ADR-024) |
| `hook` | Manage lifecycle hooks for Claude Code / Codex |
| `memory` | Local-first unified memory management |
| `serve` | FastAPI microservice |
| `ui` | Streamlit analytics dashboard |

## Documentation

| Topic | Location |
|---|---|
| Getting started | [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md) |
| Interview golden path | [`docs/guides/interview-golden-path.md`](docs/guides/interview-golden-path.md) |
| Architecture | [`docs/architecture.md`](docs/architecture.md) |
| Configuration (YAML + env) | [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md) |
| ADR index (36 numbered records) | [`docs/adr/README.md`](docs/adr/README.md) |
| Full CLI reference | [`docs/CLI_REFERENCE.md`](docs/CLI_REFERENCE.md) |
| User journey | [`docs/USER_JOURNEY.md`](docs/USER_JOURNEY.md) |
| Unknown ≠ healthy (fail-closed drops) | [`docs/guides/unknown-not-healthy.md`](docs/guides/unknown-not-healthy.md) |
| vs LiteLLM / OpenRouter / Portkey | [`docs/guides/comparison.md`](docs/guides/comparison.md) |
| Contributing | [`CONTRIBUTING.md`](CONTRIBUTING.md) |
| Security policy | [`SECURITY.md`](SECURITY.md) |

## Limits

- **Live orchestration requires a gateway.** OmniRoute must be running at `http://localhost:20128`. The credential-free quickstart and benchmark paths need no gateway.
- **Worker model availability is external.** Verdict selects from what OmniRoute reports as healthy and entitled. Quota, rate limits, and provider outages are not under Verdict's control — it recovers from them, but cannot prevent them.
- **Independent review requires `ocr` on PATH.** `open-code-review` is a separate binary. `--no-review` skips it and ends the run `BLOCKED`.
- **ADR-023 (governed swarm supervision) is superseded** by ADR-036. References to Ruflo, RuVector, SONA, hivemind, or swarm dispatch describe architecture that is no longer in Core.
- **Receipt integrity is cryptographic over event logs, not over LLM outputs.** The review step catches output problems; the receipt proves the run was not altered after the fact.
- **Version 0.2.0, active development.** Contracts, schemas, and receipt formats are versioned. Breaking changes require an ADR.

## License

MIT. See [`LICENSE`](LICENSE).
