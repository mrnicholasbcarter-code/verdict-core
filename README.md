# llm-gate

> **Intelligent, Self-Optimizing LLM Router with Deterministic Safety Gates**

[![CI Status](https://img.shields.io/github/actions/workflow/status/llm-gate-ecosystem/llm-gate/ci.yml?style=flat-square&label=CI)](https://github.com/llm-gate-ecosystem/llm-gate/actions)
[![PyPI version](https://img.shields.io/pypi/v/llm-gate?style=flat-square&color=blue)](https://pypi.org/project/llm-gate/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=flat-square)](https://opensource.org/licenses/MIT)

`llm-gate` is a lightweight, framework-agnostic LLM gateway and routing proxy. It mathematically redirects routine formatting and boilerplate queries to right-sized low-cost or free models (saving up to 60%+ in API costs) while keeping your most expensive frontier reasoning models (like Claude Opus, GPT-5) strictly reserved for high-cognitive tasks.

Unlike naive routing switches, `llm-gate` enforces **hard deterministic gates** (allow/deny lists, token count limits, privacy/redaction, and capability checks) *before* dynamic routing recommenders are consulted.

---

## 🚀 Key Features

* 🧠 **Reasoning-Aware Routing**: Maps agent tasks into Sol (frontier), Terra (medium), and Luna (fast/free) tiers dynamically.
* 🛡️ **Deterministic Safety Floor**: Hard allowance checks run locally. In the event of backend recommender failures, the gate fails open/closed safely based on configuration profiles.
* 🔒 **Data Privacy & Masking**: Strictly enforces redaction, preventing API key leaks or proprietary contexts from leaving your bounds.
* ⚡ **Ultra-Low Latency Proxy**: Transparent OpenAI-compatible streaming proxy `/v1/chat/completions` with zero middleware buffer.
* 🔄 **SONA Feedback Loop**: Telemetry outcomes (latency, success, cost) are automatically reported back to Ruflo/RuVector to continuously train the MoE ranking model.

---

## 📦 Directory & Ecosystem Layout

* [**Architecture & Routing Policy**](docs/architecture.md): The core structure of orchestrator-driven routing, capability filtering, and safety floors.
* [**Intelligence Adapter Protocol**](docs/intelligence-adapter-protocol.md): Subprocess endpoints and payload contracts for `ruflo` and `ruvector` hooks.
* [**Integration Guide**](docs/integration-guide.md): Set up instructions for Cursor, VSCode, LiteLLM, and TypeScript.
* [**Release Acceptance Checklists**](docs/release-checklists.md): Core metrics, coverage gates, and static analysis checks.

---

## 🛠️ Quick Start

### 1. Install llm-gate
Run the quick installation in your Python environment:
```bash
pip install llm-gate
```

### 2. Configure Settings
Store your policy profile settings dynamically inside your workspace environment:
```bash
export LLMGATE_INTELLIGENCE_MODE=production
export LLMGATE_RUFLO_COMMAND=ruflo
export LLMGATE_RUVECTOR_COMMAND=ruvector
```

### 3. Start the Local Server
```bash
llm-gate start --host 127.0.0.1 --port 20128
```

---

## 🤝 Repositories in this Portfolio

* [**llm-gate-node**](https://github.com/llm-gate-ecosystem/llm-gate-node): TS/Node Express middleware with SSE parity.
* [**llm-gate-risk**](https://github.com/llm-gate-ecosystem/llm-gate-risk): Serialized transactions and strict latency/risk gates.
* [**llm-gate-cockpit**](https://github.com/llm-gate-ecosystem/llm-gate-cockpit): Orderbook react dashboard and live agent stream watcher.
* [**llm-gate-strategy**](https://github.com/llm-gate-ecosystem/llm-gate-strategy): Rule evaluators and out-of-sample lookahead metrics.
* [**llm-gate-backtest**](https://github.com/llm-gate-ecosystem/llm-gate-backtest): Tick replay simulators and Monte Carlo equity paths.
