# Verdict — Policy-Gated LLM Routing Control Plane

[![PyPI](https://img.shields.io/pypi/v/verdict-core.svg)](https://pypi.org/project/verdict-core/)
[![Python](https://img.shields.io/pypi/pyversions/verdict-core.svg)](https://pypi.org/project/verdict-core/)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Tests](https://github.com/verdict/verdict-core/workflows/CI/badge.svg)](https://github.com/verdict/verdict-core/actions)

> **The gate rules on each task** — deterministic safety verdicts, availability-aware routing, quantitative-trading-grade execution, closed-loop telemetry.

---

## What is Verdict?

Verdict is a **policy-gated, availability-aware LLM routing control plane** — not a simple proxy. It provides:

- **Deterministic safety floors**: Hard gate checks (capability, budget, privacy, availability) run locally before any upstream call
- **Availability-aware routing**: Bounded cache with stale-while-revalidate, explicit `unknown`/`error` states, concurrent refresh deduplication
- **Explainability first**: `GET /v1/route/explain` surfaces observed_at, expires_at, age, source, confidence, candidate/eligible counts, per-candidate exclusion reasons, cache refresh/error state
- **Quantitative-trading-grade execution**: Monte Carlo backtest harness, capacity admission with deterministic effort reservations, conservative runtime headroom
- **Closed-loop telemetry**: SONA feedback loop feeds outcomes (latency, success, cost) back to RuVector for continuous MoE ranking improvement

---

## Quick Start

```bash
# Install
pipx install verdict-core

# Or with server extras
pipx install 'verdict-core[server]'

# Configure
verdict setup

# Route a task
verdict route "Refactor this Python module to use type hints" --terse
```

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        VERDICT CORE                              │
├─────────────────────────────────────────────────────────────────┤
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐              │
│  │   Gate      │  │ Eligibility │  │ Intelligence│              │
│  │  (Policy)   │──▶│  (Filter)   │──▶│  (Ranking)  │              │
│  └─────────────┘  └─────────────┘  └─────────────┘              │
│         │               │               │                        │
│         ▼               ▼               ▼                        │
│  ┌─────────────────────────────────────────────────┐             │
│  │          Availability Cache (SWR)                │             │
│  │  TTL + stale-window, explicit unknown/error,     │             │
│  │  isolation by provider/model/policy-version      │             │
│  └─────────────────────────────────────────────────┘             │
└─────────────────────────────────────────────────────────────────┘
```

### Core Components

| Module | Purpose |
|--------|---------|
| `verdict.gate` | Deterministic policy enforcement — capability, budget, privacy, capacity |
| `verdict.eligibility` | Availability-aware filtering with explicit unknown handling |
| `verdict.intelligence` | Advisory ranking (cannot bypass hard gate) |
| `verdict.availability_cache` | Bounded SWR cache, `explain_freshness()` for `/v1/route/explain` |
| `verdict.omniroute` | Native OmniRoute transport (250+ providers, 90+ free tiers) |
| `verdict.contracts` | Versioned Pydantic contracts for all public APIs |

---

## CLI Reference

```bash
verdict [global flags] <command> [args]

Commands:
  route       Route task to best model
  explain     Show eligibility ranking & freshness
  models      List/refresh available models
  policy      Manage routing policies (get/set/validate)
  dashboard   Launch/manage verdict-ui
  config      Manage local configuration
  completion  Generate shell completions
  serve       Launch FastAPI microservice
  detect      Detect available LLM providers
  probe       Run 1-token liveness probe
  suggest     Review intelligence suggestions
  doctor      Scan & repair config/connectivity
  check       Validate config syntax
```

### Route Examples

```bash
# Terse output (model name only)
verdict route "Write a Rust CLI tool" --terse
# → anthropic/claude-3-opus-20240229

# Verbose with reasoning
verdict route "Refactor this TypeScript component"
# → model: openai/gpt-4o
#    reason: capability=tools, budget=medium, latency=p50<2s
#    freshness: 12.3s old (omniroute:http)

# Production critical path
verdict route "Deploy to production" --criticality high --context '{"repo":"acme/api"}'
```

---

## Server Mode

```bash
# Start OpenAI-compatible proxy
verdict serve --host 0.0.0.0 --port 8000

# With availability cache (requires OmniRoute)
export OMNIROUTE_BASE_URL=http://localhost:20128
verdict serve
```

### Endpoints

| Endpoint | Description |
|----------|-------------|
| `POST /v1/chat/completions` | OpenAI-compatible chat completion |
| `POST /v1/route` | Route task, return selected model + reasoning |
| `GET /v1/route/explain` | Freshness + eligibility explain (issue #56/#73) |
| `GET /v1/models` | List available models with capability tags |

---

## Configuration

Verdict uses layered config:

```toml
# ~/.verdict/config.toml (global)
# .verdict/config.toml (project-local — takes precedence)

[gateway]
primary_model = "anthropic/claude-3-opus-20240229"
providers = {}

[intelligence]
profile = "balanced"  # fast | balanced | thorough
timeout_ms = 8000
allow_client_model_override = false

[availability]
ttl_seconds = 60
stale_window_seconds = 30
omniroute_base_url = "http://localhost:20128"  # Optional
```

---

## OmniRoute Integration

Verdict integrates natively with **OmniRoute** (`http://localhost:20128/v1`) as
its provider boundary for:
- **3,318+ models** across **250+ providers**
- **107+ free tiers** — no API keys needed
- Auto-fallback, RTK compression (15–95% token savings)
- `auto/best-coding`, `auto/best-reasoning`, `auto/best-fast` smart routing

```bash
# Start OmniRoute (Docker)
docker run -d -p 20128:20128 omnibus/omniroute

# Configure Verdict
export OMNIROUTE_BASE_URL=http://localhost:20128
verdict serve
```

### Runtime discovery and MCP

Verdict uses OmniRoute's OpenAI-compatible catalog for model identity, while
availability decisions remain separate: catalog presence does not mean a
provider is healthy, reachable, within quota, or eligible. When the deployment
exposes authenticated management APIs, the availability adapter can discover
documented runtime signals such as provider catalogs, MCP status/tools, and
quota summaries. Optional endpoints include:

```text
GET /v1/models
GET /api/models/catalog
GET /api/mcp/status
GET /api/mcp/tools
GET /api/free-tier/summary
GET /api/quota/pools/{pool_id}/usage
```

Management and MCP access normally require the OmniRoute bearer token. A
missing token, `401`, timeout, malformed response, or unavailable optional
endpoint is recorded as unknown/stale—not as healthy. Protected work therefore
fails closed when fresh runtime truth is absent. Verdict never reads
OmniRoute's private database and never copies provider credentials into model
selection. See [the worker/discovery guide](docs/guides/omniroute-workers.md)
and [the routing policy](docs/specs/ROUTING_POLICY.md) for the full contract.

## Autonomous development workflow

The repository's development contract is documented in
[Autonomous development](docs/guides/autonomous-development.md). It requires
documentation lookup and sanitized RAG ingestion before design, Code Review
Graph context and impact analysis before implementation/review, ticket-backed
work packages, OmniRoute-aware worker selection, layered verification, and
exact-head CI/PR follow-through through merge.

---

## Project Structure

```
verdict-core/
├── verdict/                 # Main package
│   ├── api.py              # FastAPI server + /v1/route/explain
│   ├── availability.py     # Capability/quota/health checks
│   ├── availability_cache.py  # Bounded SWR cache (issue #56)
│   ├── contracts.py        # Versioned Pydantic contracts
│   ├── dispatcher.py       # Routing logic
│   ├── eligibility.py      # Gate + filter pipeline
│   ├── gate.py             # Policy enforcement
│   ├── intelligence.py     # Advisory ranking
│   ├── omniroute.py        # OmniRoute transport
│   ├── planner.py          # Task decomposition
│   ├── cli.py              # Cobra-style CLI
│   └── ...
├── tests/                   # 320 tests passing
├── scripts/                 # flagship_demo.py, verify_release_artifacts.py
├── benchmarks/              # Reproducible benchmarks
└── docs/                    # Architecture, guides, API reference
```

---

## Ecosystem

| Repo | Purpose | Status |
|------|---------|--------|
| `verdict-core` | Python control plane (flagship) | ✅ 320 tests |
| `verdict-node` | Express/Next.js middleware | ✅ 139 tests |
| `verdict-cockpit` | Next.js dashboard | 🚧 |
| `verdict-risk` | Risk engine | 🚧 |
| `verdict-edge` | Edge mining framework | 🚧 |
| `verdict-backtest` | Monte Carlo harness | 🚧 |
| `verdict` | Umbrella/meta repo | 🚧 |

---

## Development

```bash
# Install dev deps
pipx install verdict-core --editable

# Run tests
pytest -v

# Lint + typecheck
uv run --extra dev --extra dashboard --extra server ruff check .
uv run --extra dev --extra dashboard --extra server mypy verdict --strict

# Run flagship demo
python scripts/flagship_demo.py
```

---

## License

MIT — see [LICENSE](LICENSE)

---

## Links

- **Documentation**: https://verdict.dev/docs
- **Issues**: https://github.com/verdict/verdict-core/issues
- **Discord**: https://discord.gg/verdict
- **OmniRoute**: https://github.com/verdict/omniroute
