<p align="center">
  <img src="https://img.shields.io/badge/python-3.10%2B-blue?style=flat-square" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/license-MIT-green?style=flat-square" alt="MIT License">
  <a href="https://github.com/mrnicholasbcarter-code/llm-gate/actions"><img src="https://img.shields.io/github/actions/workflow/status/mrnicholasbcarter-code/llm-gate/ci.yml?style=flat-square&label=CI" alt="CI"></a>
  <a href="https://pypi.org/project/llm-gate/"><img src="https://img.shields.io/pypi/v/llm-gate?style=flat-square" alt="PyPI"></a>
</p>

<h1 align="center">llm-gate</h1>
<p align="center"><b>Policy-safe, availability-aware LLM routing and workflow orchestration.</b></p>

---

**llm-gate** is a Python library and local OpenAI-compatible proxy that first understands what a task requires, then selects among models and workflows that are actually capable, healthy, policy-compliant, and usable now. It combines deterministic safety gates with live provider availability, bounded adaptive advice, verification, and explainable decisions. Criticality remains a safety input—not the routing algorithm.

```python
from llm_gate import Gate

gate = Gate()
decision = gate.route("Rewrite the auth module", criticality="high")  # compatibility input
print(decision.model)   # anthropic/claude-sonnet-4-20250514
print(decision.reason)  # includes requirements, eligibility, and verification evidence
```

## Why llm-gate?

| Problem | llm-gate solution |
|---|---|
| Accidentally routing sensitive code to weak models | Deterministic policy floor plus capability, privacy, and live-availability gates |
| Manually switching API keys between providers | Auto-detection of local servers, CLI tools, API keys, and routers |
| No visibility into routing decisions | JSONL decision logging, analytics CLI, and Streamlit dashboard |
| Vendor lock-in | OpenAI-compatible proxy works with any client (Cursor, Aider, Claude Code, etc.) |
| Cost blowups | Headroom monitoring and intelligent model selection |

## Quick Start

### Install

```bash
pip install llm-gate
```

Or from source with all extras:

```bash
git clone https://github.com/mrnicholasbcarter-code/llm-gate.git
cd llm-gate
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,server,ui]"
```

### Setup Wizard

The interactive setup wizard auto-detects your local providers and walks you through configuration:

```bash
llm-gate setup
```

```
┌─ llm-gate Provider Detection ─┐
│ Scanning for local servers,    │
│ CLIs, API keys, and routers... │
└────────────────────────────────┘
 ✓ Ollama detected at localhost:11434 (3 models)
 ✓ OPENAI_API_KEY found
 ✓ ANTHROPIC_API_KEY found

 Primary model: anthropic/claude-sonnet-4-20250514
 Add a provider: openai
   Base URL: https://api.openai.com/v1
   API key env var: OPENAI_API_KEY
 Add a provider: done

 ✓ Configuration saved to llm-gate.yaml
```

### Route a Task

```bash
llm-gate route "Fix the SQL injection in user_auth.py" --criticality high
```

```
┌─ Routing Decision ─────────────────────────┐
│ Model:    anthropic/claude-sonnet-4-20250514│
│ Status:   eligible                          │
│ Reason:   Capability + health + quota fit   │
│ Verify:   required                           │
└─────────────────────────────────────────────┘
```

### Run as a Proxy Server

Production server startup requires a caller bearer token (or a Unix socket). Bind the public/container example explicitly and provide a token:

```bash
export LLMGATE_AUTH_TOKEN='change-this-to-a-long-random-token'
export LLMGATE_HOST=127.0.0.1
export LLMGATE_UPSTREAM_BASE_URL=https://api.openai.com/v1
export OPENAI_API_KEY=sk-...
llm-gate serve --host 127.0.0.1 --port 8000
```

For an explicitly anonymous development server, use only loopback:

```bash
LLMGATE_ALLOW_ANONYMOUS=true llm-gate serve --host 127.0.0.1 --port 8000
```

Then point your tools at `http://localhost:8000/v1`:

```bash
# Works with curl
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-4o", "messages": [{"role": "user", "content": "Hello"}]}'

# Works with any OpenAI SDK client
export OPENAI_BASE_URL=http://localhost:8000/v1
```

## CLI Commands

| Command | Description |
|---|---|
| `llm-gate setup` | Interactive setup wizard with auto-detection |
| `llm-gate route <task>` | Route a single prompt with explanation |
| `llm-gate serve` | Launch OpenAI-compatible proxy server |
| `llm-gate detect` | Scan for available LLM providers |
| `llm-gate stats` | View routing analytics from decision logs |
| `llm-gate suggest` | Get evidence-backed optimization suggestions |
| `llm-gate ui` | Launch Streamlit analytics dashboard |

## Proxy Features

- **`POST /v1/chat/completions`** with streaming support and model rewriting
- **`GET /v1/models`** with local allow/deny filtering via `LLMGATE_MODEL_ALLOWLIST` / `LLMGATE_MODEL_DENYLIST`
- **`POST /v1/route`** explain-only routing decisions without forwarding
- **`GET /health`** and upstream-aware **`GET /ready`**
- Request-size enforcement via `LLMGATE_MAX_REQUEST_BYTES`
- Server-owned upstream auth (client keys are never forwarded)
- Unknown fields, tools, response-format, and usage preserved transparently

## How Routing Works

```
Request → Planner → TaskSpec → Hard Gates → Eligible Candidates → Adaptive Route
                                      │                              │
                              policy, privacy,              model/workflow choice
                              capability, health,             among eligible options
                              quota, budget, risk
                                      │                              │
                                      └────────→ Execute → Verify → Learn
```

1. **Planning** determines objective, effort, required capabilities, tools, workflow shape, risk, budget, and verification.
2. **Hard gates** reject unsafe, incompatible, stale, unhealthy, locked-out, or quota-exhausted candidates.
3. **Adaptive selection** ranks only eligible candidates using task fit, quality, cost, latency, reliability, and bounded learned evidence.
4. **Execution and verification** run the selected model/workflow, validate outcomes, and record redacted evidence for future improvement.

## Configuration

Create `llm-gate.yaml` (or use `llm-gate setup`):

```yaml
primary_model: anthropic/claude-sonnet-4-20250514
log_path: decisions.jsonl

providers:
  anthropic:
    base_url: https://api.anthropic.com/v1
    api_key_env: ANTHROPIC_API_KEY
  openai:
    base_url: https://api.openai.com/v1
    api_key_env: OPENAI_API_KEY
  ollama:
    base_url: http://localhost:11434/v1
```

## Integrations

llm-gate works as a transparent proxy with any OpenAI-compatible client:

- **[Cursor / VS Code](docs/integrations/cursor-vscode.md)** - Set base URL in settings
- **[Aider](docs/integrations/aider.md)** - `--openai-api-base http://localhost:8000/v1`
- **[Claude Code](docs/integrations/claude-code-hook.md)** - Hook-based integration
- **[LiteLLM](docs/integrations/litellm.md)** - Use as upstream proxy
- **[OpenHands](docs/integrations/openhands.md)** - Environment variable config
- **[Any OpenAI SDK](docs/integrations/universal-agnostic.md)** - Just change the base URL

See [docs/integrations/](docs/integrations/) for the full list.

## Intelligence and Suggestions

After accumulating routing decisions, llm-gate can mine your history for optimization insights:

```bash
llm-gate suggest
```

```
┌─ llm-gate Intelligence Suggestions ─┐

 High Latency Routing Detected (SUG-LAT-001)
 Category: Performance | Expires In: 7d
 Over 10 requests routed to slow-model with latency > 2500ms.
 Proposed: Evaluate adding a lightweight T3 provider.
 Confidence: 92.0% | Impact: High

───
```

Suggestions are read-only, never mutate policy automatically, and require explicit approval before any action.

## Development

```bash
# Run tests
pytest tests/ -v

# Lint and format
ruff check . --fix && ruff format .

# Type check
mypy llm_gate --strict

# Run benchmarks
python benchmarks/test_throughput.py
```

## Docker

```bash
docker build -t llm-gate .
docker run -p 8000:8000 -e LLMGATE_UPSTREAM_BASE_URL=http://host:20132/v1 llm-gate
```

## Architecture

```
llm_gate/
├── gate.py              # Core routing engine
├── classifier.py        # Criticality classification
├── router.py            # Model selection logic
├── catalog.py           # OmniRoute model catalog
├── api.py               # FastAPI proxy server
├── proxy.py             # Upstream HTTP transport
├── intelligence.py      # Intelligence service adapter
├── suggestions.py       # Evidence-backed optimization suggestions
├── provider_detection.py # Auto-detect local providers
├── cli.py               # CLI entry point
├── dashboard.py         # Streamlit analytics UI
├── logger.py            # JSONL decision logging
├── models.py            # Data models
├── escalation.py        # Tier escalation logic
├── headroom.py          # Rate limit monitoring
└── neural.py            # Neural scoring (experimental)
```

## License

[MIT](LICENSE)
