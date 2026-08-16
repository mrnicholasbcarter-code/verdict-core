<div align="center">

# Verdict

**Use the right AI model for every task—not the most expensive one.**

Verdict stretches your Claude Code Max, Codex Pro, 9router, and OmniRoute setup further by routing each task to the least expensive capable model, while reserving frontier models for work that truly needs them. Configure once, use more of what you already have, and keep your best-model usage under control.

[![CI](https://github.com/mrnicholasbcarter-code/verdict-core/actions/workflows/ci.yml/badge.svg)](https://github.com/mrnicholasbcarter-code/verdict-core/actions/workflows/ci.yml)
[![Security](https://github.com/mrnicholasbcarter-code/verdict-core/actions/workflows/security.yml/badge.svg)](https://github.com/mrnicholasbcarter-code/verdict-core/actions/workflows/security.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![MIT license](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Status: active development](https://img.shields.io/badge/status-active%20development-orange.svg)](#project-status)

[Quickstart](#30-second-demo) · [Why it matters](#why-verdict) · [Architecture](#architecture) · [CLI](#cli-reference) · [Docs](#documentation) · [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md)

</div>

## The Problem

## Why Verdict?

### The problem it solves

Claude Code Max and Codex Pro give you access to excellent frontier models—but frontier usage is limited and expensive to burn on every action. At the same time, 9router and OmniRoute may already expose many capable alternatives: free models, low-cost models, fast models, and specialists.

Verdict is the decision layer between your task and those providers:

1. Understand the task's difficulty, risk, context, and tool needs.
2. Remove models that cannot safely or reliably handle it.
3. Send routine work to the least expensive capable option.
4. Escalate difficult or sensitive work to a frontier model when justified.
5. Record why the route was chosen and what happened.

The goal is not “always use the cheapest model.” The goal is **use the cheapest model that is good enough—and keep premium capacity available for the work that needs it.**


Most AI tooling starts with: *"which model should I use?"*

Verdict starts earlier: ***"should this action happen, which choices are allowed, and can we prove what happened?"***

In plain English: Verdict helps you use more of the models you already pay for—free, low-cost, and frontier—without sending every task to the most expensive option. It blocks choices that violate your rules, budget, privacy, or safety requirements.

It's the difference between a recommendation engine and a **control plane**:

| | Typical router | **Verdict** |
|---|---|---|
| Model selection | Heuristic tiers, static allowlists | Orchestrator proposes candidates; Verdict admits only policy- and evidence-qualified options |
| Safety | Best-effort fallback | **Fail-closed gate** — capability, budget, privacy, availability checks run *before* any upstream call |
| Unknown health | Assumed healthy | Explicit `unknown` / `error` states — **unknown ≠ healthy** |
| Explainability | "we picked GPT-4" | Per-candidate exclusion reasons, freshness timestamps, confidence, cache state — served at `GET /v1/route/explain` |
| Learning | None | Outcomes feed back through SONA / RuVector — advisory only, **never** bypasses the gate |
| Accountability | Logs | Durable, privacy-safe **evidence receipts** for every routing decision |

> An optional orchestrator can do the expensive research. The gate (deterministic Python, no LLM in the enforcement path) does the enforcing. Neither can do the other's job — by design, codified in [20+ ADRs](docs/adr/).

Verdict Core is a **deterministic execution-policy control plane** that sits between your agents and model providers:

```
┌─────────────────────────────────────────────────────────────────┐
│                        YOUR AGENTS                              │
└─────────────────────────┬───────────────────────────────────────┘
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│                    VERDICT CORE (Control Plane)                 │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐            │
│  │  Eligibility │ │  Adaptive    │ │  Evidence    │            │
│  │  Gate        │ │  Ranking     │ │  Chain       │            │
│  └──────────────┘ └──────────────┘ └──────────────┘            │
└─────────────────────────┬───────────────────────────────────────┘
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│              OMNIROUTE (Intelligent Model Router)               │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐            │
│  │  19 Strategies│ │  Quota Guard │ │  Cost/Quality│            │
│  └──────────────┘ └──────────────┘ └──────────────┘            │
└─────────────────────────┬───────────────────────────────────────┘
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│                    MODEL PROVIDERS (3500+ models)               │
│  Anthropic • OpenAI • OpenRouter • Local • Custom              │
└─────────────────────────────────────────────────────────────────┘
```

## Key Features

| Feature | Description |
|---------|-------------|
| **Deterministic Routing** | Same task → same model, every time. No randomness. |
| **Eligibility Gates** | Hard requirements (capabilities, latency, budget) enforced before ranking |
| **Adaptive Ranking** | Learns from runtime observations; promotes healthy models, demotes failing ones |
| **Evidence Chain** | Append-only audit trail: every decision has a verifiable receipt |
| **Cost Optimization** | 90% reduction vs always-frontier via task-based model selection |
| **Multi-Provider** | Anthropic, OpenAI, OpenRouter, local models — unified interface |
| **Formal Verification** | Contracts, schemas, and proofs for every layer |

## 30-Second Demo

No API keys. No config. Deterministic, offline, auditable. This is a local fixture demo—not a live provider call or production-readiness proof:

```bash
verdict quickstart --non-interactive --dry-run
```

```
Verdict credential-free quickstart
===================================
Task: Add structured output to the invoice parser
Required capabilities: structured_output, tools
Selected route: demo/frontier-tools
Excluded candidates: 3
Status: PASS
- demo/no-tools: missing capability: tools
- demo/quota-empty: quota exhausted
- demo/unverified: health unknown
```

Every exclusion carries a machine-readable reason and state (`capability_mismatch`, `quota_exhausted`, `unknown`). That's the gate working.

---

## Install

```bash
# Recommended: transparent source install (uv)
# Requires Python 3.10+ and uv.
git clone https://github.com/mrnicholasbcarter-code/verdict-core.git
cd verdict-core
uv sync --extra dev --extra server --extra dashboard

# Optional convenience installer (review release assets first; Linux/macOS)
curl -fsSL https://raw.githubusercontent.com/mrnicholasbcarter-code/verdict-core/main/install.sh | bash
```

Then:

```bash
verdict setup            # interactive setup wizard (supports --dry-run --json)
verdict doctor --fix     # scan & repair config / connectivity issues
verdict detect           # discover available LLM providers on this machine
```

## Cost Demo

```bash
# Run 100-task routing simulation showing 90% savings
python scripts/demo-routing.py

# Output shows:
# - Always Opus:      $12.47
# - Always Haiku:     $0.18
# - Verdict Routed:   $1.23
# - SAVINGS vs Opus:  $11.24 (90.1%)
# - Quality gates:    Reasoning→Frontier 88%, Simple→Cheap 92%
```

## How It Works

```
                    ┌──────────────────────────────────────────────┐
                    │           ORCHESTRATOR (frontier model)       │
                    │  1. Research the task                         │
                    │  2. Review the OmniRoute catalog              │
                    │  3. Pick right-sized candidates per slice     │
                    │  4. Dispatch workers (Ruflo swarms / agents)  │
                    └──────────────────┬───────────────────────────┘
                                       │ candidate set
                                       ▼
                    ┌──────────────────────────────────────────────┐
                    │        ELIGIBILITY GATE (deterministic)       │
                    │  ✓ Capability passports  ✓ Budget floors     │
                    │  ✓ Privacy policy        ✓ Probe-verified     │
                    │    availability (TTL + stale-while-revalidate)│
                    │                                               │
                    │  PROTECTED WORK: fail-closed when fresh truth │
                    │  is absent. Intelligence is advisory — it can │
                    │  NEVER re-admit an excluded candidate.        │
                    └──────────────────┬───────────────────────────┘
                                       │ admitted set
                                       ▼
                    Workers execute → outcomes → SONA / RuVector
                    (learning loop improves advice, not authorization)
```

**The gate has no selection logic. The orchestrator has no enforcement power.** That separation is the whole point — see [ADR-002](docs/adr/ADR-002-orchestrator-routing.md) and the [Routing Policy](docs/specs/ROUTING_POLICY.md).

### Hard guarantees (test-enforced)

- **Intelligence cannot re-admit an excluded candidate** — `test_ranker_cannot_reintroduce_excluded_candidate`
- **Protected work fails closed when fresh truth is absent** — `test_protected_work_fails_closed_when_truth_absent`
- **Gate filters before any ranking** — `test_intelligence_route_filters_before_ranking`
- **Explain surface carries per-model eligibility and exclusions** — `test_explain_surfaces_eligible_set_and_exclusions`
- **Budget, concurrency, and timeout limits enforced** — over-budget plans and replan increases rejected (`tests/test_planner.py`, `tests/test_ruflo_verification.py`)
- Every claim maps to acceptance criteria in [ACCEPTANCE_GATES.md](ACCEPTANCE_GATES.md) — CI fails if any gate lacks evidence

### Evidence-backed public claims

Every public claim carries a status in the [claims ledger](docs/proof/claims_ledger.v1.json) — verified, observed, partial, self-reported, or unsupported — indexed in the [proof matrix](docs/proof/EVIDENCE_INDEX.md). Catalog counts are historical observations, not proof that every advertised route is live. The [portfolio proof matrix](docs/portfolio/PORTFOLIO_PROOF_MATRIX.md) maps each audience story to reproducible evidence and states its limits.

---

## CLI Reference

```
verdict setup          Interactive setup wizard (plan / --dry-run / --json)
verdict route          Route a task: --criticality {critical,high,medium,low} --terse
verdict quickstart     Credential-free deterministic flagship demo
verdict detect         Discover local LLM providers (--config emits suggested config)
verdict probe          1-token liveness probe (--allow-live-probe for explicit consent)
verdict catalog        Qualify an OmniRoute catalog snapshot (bounded sample probes)
verdict benchmark      Reproducible local benchmark harness (--fixture, --output-json)
verdict stats          Routing analytics
verdict suggest        Review intelligence suggestions from past outcomes
verdict serve          Launch the FastAPI microservice
verdict ui             Launch the Streamlit analytics dashboard
verdict doctor         Scan & repair configuration (--fix, --json)
verdict runtime        Inspect/reconcile global Ruflo/RuVector ownership
verdict memory         Local-first unified memory plane (put/search/export/import/...)
verdict check          Validate config syntax and sanity
verdict uninstall      Reversibly remove hooks and MCP registrations
```

Full flags and examples: **[docs/CLI_REFERENCE.md](docs/CLI_REFERENCE.md)**

---

## Configuration

Layered — project overrides global:

1. **Global**: `~/.verdict/config.toml`
2. **Project**: `.verdict/config.toml`

```toml
[gateway]
primary_model = "anthropic/claude-3-opus-20240229"   # frontier floor for protected work
providers = {}

[intelligence]
profile = "balanced"        # fast | balanced | thorough
timeout_ms = 8000
allow_client_model_override = false

[availability]
ttl_seconds = 60            # availability cache TTL
stale_window_seconds = 30   # stale-while-revalidate window
omniroute_base_url = "http://localhost:20128"   # optional live catalog
```

Full reference: **[docs/CONFIGURATION.md](docs/CONFIGURATION.md)**

---

## Architecture

| Component | Role | Technology | Status |
|---|---|---|---|
| **EligibilityGate** | Fail-closed deterministic checks — no LLM in the request path | Python | Implemented |
| **ProbeRunner** | Consented, budgeted liveness probes | Python + httpx | Implemented |
| **OmniRoute catalog** | Optional historical catalog snapshots with bounded liveness evidence and explicit limitations | External service (optional) | Optional adapter |
| **Orchestrator** | Candidate research, assignment, and worker dispatch | Optional runtime adapters | In progress |
| **Learning loop** | Outcome → advisory feedback | Optional intelligence adapters | In progress |
| **Evidence ledger** | Durable, privacy-safe routing receipts | JSONL + signed manifests | Implemented; expanding |

Design decisions live in **[docs/adr/](docs/adr/)** — 20+ records covering the evidence ledger, orchestrator boundary, fail-closed capability passports, consented probes, catalog qualification, gateway adapter contracts, and more.

### TypeScript ecosystem

TypeScript packages are published under the current `@bodanglin/*` namespace. Check each package README and release metadata before integrating:

| Package | Description |
|---|---|
| `@bodanglin/verdict-contracts` | Canonical TypeScript contract schemas and types |
| `@bodanglin/verdict-client` | TypeScript client SDK |

Python ↔ TypeScript field-level parity is verified in CI — see [CONTRACT_PARITY.md](CONTRACT_PARITY.md).

### Ecosystem repos

| Repo | Role | Status |
|------|------|--------|
| `verdict-core` | Control plane (this repo) | Active |
| `verdict-node` | TypeScript adapter | Active |
| `verdict-ecosystem` | Cross-repo coordination | Active |
| `verdict-risk` | Risk evaluation provider | In progress |
| `verdict-strategy` | Strategy evaluation provider | In progress |
| `verdict-backtest` | Backtest provider | In progress |
| `verdict-cockpit` | UI dashboard | In progress |

---

## Security

- **Fail-closed protected work**: protected actions halt when required fresh truth is unavailable — no silent fallback
- **Consent-gated probes**: network liveness checks require explicit `--allow-live-probe`
- **Privacy**: privacy-safe receipt ledger ([THREAT_MODEL_RECEIPTS.md](docs/THREAT_MODEL_RECEIPTS.md)); logs and evidence must not contain PII
- **Supply chain**: CI runs dependency/security checks on protected-branch pushes, pull requests, and scheduled runs; see [security workflow](.github/workflows/security.yml) for scope and documented exceptions
- **Installer trust**: source/uv installation is the most transparent path; review `install.sh` and release assets before using `curl | bash`
- **Report vulnerabilities**: see [SECURITY.md](SECURITY.md)

---

## Documentation

| Topic | Link |
|---|---|
| Getting started | [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md) |
| CLI reference | [docs/CLI_REFERENCE.md](docs/CLI_REFERENCE.md) |
| Configuration | [docs/CONFIGURATION.md](docs/CONFIGURATION.md) |
| Architecture decision records (20+) | [docs/adr/](docs/adr/) |
| Routing policy | [docs/specs/ROUTING_POLICY.md](docs/specs/ROUTING_POLICY.md) |
| Acceptance gates (G1–G7) | [ACCEPTANCE_GATES.md](ACCEPTANCE_GATES.md) |
| Evidence index & claims ledger | [docs/proof/EVIDENCE_INDEX.md](docs/proof/EVIDENCE_INDEX.md) |
| Portfolio proof matrix | [docs/portfolio/PORTFOLIO_PROOF_MATRIX.md](docs/portfolio/PORTFOLIO_PROOF_MATRIX.md) |
| Capability passports | [docs/CAPABILITY_PASSPORTS.md](docs/CAPABILITY_PASSPORTS.md) |
| Contract parity (Python ↔ TS) | [CONTRACT_PARITY.md](CONTRACT_PARITY.md) |
| Autonomous development | [docs/guides/autonomous-development.md](docs/guides/autonomous-development.md) |
| OmniRoute workers | [docs/guides/omniroute-workers.md](docs/guides/omniroute-workers.md) |
| Runtime ownership | [docs/guides/runtime-ownership.md](docs/guides/runtime-ownership.md) |
| Local development | [docs/guides/local-development.md](docs/guides/local-development.md) |

---

## Development

```bash
# Tests (81 test files — unit + integration)
uv run pytest -q

# Lint & format
uv run --extra dev --extra server --extra dashboard ruff check .
uv run --extra dev --extra server --extra dashboard ruff format --check .

# Type check (strict)
uv run --extra dev --extra server --extra dashboard mypy verdict --strict

# Build
uv run python -m build
```

Contributing guide: [CONTRIBUTING.md](CONTRIBUTING.md) · Versioning: [VERSIONING.md](VERSIONING.md) · Release process: [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md)

---

## License

[MIT](LICENSE) — © Verdict contributors

---

**Built by** [Nicholas Carter](https://github.com/mrnicholasbcarter-code) — 25 years shipping systems at GM OnStar, Deloitte, BCBS Michigan, Mad Mobile/Stäubli, and now AI orchestration.
