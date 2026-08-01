<div align="center">

# Verdict

**Route LLM tasks by criticality.**

Never send prod code to a cheap model. Never burn $20/hr on formatting.

[![CI](https://github.com/mrnicholasbcarter-code/verdict-core/actions/workflows/ci.yml/badge.svg)](https://github.com/mrnicholasbcarter-code/verdict-core/actions/workflows/ci.yml)
[![Security](https://github.com/mrnicholasbcarter-code/verdict-core/actions/workflows/security.yml/badge.svg)](https://github.com/mrnicholasbcarter-code/verdict-core/actions/workflows/security.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

[Quickstart](#30-second-demo) · [How It Works](#how-it-works) · [CLI](#cli-reference) · [Architecture](#architecture) · [Docs](#documentation) · [Contributing](CONTRIBUTING.md)

</div>

---

## Why Verdict?

Every LLM router today answers the same question: *"which model should I use?"*

Verdict answers a harder one: ***"which models am I allowed to use — and can you prove it?"***

It's the difference between a recommendation engine and a **control plane**:

| | Typical router | **Verdict** |
|---|---|---|
| Model selection | Heuristic tiers, static allowlists | Orchestrator picks from the **OmniRoute catalog** — thousands of advertised models, liveness probe-verified in bounded samples |
| Safety | Best-effort fallback | **Fail-closed gate** — capability, budget, privacy, availability checks run *before* any upstream call |
| Unknown health | Assumed healthy | Explicit `unknown` / `error` states — **unknown ≠ healthy** |
| Explainability | "we picked GPT-4" | Per-candidate exclusion reasons, freshness timestamps, confidence, cache state — served at `GET /v1/route/explain` |
| Learning | None | Outcomes feed back through SONA / RuVector — advisory only, **never** bypasses the gate |
| Accountability | Logs | Durable, privacy-safe **evidence receipts** for every routing decision |

> The orchestrator (a frontier model you pay for once per unit of work) does the expensive thinking. The gate (deterministic Python, no LLM) does the enforcing. Neither can do the other's job — by design, codified in [20+ ADRs](docs/adr/).

---

## 30-Second Demo

No API keys. No config. Deterministic, offline, auditable:

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
# Universal installer (Linux/macOS — binary release, falls back to pipx)
curl -fsSL https://raw.githubusercontent.com/mrnicholasbcarter-code/verdict-core/main/install.sh | bash

# From source (uv)
git clone https://github.com/mrnicholasbcarter-code/verdict-core.git
cd verdict-core
uv sync --extra dev --extra server --extra dashboard
```

Then:

```bash
verdict setup            # interactive setup wizard (supports --dry-run --json)
verdict doctor --fix     # scan & repair config / connectivity issues
verdict detect           # discover available LLM providers on this machine
```

---

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

| Component | Role | Technology |
|---|---|---|
| **EligibilityGate** | Fail-closed deterministic checks — no LLM in the request path | Python |
| **ProbeRunner** | Consented, budgeted liveness probes | Python + httpx |
| **OmniRoute catalog** | Catalog mirror: pricing, capabilities, context windows — liveness probe-verified in bounded samples | External service (port 20128) |
| **Orchestrator** | Frontier-model selection & worker dispatch | Ruflo swarms |
| **Learning loop** | Outcome → advisory feedback | SONA + RuVector + ReasoningBank |
| **Evidence ledger** | Durable, privacy-safe routing receipts | JSONL + signed manifests |

Design decisions live in **[docs/adr/](docs/adr/)** — 20+ records covering the evidence ledger, orchestrator boundary, fail-closed capability passports, consented probes, catalog qualification, gateway adapter contracts, and more.

### TypeScript ecosystem

| Package | Description |
|---|---|
| `@verdict/node` | Express/Next.js middleware — OpenAI-compatible forwarding with SSE parity |
| `@verdict/contracts` | Canonical Zod schemas & TS types shared with Python |
| `verdict-client` | TypeScript client SDK |

Python ↔ TypeScript field-level parity is verified in CI — see [CONTRACT_PARITY.md](CONTRACT_PARITY.md).

---

## Security

- **Fail-closed everywhere**: protected work halts when fresh truth is unavailable — never silently falls back
- **Consent-gated probes**: network liveness checks require explicit `--allow-live-probe`
- **Privacy**: no PII in logs; privacy-safe receipt ledger ([THREAT_MODEL_RECEIPTS.md](docs/THREAT_MODEL_RECEIPTS.md))
- **Supply chain**: CI runs `pip-audit`, `npm audit`, and `osv-scanner` on every commit
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
