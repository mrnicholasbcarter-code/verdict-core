# Enforcement & Learning Policy

**Status:** Active
**Authority:** Governs how Verdict enforces routing decisions and how the
learning loop closes. The former LLM-gate name is retained only for legacy
compatibility.
**Related ADR:** `ADR-ORCHESTRATOR-ROUTING.md`
**Related Policy:** `ROUTING_POLICY.md`

---

## 1. Enforcement Boundary (What the Gate Guarantees)

| Layer | Guarantee | Mechanism |
|-------|-----------|-----------|
| **Protected work** | Only `ready` models admitted; fail-closed on `unknown`/`error`/missing | `EligibilityGate(protected_fail_closed=True)` |
| **Non-protected work** | `eligible`/`ready`/`degraded` admitted; dev-mode allows unverified | `allow_unverified_in_dev=True` |
| **Pre-ranking filter** | No excluded candidate can re-enter via ranker/plan/outcome | Invariant: single `EligibilityGate` authority |
| **Structured explain** | `/v1/route/explain` surfaces admitted set, exclusions with reasons, cache confidence | Same gate data, no hidden state |

**The gate is the ONLY enforcement point.** Downstream (ranker, dispatcher, swarm, Ruflo) must treat the gate's eligible set as immutable.

---

## 2. Learning Loop (How the System Gets Smarter)

### 2.1 The Loop

```
orchestrator (frontier model)
    │
    ├─► research task → review live catalog (/v1/models) → pick model per slice
    │
    ├─► spec work → dispatch to workers (ruflo swarm / agent team / AgentSDK)
    │
    ├─► verify worker output → green/red
    │
    └─► record_outcome(model_id, task_class, success, cost, latency)
            │
            ▼
ruflo hooks_model-outcome  (subprocess CLI / MCP)
            │
            ▼
SONA pattern distillation → ReasoningBank pattern store
            │
            ▼
intelligence-route (Q-learning) reads patterns → better tier/combo/harness picks next time
```

### 2.2 What Gets Recorded

Every worker completion → `record_outcome` called with:

```python
record_outcome(
    model_id="openrouter/openai/gpt-4o",
    task_class="codegen:refactor",
    success=True,
    cost_usd=0.0012,
    latency_ms=847,
)
```

This is a **thin subprocess call** to `ruflo hooks_model-outcome` (same pattern as `intelligence.py._probe_managed_backend` — no internal Ruflo imports).

### 2.3 What the Learner Improves

The neural/SONA loop improves **orchestrator selection decisions**, not the gate:

| Learned Signal | Applied By |
|----------------|------------|
| "Free models fail on complex refactors" | Orchestrator picks mid-tier for `codegen:refactor` |
| "Gemini 3.1 Pro excels at long-context analysis" | Orchestrator routes `research:long-context` to it |
| "GPT-5.5 has high latency under load" | Orchestrator prefers `gpt-5.4` for latency-sensitive tasks |
| "Worker mirages: claims success but tests fail" | Orchestrator verifies more aggressively for that model |

**The gate stays deterministic.** The learning loop informs the *orchestrator's* choices, which are then *filtered* by the gate.

---

## 3. Coupling Rules (What Verdict May/Not Touch)

| Category | Allowed | Forbidden |
|----------|---------|-----------|
| **Ruflo internals** | Call `ruflo hooks_model-outcome` via subprocess | `import ruflo.neural`, `import ruvector` |
| **RuVector** | Query `brain search` / `sona patterns` via CLI | Internal HNSW/LoRA/embedding calls |
| **Selection logic** | Orchestrator owns selection; gate filters | `intelligence.route()` calling `select_best_model(tier)` |
| **Tier assignment** | Gate consumes orchestrator's candidate list | `classifier.classify(model_id)` in hot path |

---

## 4. Worker Dispatch & Verification

| Dispatch Method | When to Use | Verification Required |
|-----------------|-------------|----------------------|
| **ruflo swarm** (`swarm init` + `agent_spawn`) | Multi-file features, refactors, audits | Parent swarm agent verifies children |
| **ruflo hive-mind** | Consensus, adversarial review, security | Raft/Byzantine quorum |
| **ruflo autopilot** | Long-running keep-going loops | 270s cache-aware heartbeat; human sample-check |
| **OpenRouter AgentSDK** (`@openrouter/agent`) | Multi-turn tool loops, web research | Orchestrator verifies final artifact |
| **Claude Code subagents** | Single-file edits, quick patches | Orchestrator runs tests + lint |

**All workers** inherit:
- Same `claims`/`TDD`/lint standards as parent
- Same context (RAG + shared AgentDB namespace)
- Structured output contract: `{finding, evidence, confidence, files_touched, follow_ups[]}`

---

## 5. Negative Signal is Mandatory (Mirage Guard)

From `ruflo-intelligence` ADR-0001:

> **Trajectories: 0 rows — nothing records action→outcome.**
> **`feedback(false)` never called → confidence only drifts up → can't learn from failures/mirages.**
> **ReasoningBank: 0 rows, disconnected from hook pipeline.**

### The Fix (Wire This Before Any Learning Claims)

```python
# In worker completion path:
from verdict.learning_feedback import record_outcome

# SUCCESS
record_outcome(model_id, task_class, success=True, cost=cost, latency=latency)

# FAILURE (critical — this is the missing negative signal)
record_outcome(model_id, task_class, success=False, cost=cost, latency=latency, reason="tests-failed")
```

Only **verified** outcomes feed positive signal. Unverified "success" = `quality=0` until orchestrator confirms.

---

## 6. Verification Gates

```bash
# Before merge:
ruff check . && ruff format --check .
mypy --strict verdict
pytest -q
code-review-graph detect-changes  # routing path blast radius
# Exact-SHA CI watch (CI/Lint/CodeQL green)
```

---

## 7. Related Docs

- `ADR-ORCHESTRATOR-ROUTING.md` — orchestrator/gate boundary, learning loop wiring
- `ROUTING_POLICY.md` — gate jobs, dynamic-catalog rule, protected work floor
- `ORCHESTRATOR_DRIVEN_ROUTING.md` — full intent: zero-hardcode, orchestrator cognitive pass, SONA learning
- ruflo `intelligence-route` skill — `hooks_model-route` / `hooks_model-outcome` CLI
- ruflo `neural-train` skill — SONA distillation + ReasoningBank
- ruflo `agentdb` ADR-0001 — namespace convention (`pattern` vs `patterns` vs `claude-memories`)

---

*Policy aligned with `ADR-ORCHESTRATOR-ROUTING.md`. The gate enforces; the orchestrator selects; the neural loop learns.*
