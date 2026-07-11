<p align="center">
  <img src="https://raw.githubusercontent.com/mrnicholasbcarter-code/llm-gate/main/docs/logo.svg" width="120" alt="llm-gate logo" />
</p>

<h1 align="center">llm-gate</h1>

<p align="center">
  <strong>Route LLM tasks by criticality. Never send prod code to a cheap model.<br/>Never burn $20/hr on formatting.</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/pypi/v/llm-gate?color=blue" alt="PyPI" />
  <img src="https://img.shields.io/badge/python-3.10+-blue.svg" alt="Python Version" />
  <img src="https://img.shields.io/badge/license-MIT-green" alt="License" />
</p>

---

## ⚡ The Headless Quickstart (All OS)

Want to skip the interactive wizard and deploy `llm-gate` headless in CI/CD or directly to your server? Run this one-liner in your terminal (macOS, Linux, or WSL):

```bash
curl -sSL https://raw.githubusercontent.com/mrnicholasbcarter-code/llm-gate/main/quickstart.sh | bash
```

**Or manually for Windows/AnyOS:**
```bash
mkdir -p ~/.llm-gate && cat << 'EOF' > ~/.llm-gate/llm-gate.yaml
primary_model: "anthropic/claude-3-opus-20240229"
providers:
  anthropic:
    base_url: "https://api.anthropic.com/v1"
    api_key_env: "ANTHROPIC_API_KEY"
  groq:
    base_url: "https://api.groq.com/openai/v1"
    api_key_env: "GROQ_API_KEY"
EOF
pip install llm-gate[rich,pyyaml]
```

---

## 🌟 The CLI Dashboard

`llm-gate` ships with a breathtaking terminal UI built on `rich`. 

### `llm-gate setup`
Don't want to mess with YAML? Run the interactive wizard to map your providers.
```text
╭─────────────────────────────────────────────────────────╮
│ llm-gate Setup Wizard                                   │
│ Let's configure your routing engine.                    │
╰─────────────────────────────────────────────────────────╯
Enter your Tier 0 (Critical) primary model [anthropic/claude-3-opus-20240229]:
Let's add some offload providers (Tier 1-3).
Provider name (e.g., anthropic, groq, local_ollama): groq
Base URL for groq [https://api.anthropic.com/v1]: https://api.groq.com/openai/v1
Environment variable for API key (leave blank if none) []: GROQ_API_KEY
✔ Saved configuration to llm-gate.yaml!
```

### `llm-gate route "<prompt>"`
Visually test how a prompt is escalated and which model is selected.
```text
╭─ Routing Decision ────────────────────────────────────────╮
│ Task: Review this payment processing module for race...   │
│                                                           │
│ Decision:                                                 │
│ • Model:     anthropic/claude-3-opus-20240229             │
│ • Provider:  primary                                      │
│ • Tier:      T0                                           │
│ • Status:    ⚠ ESCALATED                                  │
│ • Latency:   12.4ms                                       │
│                                                           │
│ Reason: escalated to tier 0 (money-path); critical        │
╰───────────────────────────────────────────────────────────╯
```

### `llm-gate stats`
Analyze your true spend and routing distribution across models instantly.
```text
  Routing Distribution by Tier  
 ┏━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━━┓
 ┃        Tier        ┃   Volume ┃ % of Traffic ┃
 ┡━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━━┩
 │ T0 (Money Path / UI)      142          4.9 % │
 │ T1 (High Cap)             840         29.0 % │
 │ T2 (Logic)               1120         38.6 % │
 │ T3 (Formatting)           798         27.5 % │
 └────────────────────┴──────────┴──────────────┘
 Total Requests: 2900
 P50 Latency: 4.2ms

 Top Routed Models:
   • groq/llama-3-8192: 798 calls
   • anthropic/claude-3-sonnet: 550 calls
   • primary/claude-3-opus: 142 calls
```

---

## The Problem

You're paying for **Claude Opus** / **GPT-4o** / **Fable-5** to reformat JSON, summarize logs, and lint docstrings.

Meanwhile, your production database migrations, security reviews, and payment integrations are getting the same model as your throwaway scripts.

**Most LLM costs come from sending the wrong task to the wrong model.**

## The Solution

```python
from llm_gate import Gate

gate = Gate()  # auto-discovers models from any OpenAI-compatible endpoint

result = gate.route(
    task="Refactor this auth module to use JWT refresh tokens",
    criticality="high",  # or: "critical", "medium", "low"
)

print(result.model)     # → "claude-3-sonnet"
print(result.provider)  # → "anthropic"
print(result.reason)    # → "high criticality + auth/security keywords → tier 2"
```

```python
# Critical path — NEVER offloads to a cheap model
result = gate.route(
    task="Review this payment processing function for edge cases",
    criticality="critical",
)
# result.model → your most capable tier-0 model, always

# Bulk work — sends to the cheapest model with capacity
result = gate.route(
    task="Add type hints to these 50 utility functions",
    criticality="low",
)
# result.model → "llama-3-8b" (free tier, plenty of capacity)
```

## Features

- **Learned Routing & Orchestration** — Neural routing that analyzes historical task success to predict the optimal model for complex review and research workflows.
- **4 Criticality Tiers** — `critical` / `high` / `medium` / `low`. Money-path code never leaves your best model.
- **Auto-discovery** — Queries any OpenAI-compatible `/v1/models` endpoint dynamically.
- **Capability Classification** — Auto-tiers models by ID pattern (`opus`/`gpt-4o` → high, `flash`/`mini` → low).
- **Quota-aware** — Checks provider rate limits before routing to avoid silent queuing.
- **Keyword Escalation** — Detects `auth`, `payment`, `security`, `migration` and forcefully bumps criticality.
- **Decision Logging** — Every routing decision is JSONL logged to constantly feed the Learned Router.
- **Zero dependencies** — Core engine is pure Python. Installs in < 1 second. (CLI requires `rich`).

## Install

Base install (engine only):
```bash
pip install llm-gate
```

Full install (includes elite CLI dashboard):
```bash
pip install llm-gate[cli]
```

## How Routing Works

```
Task arrives → Keyword scan → Criticality floor applied
                                     ↓
                            Tier determined (T0-T3)
                                     ↓
                   ┌─────────────────┴─────────────────┐
                   │                                     │
              T0: CRITICAL                        T1-T3: OFFLOADABLE
              Never offload.                      Find best model at tier.
              Use primary model.                  Check quota headroom.
                   │                              Prefer cheapest adequate.
                   │                                     │
                   │                              ┌──────┴──────┐
                   │                              │             │
                   │                          Available?   Exhausted?
                   │                              │             │
                   │                         Use offload   Fail open →
                   │                           model      primary model
                   ↓                              ↓             ↓
                Return                         Return       Return
               Decision                       Decision     Decision
```






## 🔌 Universal Ecosystem Integration 

`llm-gate` isn't just for developers. It is the universal policy engine and criticality router for the absolute most popular apps across OpenRouter's Top verticals.

### 💻 Top Coding Agents
*Ensure your agents never burn frontier-tokens parsing JSON or diffs:*
- **CLI Agents:** Kilo Code, pi, Poolside, Codebuff, Aider, Qwen Code, OpenCode
- **IDE Extensions:** Cursor, Zed Editor, Cline, Roo Code
- **Agentic Frameworks:** Claude Code, OpenClaw, OpenHands, Jcode, Hermes Agent

### ⚡ Top Productivity Tools
*Route daily summarization tasks to inexpensive models while keeping deep research logic on Tier-0:*
- **Browser Agents:** Web Voyager, Letaido, Clark
- **Automation/Gateways:** OmniRoute, 9router, Portkey, Peezy Gateway (p0.systems)
- **Workflows:** Notion integrations, Task orchestrators

### 🎨 Top Creative & Gaming Agents
*Preserve complex narrative reasoning while aggressively offloading repetitive dialog generation:*
- **Gaming AI:** Lemonade (Roblox AI), Studs.gg, GDevelop
- **RPGs & Narrative:** SillyTavern webhooks, NovelAI proxies, Character agent loops (Ito, Olam Labs)

*(Using an unlisted app? See our [Universal Integration Script / Proxies](docs/integrations/universal-agnostic.md) to route literally anything).*

## Philosophy

1. **Critical code never touches a cheap model.** Payment logic, auth flows, database migrations, and production deployments always go to your best model. No exceptions.
2. **Cheap work never touches an expensive model.** Formatting, linting, type hints, log summarization, and boilerplate generation go to the fastest, cheapest model with capacity.
3. **Fail open, never block.** If every offload model is rate-limited or down, the task goes to your primary model. Work never stops.

## License

MIT. See [LICENSE](LICENSE).
