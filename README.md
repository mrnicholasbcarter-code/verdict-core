# Verdict Core — Deterministic AI Orchestration Control Plane

[![Build Status](https://github.com/mrnicholasbcarter-code/verdict-core/workflows/CI/badge.svg)](https://github.com/mrnicholasbcarter-code/verdict-core/actions)
[![Coverage](https://img.shields.io/codecov/c/github/mrnicholasbcarter-code/verdict-core)](https://codecov.io/gh/mrnicholasbcarter-code/verdict-core)
[![Python Version](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Mypy](https://img.shields.io/badge/mypy-strict-brightgreen)](https://mypy-lang.org/)
[![Pre-commit](https://img.shields.io/badge/pre--commit-enabled-brightgreen)](https://pre-commit.com/)

> **Production-ready deterministic AI orchestration.** Route requests to the right model, enforce policies, verify outcomes — all with formal guarantees.

## The Problem

AI orchestration today is **non-deterministic, expensive, and unverifiable**:

| Pain Point | Reality |
|------------|---------|
| **Cost** | Teams burn \$100k+/month on Opus/GPT-4 when 90% of tasks need Haiku/GPT-4o-mini |
| **Reliability** | No guarantees a model won't hallucinate, leak PII, or exceed budget |
| **Governance** | Can't prove *why* a model was chosen or *what* it was allowed to do |
| **Portability** | Locked into one provider; switching costs are prohibitive |

## The Solution

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

## Quick Start

```bash
# Install
pip install verdict-core

# Run the credential-free demo
python -m verdict demo

# Expected output:
# Verdict credential-free quickstart
# ===================================
# Task: Add structured output to the invoice parser
# Required capabilities: structured_output, tools
# Selected route: demo/frontier-tools
# Excluded candidates: 3
# Status: PASS
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

## Architecture

### Core Contracts (ADR Trail)
- **ADR-021**: Deterministic Provider Receipts
- **ADR-020**: Cross-Repo Compatibility Gate
- **ADR-019**: Node Envelope Enforcement
- **ADR-018**: Context Provider Conformance
- **ADR-017**: SwarmSpec Governance

### Ecosystem Repos
| Repo | Role | Status |
|------|------|--------|
| `verdict-core` | Control plane (this repo) | ✅ Active |
| `verdict-node` | TypeScript adapter | ✅ Active |
| `verdict-ecosystem` | Cross-repo coordination | ✅ Active |
| `verdict-risk` | Risk evaluation provider | 🚧 In progress |
| `verdict-strategy` | Strategy evaluation provider | 🚧 In progress |
| `verdict-backtest` | Backtest provider | 🚧 In progress |
| `verdict-cockpit` | UI dashboard | 🚧 In progress |

## Verification

```bash
# Run all tests
pytest -x -q

# Lint
ruff check .

# Typecheck
mypy verdict --strict

# Format
ruff format .

# Build package
python -m build
```

## Documentation

- [Architecture Overview](docs/architecture.md)
- [ADR Index](docs/adr/README.md)
- [API Reference](docs/api.md)
- [Deployment Guide](docs/deployment.md)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, testing, and PR process.

## License

MIT License — see [LICENSE](LICENSE) for details.

---

**Built by** [Nicholas Carter](https://github.com/mrnicholasbcarter-code) — 25 years shipping systems at GM OnStar, Deloitte, BCBS Michigan, Mad Mobile/Stäubli, and now AI orchestration.
