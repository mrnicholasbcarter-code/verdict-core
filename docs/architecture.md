# System Architecture & Routing Policy

`llm-gate` serves as a self-optimizing, token-efficient intelligence router. It sits as a thin proxy gateway between agent runtimes (Codex, Hermes, Claude Code) and model catalog providers (O‍mniRoute, OpenRouter), ensuring requests are routed with mathematical cost/performance efficiency.

---

## 1. Core Principles

* **Task-First Optimization**: Frontier models (running at xhigh reasoning) are reserved exclusively for complex cognitive tasks. Standard processing tasks (formatting, boilerplate additions, CLI interactions) are delegated to right-sized local/cloud free models.
* **Deterministic Enforcement & Safety Floors**: Model-routed tasks must respect strict, local policies (privacy filters, cost limits, capability thresholds, deny lists). 
* **Zero Hardcoding**: All available routing candidates, latencies, pricing, and capabilities are dynamically queried from live local/remote catalogs.
* **Closed-Loop Feedback**: Downstream results, speeds, and execution outcomes are fed back through Ruflo/RuVector SONA hooks to continuously optimize model selection.

---

## 2. Dynamic Routing Flow

```
   [ Client Request ]
           │
           ▼
[ llm-gate Proxy Gateway ] ◄─────► [ Deterministic Safety Gates ]
           │                                 │ (Redaction, Privacy, Policies)
           ▼                                 ▼
   [ Candidate List ] ◄───────────────── [ Allow / Deny Lists ]
           │
           ▼
[ O‍mniRoute Catalog API ] ◄───────► [ Availability Probes & Cache ]
           │
           ▼
   [ Selector Layer ] ◄──────────────► [ Ruflo/RuVector Routing Advisor ]
           │
           ▼
[ Target Model Dispatch ] ───────► [ Execution & Output Streaming ]
                                             │
                                             ▼
                                   [ SONA Feedback Loop ]
```

### Deterministic Safety Gates
Before any routing takes place, `llm-gate` applies hard deterministic gates locally:
1. **Allow/Deny Listing**: Rejects requests requesting blocked models.
2. **Context Window Constraint**: Ensures the target model supports the requested payload length.
3. **Capabilities Gate**: Matches task classes (e.g. `coding`, `reasoning`, `multimodal`) with the target model catalog.

### Ruflo/RuVector Managed Intelligence Routing
Once candidate filtering completes, the eligible model set is passed to the Ruflo/RuVector intelligence orchestrator via:
`ruflo hooks model-route -t <task_class> --context <context_len>`
This fetches model rankings dynamically optimized via reinforcement learning (SONA). If the adapter is unavailable, `llm-gate` falls back gracefully to a configured local safety floor model in development mode, or fails closed in production mode.

---

## 3. Availability Adapter Boundary

The `llm-gate` catalog handles local normalizations for provider endpoints. Candidates are parsed using:
* `catalog()`: normalizes O‍mniRoute `/v1/models` API responses.
* `runtime()`: checks the loopback proxy port (default `20128`) availability.
* `discover_capabilities()`: inspects tool-calling, reasoning, and context window metrics.

If a model fails a TCP connection lookup or returns transport errors, `llm-gate` quarantines the model and avoids dispatching tasks to it until the next lifecycle check.
