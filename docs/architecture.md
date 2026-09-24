# Architecture Overview

> **Shipped behavior (BOD-180).** This page describes the live control-plane path.
> Design-only specifications under `docs/architecture/*_SPECIFICATION.md` are
> **not** additional shipped behaviour unless linked from an ADR marked CURRENT.

## High-level architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            VERDICT CORE                                      │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐                  │
│  │    GATE      │───▶│ ELIGIBILITY  │───▶│ INTELLIGENCE │                  │
│  │  (compose)   │    │   (hard)     │    │  (advisory)  │                  │
│  └──────────────┘    └──────────────┘    └──────────────┘                  │
│        │                    │                    │                          │
│        ▼                    ▼                    ▼                          │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │                   AVAILABILITY CACHE (SWR)                            │  │
│  │  TTL + stale-window, explicit unknown/error, isolation by             │  │
│  │  provider/model/policy-version, explain_freshness() for /explain      │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│                              │                                              │
│                              ▼                                              │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │  SERVE PATH / EXECUTION PATH (BOD-104) → DISPATCHER → PROXY           │  │
│  │  OmniRoute (optional): inventory / execute / health only              │  │
│  │  Core metadata (models.dev + LiteLLM): capability SoT                 │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

**Invariant:** hard eligibility runs before advisory ranking. A model excluded by
a gate cannot be restored by a score, similarity signal, or cost heuristic.

## Core components

### 1. Gate (`verdict/gate.py`)

Composes eligibility + intelligence and exposes the routing entry points:

```python
class Gate:
    def route(self, task: TaskSpec, ...) -> RoutingDecision: ...
    def route_with_strategy(self, task: TaskSpec, ...) -> RoutingDecision: ...
```

`Gate` does **not** expose a `check()` API. Policy floors are applied through
`EligibilityGate.evaluate` and related passport/capability gates on the serve path.

### 2. [Eligibility Gate (`verdict/eligibility.py`)](../verdict/eligibility.py)

Availability-aware hard filtering with explicit unknown handling:

```python
class EligibilityGate:
    def evaluate(
        self,
        models: list[ModelInfo],
        *,
        protected: bool = False,
        dev_mode: bool = False,
    ) -> list[EligibilityRecord]: ...
```

**Key invariant:** unknown / error availability is never treated as healthy for
protected work when fail-closed mode is enabled.

### 3. [Intelligence Service (`verdict/intelligence.py`)](../verdict/intelligence.py)

**Advisory ranking only** — cannot bypass hard gates. Orders already-eligible
candidates using historical MemoryPlane signals and expected-value estimates.

Canonical durable memory is MemoryPlane (`verdict/memory_*`). Ruflo/swarm
learning is not part of Core after BOD-17.

### 4. Availability Cache (`verdict/availability_cache.py`)

Bounded SWR cache (issue #56):

- **Cache key**: provider + model + policy_version
- **TTL**: configurable (default 60s)
- **Stale window**: configurable (default 30s) — serve stale, trigger async refresh
- **Explicit states**: fresh, stale, unknown, error, refreshing
- **Explain endpoint**: `GET /v1/route/explain` surfaces freshness + eligibility explain records

### 5. Serve path / execution path (`verdict/serve_path.py`, BOD-104)

On the API serve path, BOD-104 execution-path authority is required. Demoted
legacy selectors may still feed candidate sets, but they are not routing
authority when `require_execution_path_authority` is set (BOD-127 cutover).

### 6. Dispatcher (`verdict/dispatcher.py`)

Binds an authorized `selected_route`, hydrates the execution plan, and emits
assignment explanation. `SwarmDispatcher` is a legacy class name for the
authorize-only dispatcher after BOD-17. It is not Ruflo swarm supervision and
does not supervise a swarm.

### 7. [OmniRoute transport (`verdict/omniroute.py`)](../verdict/omniroute.py)

Optional HTTP transport for catalog inventory, execute, and health evidence.
The live catalog has thousands of models across dozens of providers (live count
via `verdict eligibility`); this document does not freeze a provider or
free-tier count.


- HTTP catalog/runtime only
- **Not** capability metadata SoT
- **Not** WebSocket/RTK/auto-fallback product surface inside Verdict

Harness adapters (Claude Code, Codex, Cursor, …) talk to Verdict. OmniRoute on
`:20128` is optional upstream transport behind Verdict — never the harness base URL.

### 8. Core metadata store (`verdict/metadata/`)

Independently fetched model caps from models.dev (primary) and LiteLLM
(secondary). Each field carries `source` + `version|fetched_at`. Soft ranks are
never invented. See [`guides/model-metadata-store.md`](guides/model-metadata-store.md)
and [ADR-032](adr/ADR-032-core-model-metadata-store.md).

---


## Orchestration layer (ADR-036)

[ADR-036](adr/ADR-036-goal-to-receipt-orchestration.md) defines the shipped
`verdict.orchestration` control plane. It owns a run from a goal through a
validated DAG, bounded execution and independent review to a durable receipt.
The [interview golden path](guides/interview-golden-path.md) documents the
operator flow.

```
verdict/orchestration/
  contracts -> planner -> eligibility -> runtime -> executors -> review -> receipt
                     |          |          |            |
                 recovery   supervisor    tui          cli
```

Implementation roles are split as follows:

- `contracts` defines the run, work-graph, node, and receipt boundaries.
- `planner` builds and validates the frontier work graph.
- `eligibility` applies the `DISCOVERED -> ENTITLED -> HEALTHY -> AVAILABLE ->
  TASK_ELIGIBLE -> SELECTED` ladder.
- `runtime` advances the DAG; `executors` perform bounded worker attempts.
- `recovery` handles same-node retry and cooldown-aware recovery.
- `review` performs the independent review gate before completion.
- `receipt` persists the run result; `supervisor` watches and resumes a
  controller; `tui` renders live state; `cli` provides the command handlers.

The Prime Agent is the execution harness, not the orchestration authority.
OmniRoute supplies inventory and transport only. The legacy [ADR-023](adr/ADR-023-governed-swarm-supervision.md)
is superseded in orchestration scope; ADR-036 is current.

## Implementation references

These are the live replacements for the retired architecture-page links:

- Eligibility gate: [`verdict/eligibility.py`](../verdict/eligibility.py) and
  [ADR-010](adr/ADR-010-fail-closed-capability-passports.md).
- Intelligence service: [`verdict/intelligence.py`](../verdict/intelligence.py).
- Proxy layer: [`verdict/proxy.py`](../verdict/proxy.py) and
  [ADR-035](adr/ADR-035-authorized-selected-route-dispatch.md).
- Telemetry and adapter boundary: [`verdict/gateway_adapter_runtime.py`](../verdict/gateway_adapter_runtime.py)
  and [ADR-020](adr/ADR-020-gateway-adapter-contracts.md).

## Data flow

### Request routing

```
Client Request
     │
     ▼
┌─────────────────────────────────────────────────────────────────┐
│ POST /v1/route  (or OpenAI-compatible /v1/chat/completions)     │
└─────────────────────────────────────────────────────────────────┘
     │
     ▼
┌─────────────────────────────────────────────────────────────────┐
│ GATE.route / route_with_strategy                                │
│ - Build TaskSpec                                                │
│ - Compose eligibility + intelligence                            │
└─────────────────────────────────────────────────────────────────┘
     │
     ▼
┌─────────────────────────────────────────────────────────────────┐
│ ELIGIBILITY.evaluate                                            │
│ - AvailabilityCache report                                      │
│ - Partition: eligible / unknown / ineligible (+ named reasons)  │
│ - Protected work: unknown/error fail closed                     │
└─────────────────────────────────────────────────────────────────┘
     │
     ▼
┌─────────────────────────────────────────────────────────────────┐
│ INTELLIGENCE.rank (advisory)                                    │
│ - Orders kept candidates only                                   │
│ - Cannot restore hard-excluded models                           │
└─────────────────────────────────────────────────────────────────┘
     │
     ▼
┌─────────────────────────────────────────────────────────────────┐
│ SERVE PATH / EXECUTION PATH (BOD-104) → DISPATCHER              │
│ - Authorize selected_route                                      │
│ - Hydrate ContextPack / tools plan                              │
│ - Empty eligible set → blocked (no silent frontier fallback)    │
└─────────────────────────────────────────────────────────────────┘
     │
     ▼
┌─────────────────────────────────────────────────────────────────┐
│ PROXY: forward to upstream (OpenAI-compatible)                  │
│ - Bind concrete identity                                        │
│ - Stream / return response                                      │
└─────────────────────────────────────────────────────────────────┘
```

---

## Explainability (`/v1/route/explain`)

Returns freshness + eligibility explain records from
`AvailabilityCache.explain` and `EligibilityGate` explain surfaces. Exact JSON
shape is versioned with the API; treat the OpenAPI schema and live
`/v1/route/explain` response as authoritative over any pasted example.

---

## Contracts (`verdict/contracts.py`)

Versioned, strict dataclass contracts for all public APIs. The canonical
machine-readable definition is [schemas/contracts.v1.json](../schemas/contracts.v1.json)
and the checked-in fixture is validated by:

```bash
uv run python scripts/validate_contract_schema.py
```

The v1 boundary rejects missing or blank objectives, wrong JSON types, secret
bearing fields, unknown fields, negative/non-finite budget and latency values,
unknown safety enums, and workflow steps whose actions are not in the safe v1
action vocabulary. `schema_version` defaults to `"1"` for Python callers that
omit it; any declared version other than `"1"` is rejected.

| Contract | Purpose |
|----------|---------|
| `TaskSpec` | Normalized task input (versioned) |
| `RoutingDecisionContract` | Gate → Eligibility → Intelligence output |
| `AvailabilitySnapshot` | Cache entry with metadata |
| `OutcomeEpisode` | Feedback / outcome episode record |
| `LearningEvent` | **Removed / superseded (BOD-17)** — was Ruflo learning; use MemoryPlane |
| `VerificationPlan` | Post-deployment verification |

---

## Security

- **API key redaction**: keys stripped from logs/telemetry
- **PII masking**: common secret/PII patterns redacted
- **Private host blocking**: RFC1918, localhost, metadata endpoints blocked by default
- **Fail-closed**: unknown availability is ineligible for protected work

---

## Performance

No CI-backed latency SLA is currently claimed for Gate/Eligibility/Intelligence.
Treat any historical p50/p99 tables as **unmeasured targets**, not shipped proof.
Use [`proof/EVIDENCE_INDEX.md`](proof/EVIDENCE_INDEX.md) for what is actually certified.

---

## Related

- [ADR index (lifecycle-classified)](adr/README.md)
- [ADR-004 Local-first MemoryPlane](adr/ADR-004-local-first-memory-plane.md)
- [ADR-010 Fail-closed capability passports](adr/ADR-010-fail-closed-capability-passports.md)
- [ADR-032 Core model metadata store](adr/ADR-032-core-model-metadata-store.md)
- [ADR-023 Governed swarm supervision](adr/ADR-023-governed-swarm-supervision.md) — **SUPERSEDED** by [ADR-035](adr/ADR-035-authorized-selected-route-dispatch.md)
- [ADR-035 Authorized selected-route dispatch](adr/ADR-035-authorized-selected-route-dispatch.md) — BOD-104 / BOD-67 post-swarm path
- [ADR Orchestrator Routing](adr/ADR-ORCHESTRATOR-ROUTING.md) — **SUPERSEDED** (BOD-17 / BOD-127 → ADR-035)
- Cross-repo evidence: [verdict-ecosystem ADR_LIFECYCLE (BOD-169)](https://github.com/mrnicholasbcarter-code/verdict-ecosystem/blob/main/docs/ADR_LIFECYCLE.md)
- [Unknown ≠ healthy](guides/unknown-not-healthy.md)
- [Free-tier admit smoke](guides/free-tier-admit-smoke.md)
- [Model metadata store](guides/model-metadata-store.md)
- Design-only specs under [`architecture/`](architecture/) — label as planned unless an ADR marks them CURRENT
