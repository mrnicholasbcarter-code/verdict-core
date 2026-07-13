# llm-gate — Technical Specification
## v0.1.0 — July 2026 (historical library profile)

> This document describes the original library-only release. The current
> framework-agnostic proxy target is specified in
> [`docs/specs/PRODUCT_SPEC_V0.2.md`](specs/PRODUCT_SPEC_V0.2.md), with its
> release gates in [`docs/specs/RELEASE_ACCEPTANCE.md`](specs/RELEASE_ACCEPTANCE.md).

---

## 1. Overview

llm-gate is a zero-dependency Python library that routes LLM tasks to the most cost-effective model based on task criticality. It solves the problem of developers paying premium model prices for trivial work while sometimes under-serving critical code paths.

### Core Invariant

> **Critical code NEVER touches a cheap model. Cheap work NEVER touches an expensive model. If routing fails, work proceeds on the primary model.**

---

## 2. Architecture

```
                          ┌──────────────────────┐
                          │    gate.route(task)   │
                          └──────────┬───────────┘
                                     │
                          ┌──────────▼───────────┐
                          │   Keyword Scanner     │
                          │   (escalation check)  │
                          └──────────┬───────────┘
                                     │
                          ┌──────────▼───────────┐
                          │   Tier Resolution     │
                          │   input × escalation  │
                          │   → effective tier     │
                          └──────────┬───────────┘
                                     │
                     ┌───────────────┴───────────────┐
                     │                               │
              ┌──────▼──────┐                 ┌──────▼──────┐
              │  T0: CRIT   │                 │ T1-T3: GATE │
              │  → primary  │                 │  → offload  │
              │  (always)   │                 │  candidate   │
              └──────┬──────┘                 └──────┬──────┘
                     │                               │
                     │                    ┌──────────▼──────────┐
                     │                    │  Model Discovery    │
                     │                    │  (cached /v1/models │
                     │                    │   per provider)     │
                     │                    └──────────┬──────────┘
                     │                               │
                     │                    ┌──────────▼──────────┐
                     │                    │  Capability Match   │
                     │                    │  (tier ≤ model cap) │
                     │                    └──────────┬──────────┘
                     │                               │
                     │                    ┌──────────▼──────────┐
                     │                    │  Headroom Check     │
                     │                    │  (quota remaining?) │
                     │                    └──────────┬──────────┘
                     │                               │
                     │                    ┌──────────▼──────────┐
                     │                    │  Select cheapest    │
                     │                    │  adequate model     │
                     │                    └──────────┬──────────┘
                     │                               │
                     │                 ┌─────────────┴────────┐
                     │                 │                      │
                     │          ┌──────▼──────┐       ┌───────▼──────┐
                     │          │  Available  │       │  Exhausted   │
                     │          │  → offload  │       │  → primary   │
                     │          │  model      │       │  (fail-open) │
                     │          └──────┬──────┘       └───────┬──────┘
                     │                 │                      │
                     ▼                 ▼                      ▼
              ┌──────────────────────────────────────────────────┐
              │              RoutingDecision returned            │
              │              + JSONL log entry written           │
              └──────────────────────────────────────────────────┘
```

---

## 3. Data Model

### 3.1 Tiers

| Tier | Name | Int | Behavior | Example Tasks |
|------|------|-----|----------|---------------|
| T0 | CRITICAL | 0 | **Never offload.** Always uses `primary_model`. | Payment processing, auth/security review, production deploys, database migrations |
| T1 | HIGH | 1 | Offload only to high-capability models. | Architecture decisions, complex refactoring, security audits, API design |
| T2 | MEDIUM | 2 | Offload to mid-capability models. Default tier. | Feature implementation, test writing, code review, debugging |
| T3 | LOW | 3 | Offload to cheapest available model. | Formatting, type hints, docstrings, log summarization, boilerplate |

### 3.2 RoutingDecision

```python
@dataclass(frozen=True)
class RoutingDecision:
    model: str              # fully-qualified model ID (e.g., "anthropic/claude-sonnet-4")
    provider: str           # provider name from config
    tier: int               # effective tier (0-3)
    reason: str             # human-readable routing explanation
    alternatives: list[str] # other models considered but not chosen
    headroom_pct: float     # remaining quota % for chosen model (-1 if unknown)
    latency_ms: float       # time to compute routing decision
    escalated: bool         # was criticality bumped by keyword scanner?
    escalation_reason: str | None  # which pattern triggered escalation
    logged: bool            # was this decision written to the log?
```

### 3.3 ModelInfo

```python
@dataclass(frozen=True)
class ModelInfo:
    id: str                 # model ID from /v1/models
    provider: str           # provider name
    capability_tier: int    # auto-classified: 0 (strongest) to 3 (weakest)
    context_window: int     # max tokens if available, else -1
    is_available: bool      # passed headroom check
```

### 3.4 ProviderConfig

```python
@dataclass
class ProviderConfig:
    base_url: str                          # e.g., "https://api.anthropic.com/v1"
    api_key: str | None = None             # direct key
    api_key_env: str | None = None         # env var name to read key from
    models_endpoint: str = "/models"       # override for non-standard APIs
    headroom_endpoint: str | None = None   # optional quota/usage endpoint
    priority: int = 0                      # higher = preferred at same tier
```

---

## 4. Keyword Escalation

The keyword scanner runs regex patterns against the task string. If a pattern matches, the effective criticality is bumped UP (never down) to the pattern's `min_tier`.

### Default Escalation Patterns

```python
DEFAULT_ESCALATION_PATTERNS = [
    # T0 — never offload
    EscalationPattern(
        pattern=r"(payment|billing|charge|refund|stripe|invoice|subscription)",
        min_tier=0,
        label="money-path",
    ),
    EscalationPattern(
        pattern=r"(live.?order|place.?order|execute.?trade|real.?money|production.?deploy)",
        min_tier=0,
        label="live-execution",
    ),
    # T1 — high capability required
    EscalationPattern(
        pattern=r"(auth|login|token|session|password|jwt|oauth|credential|secret)",
        min_tier=1,
        label="auth-security",
    ),
    EscalationPattern(
        pattern=r"(migrat|schema|alter.?table|foreign.?key|index|constraint)",
        min_tier=1,
        label="data-migration",
    ),
    EscalationPattern(
        pattern=r"(security|vulnerab|injection|xss|csrf|sanitiz|escap)",
        min_tier=1,
        label="security",
    ),
    EscalationPattern(
        pattern=r"(architect|system.?design|infrastructure|scaling|distributed)",
        min_tier=1,
        label="architecture",
    ),
]
```

Users can override or extend these in the YAML config.

---

## 5. Model Discovery & Classification

### 5.1 Discovery

On first use (and every `discovery_ttl` seconds thereafter), Gate calls `GET {base_url}/models` for each configured provider. The response is expected to be OpenAI-compatible:

```json
{
  "data": [
    {"id": "claude-sonnet-4-20250514", "object": "model", ...},
    {"id": "claude-haiku-3.5", "object": "model", ...}
  ]
}
```

Models are cached in-memory. Cache is invalidated after `discovery_ttl` seconds (default: 60).

### 5.2 Auto-Classification

Models are classified into capability tiers by ID pattern matching:

```python
CAPABILITY_PATTERNS = {
    0: [r"opus", r"gpt-5\.5", r"grok-4", r"o3-pro"],
    1: [r"sonnet-4", r"gpt-5\.4", r"gpt-4o(?!-mini)", r"grok-3", r"claude-3\.5-sonnet"],
    2: [r"sonnet-3", r"gpt-4o-mini", r"haiku-3\.5", r"llama.*70b", r"qwen.*72b", r"mistral-large"],
    3: [r"haiku", r"flash", r"mini", r"8b", r"7b", r"nano", r"lite", r"instant"],
}
```

A model's capability tier determines the LOWEST criticality tier it can serve. A tier-3 model (e.g., `gemini-flash`) can only serve T3 (LOW) tasks. A tier-0 model can serve anything.

Users can override classification per model in config.

### 5.3 Headroom Check

If a provider exposes quota/usage data (e.g., via response headers or a dedicated endpoint), Gate checks remaining capacity before routing. A model with <5% remaining headroom is marked `is_available = False`.

If no quota data is available, the model is assumed available (fail-open).

---

## 6. Routing Algorithm

```python
def route(task: str, criticality: str = "medium", context: dict | None = None) -> RoutingDecision:
    tier = CRITICALITY_TO_TIER[criticality]  # "critical"→0, "high"→1, "medium"→2, "low"→3

    # 1. Keyword escalation: bump tier UP if task matches escalation patterns
    for pattern in escalation_patterns:
        if pattern.matches(task):
            tier = min(tier, pattern.min_tier)
            escalation_reason = pattern.label
            break

    # 2. If T0 (critical): always use primary model, skip offload
    if tier == 0:
        return RoutingDecision(model=primary_model, tier=0, reason="critical — never offload")

    # 3. Discover and filter models
    candidates = []
    for provider in providers:
        for model in discover_models(provider):
            if model.capability_tier <= tier and model.is_available:
                candidates.append(model)

    # 4. Sort: prefer cheapest adequate model (highest capability_tier number first),
    #    then by provider priority, then alphabetical for determinism
    candidates.sort(key=lambda m: (-m.capability_tier, -m.provider.priority, m.id))

    # 5. Select first available
    if candidates:
        chosen = candidates[0]
        return RoutingDecision(model=chosen.id, provider=chosen.provider, tier=tier, ...)

    # 6. Fail-open: no offload model available, use primary
    return RoutingDecision(model=primary_model, tier=tier, reason="fail-open — no offload capacity")
```

---

## 7. Decision Logging

Every call to `gate.route()` appends one JSONL row to `log_path`:

```json
{
    "ts": "2026-07-11T18:30:00.123Z",
    "task_hash": "sha256_first8",
    "task_preview": "first 120 chars...",
    "task_len": 450,
    "input_criticality": "medium",
    "effective_tier": 1,
    "escalated": true,
    "escalation_reason": "auth-security",
    "model_chosen": "anthropic/claude-sonnet-4",
    "provider": "anthropic",
    "tier": 1,
    "alternatives_considered": ["groq/llama-3.3-70b-versatile", "google/gemini-2.5-flash"],
    "headroom_pct": 72.5,
    "latency_ms": 8.2,
    "reason": "escalated to high (auth keywords); best available high-tier model"
}
```

### Log Consumers

The log is designed for three use cases:

1. **Cost analysis** — aggregate by tier/model to see spend distribution
2. **Router training** — pair `(task_hash, model_chosen)` with downstream quality scores to train a contextual ML router
3. **Debugging** — understand why a specific task was routed to a specific model

The `task_hash` is a truncated SHA-256 of the full task text, enabling join with downstream quality data without storing full prompts in logs by default. Set `log_full_task=True` to include the complete task.

---

## 8. Configuration

### 8.1 YAML Config

```yaml
# llm-gate.yaml
primary_model: "anthropic/claude-sonnet-4"
discovery_ttl: 60
log_path: "llm-gate-decisions.jsonl"
log_full_task: false

providers:
  anthropic:
    base_url: "https://api.anthropic.com/v1"
    api_key_env: "ANTHROPIC_API_KEY"
    priority: 10
  groq:
    base_url: "https://api.groq.com/openai/v1"
    api_key_env: "GROQ_API_KEY"
    priority: 5
  ollama:
    base_url: "http://localhost:11434/v1"
    priority: 1

# Override auto-classification for specific models
model_overrides:
  "groq/llama-3.3-70b-versatile":
    capability_tier: 1  # treat as high-capability despite auto-tier

# Custom escalation patterns (merged with defaults)
escalation_patterns:
  - pattern: "(kubernetes|helm|terraform|infrastructure)"
    min_tier: 1
    label: "infra"

# Tier preferences (optional — overrides cheapest-first sort)
tiers:
  critical:
    never_offload: true
  high:
    prefer_models: ["claude-sonnet-4", "gpt-4o"]
  medium:
    prefer_models: ["claude-haiku-3.5", "gpt-4o-mini", "llama-3.3-70b"]
  low:
    prefer_models: ["gemini-flash", "llama-3.1-8b"]
```

### 8.2 Programmatic Config

```python
from llm_gate import Gate, ProviderConfig

gate = Gate(
    primary_model="anthropic/claude-sonnet-4",
    providers={
        "groq": ProviderConfig(
            base_url="https://api.groq.com/openai/v1",
            api_key_env="GROQ_API_KEY",
        ),
    },
)
```

### 8.3 Environment Variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `LLM_GATE_CONFIG` | Path to YAML config file | `./llm-gate.yaml` |
| `LLM_GATE_PRIMARY_MODEL` | Override primary model | (from config) |
| `LLM_GATE_LOG_PATH` | Override log file path | `./llm-gate-decisions.jsonl` |
| `LLM_GATE_LOG_FULL_TASK` | Log full task text | `false` |
| `LLM_GATE_DISCOVERY_TTL` | Model discovery cache TTL (seconds) | `60` |

---

## 9. CLI

```bash
# Route a task
llm-gate route "Review this auth module" --criticality high
# Output: model=anthropic/claude-sonnet-4 tier=1 reason="high tier, best available"

# List discovered models with tiers
llm-gate models
# Output:
# PROVIDER    MODEL                        TIER  AVAILABLE
# anthropic   claude-sonnet-4              1     ✓ (82%)
# groq        llama-3.3-70b-versatile      2     ✓ (100%)
# ollama      qwen2.5-coder:7b             3     ✓ (local)

# Analyze routing decisions
llm-gate stats --last 7d
# Output:
# TIER      COUNT   AVG LATENCY   TOP MODEL
# critical  12      2.1ms         anthropic/claude-sonnet-4
# high      45      8.3ms         anthropic/claude-sonnet-4
# medium    189     5.7ms         groq/llama-3.3-70b
# low       423     3.2ms         ollama/qwen2.5-coder:7b
# ESTIMATED SAVINGS: $142.30 (vs all-primary routing)

# Validate config
llm-gate check
```

---

## 10. Package Structure

```
llm-gate/
├── llm_gate/
│   ├── __init__.py          # public API: Gate, RoutingDecision, ProviderConfig
│   ├── gate.py              # Gate class — main entry point
│   ├── router.py            # routing algorithm (tier resolution, model selection)
│   ├── discovery.py         # model discovery from /v1/models endpoints
│   ├── classifier.py        # capability auto-classification by model ID
│   ├── escalation.py        # keyword scanner and escalation patterns
│   ├── headroom.py          # quota/headroom checks per provider
│   ├── logger.py            # JSONL decision logging
│   ├── config.py            # YAML/dict config loader
│   ├── models.py            # dataclasses: RoutingDecision, ModelInfo, ProviderConfig
│   ├── cli.py               # CLI entry point
│   └── py.typed             # PEP 561 marker
├── tests/
│   ├── test_gate.py         # integration tests
│   ├── test_router.py       # routing algorithm unit tests
│   ├── test_classifier.py   # auto-classification tests
│   ├── test_escalation.py   # keyword escalation tests
│   ├── test_discovery.py    # model discovery tests (mocked HTTP)
│   ├── test_headroom.py     # headroom check tests
│   ├── test_logger.py       # logging tests
│   ├── test_config.py       # config loader tests
│   └── test_cli.py          # CLI tests
├── docs/
│   ├── SPEC.md              # this file
│   └── logo.svg             # project logo
├── examples/
│   ├── basic.py             # minimal usage
│   ├── multi_provider.py    # multi-provider setup
│   └── with_logging.py      # decision logging and analysis
├── README.md
├── LICENSE
├── CONTRIBUTING.md
├── pyproject.toml
└── .gitignore
```

---

## 11. Non-Goals (v0.1 historical profile)

These are explicitly out of scope for the initial release:

- **Prompt proxying in v0.1.** The original library did not send prompts to models. The
  current alpha has a separate OpenAI-compatible proxy slice; it is not yet a production
  drop-in replacement and is governed by the v0.2 acceptance matrix.
- **ML-based routing.** The default router is deterministic (tier + keywords). The decision log enables training an ML router, but that's a future module.
- **Token counting.** No tokenizer dependency. Context window checks use the model's advertised limit if available.
- **Response quality scoring.** The log records decisions, not outcomes. Quality scoring is the caller's responsibility.
- **Streaming in v0.1.** The original library had no streaming path. The current alpha
  transport passes arbitrary upstream byte chunks through, with broader compatibility gates
  still open.

---

## 12. Future Roadmap

| Version | Feature |
|---------|---------|
| 0.2 | Async `gate.aroute()` for async applications |
| 0.3 | Built-in cost estimation per model (price-per-token registry) |
| 0.4 | Embedding-based task classifier (optional dependency on sentence-transformers) |
| 0.5 | A/B routing: send N% of tasks to an alternative model and compare quality |
| 1.0 | Stable API, full docs site, PyPI release |

---

## 13. Origin

This library was extracted from a production event-driven algorithmic trading system where routing LLM tasks across 15+ providers reduced API costs by ~60% while maintaining code quality for critical money-path operations. The original system (closed-source) has been routing thousands of decisions daily since early 2026.
