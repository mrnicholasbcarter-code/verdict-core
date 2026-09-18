# Architecture Overview

## High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            VERDICT CORE                                      │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐                  │
│  │    GATE      │───▶│ ELIGIBILITY  │───▶│ INTELLIGENCE │                  │
│  │  (Policy)    │    │   (Filter)   │    │  (Ranking)   │                  │
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
│  │                    OMNIROUTE TRANSPORT                                 │  │
│  │  250+ providers, 90+ free tiers, auto-fallback, RTK compression       │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Core Components

### 1. Gate (`verdict/gate.py`)

**Deterministic policy enforcement** — hard safety floors that cannot be bypassed.

```python
class Gate:
    def check(self, task: TaskSpec, candidates: list[ModelInfo]) -> GateResult:
        # 1. Capability check
        # 2. Budget check  
        # 3. Privacy check
        # 4. Capacity check (headroom)
        # 5. Availability check (delegates to EligibilityGate)
```

**Checks:**
- Capability requirements (tools, vision, reasoning, structured output)
- Budget per 1k tokens
- Privacy level (standard/strict/paranoid)
- Capacity admission with deterministic effort reservations

### 2. Eligibility Gate (`verdict/eligibility.py`)

**Availability-aware filtering** with explicit unknown handling.

```python
class EligibilityGate:
    def filter(self, candidates: list[AvailabilityCandidate]) -> EligibilityResult:
        # Partition into: eligible, unknown, ineligible
        # Unknown = explicit unknown/error state (fail-closed for protected work)
        # Ineligible = failed capability/budget/privacy checks
```

**Key invariant:** Unknown models are NEVER eligible for protected work (capability_required=True).

### 3. Intelligence Service (`verdict/intelligence.py`)

**Advisory ranking** — cannot bypass hard gate. Provides:
- Historical outcome learning (Ruflo)
- Semantic similarity (RuVector)
- Expected value estimation

### 4. Availability Cache (`verdict/availability_cache.py`)

**Bounded SWR cache** (issue #56):

- **Cache key**: provider + model + policy_version
- **TTL**: configurable (default 60s)
- **Stale window**: configurable (default 30s) — serve stale, trigger async refresh
- **Explicit states**: fresh, stale, unknown, error, refreshing
- **Isolation**: provider/model/policy-version prevents cross-contamination
- **Explain endpoint**: `GET /v1/route/explain` surfaces observed_at, expires_at, age, source, confidence, candidate/eligible counts, refresh/error state

### 5. OmniRoute Transport (`verdict/omniroute.py`)

Native integration with OmniRoute gateway:
- HTTP transport with connection pooling
- WebSocket for real-time updates
- Catalog identity and liveness only — **not** capability metadata SoT

### 5b. Core metadata store (`verdict/metadata/`)

Independently fetched model caps from models.dev (primary) and LiteLLM
(secondary). Each field carries `source` + `version|fetched_at`. Soft ranks are
never invented. See [`guides/model-metadata-store.md`](guides/model-metadata-store.md)
and [ADR-032](adr/ADR-032-core-model-metadata-store.md).

---

## Data Flow

### Request Routing

```
Client Request
     │
     ▼
┌─────────────────────────────────────────────────────────────────┐
│ POST /v1/route (or /v1/chat/completions)                        │
└─────────────────────────────────────────────────────────────────┘
     │
     ▼
┌─────────────────────────────────────────────────────────────────┐
│ GATE: Build TaskSpec from request                               │
│ - Extract capability requirements                                │
│ - Extract budget from context                                    │
│ - Extract privacy level                                          │
└─────────────────────────────────────────────────────────────────┘
     │
     ▼
┌─────────────────────────────────────────────────────────────────┐
│ ELIGIBILITY: Filter candidates via AvailabilityCache            │
│ - Get cached availability report                                 │
│ - Partition: eligible / unknown / ineligible                    │
│ - Unknown models: explicit unknown state                        │
└─────────────────────────────────────────────────────────────────┘
     │
     ▼
┌─────────────────────────────────────────────────────────────────┐
│ INTELLIGENCE: Rank eligible candidates (advisory)               │
│ - Historical outcomes (Ruflo)                                    │
│ - Semantic similarity (RuVector)                                 │
│ - Expected value estimation                                      │
└─────────────────────────────────────────────────────────────────┘
     │
     ▼
┌─────────────────────────────────────────────────────────────────┐
│ DISPATCHER: Select best model, log decision                     │
│ - Apply capacity admission                                       │
│ - Emit telemetry (SONA)                                          │
└─────────────────────────────────────────────────────────────────┘
     │
     ▼
┌─────────────────────────────────────────────────────────────────┐
│ PROXY: Forward to upstream (OpenAI-compatible)                  │
│ - Rewrite model field                                            │
│ - Stream response                                                │
└─────────────────────────────────────────────────────────────────┘
```

---

## Explainability Contract (`/v1/route/explain`)

Returns per-model freshness + eligibility reasoning:

```json
{
  "policy_version": "policy-2026-07-13.1",
  "cached_models": ["openai/gpt-4o", "anthropic/claude-3-opus"],
  "cache_state": "configured",
  "gate": {
    "eligible": ["openai/gpt-4o"],
    "exclusions": {
      "anthropic/claude-3-opus": "quota exhausted"
    }
  },
  "model": {
    "model_id": "openai/gpt-4o",
    "observed_at": "2026-07-20T10:30:00Z",
    "expires_at": "2026-07-20T10:31:00Z",
    "age_seconds": 45.2,
    "source": "omniroute:http",
    "confidence": 0.92,
    "candidate_count": 3,
    "eligible_count": 2,
    "refreshing": false,
    "refresh_error": null,
    "errors": []
  }
}
```

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
action vocabulary. `metadata`, `context`, and signal payloads remain open JSON
objects for forward-compatible integration data; that openness does not relax
validation of the safety fields around them. `schema_version` defaults to
`"1"` for Python callers that omit it, but any declared version other than
`"1"` is rejected. Legacy payloads must enter through
`contract_from_legacy_dict`, which performs an explicit compatibility mapping.

The Python loader and JSON Schema are tested together with canonical valid and
invalid fixtures. The TypeScript declarations in `contracts/index.ts` are a
consumer-facing compatibility surface; full TypeScript runtime parity is
tracked separately and must not be treated as a policy bypass.

Contracts include:

| Contract | Purpose |
|----------|---------|
| `TaskSpec` | Normalized task input (versioned) |
| `RoutingDecisionContract` | Gate → Eligibility → Intelligence output |
| `AvailabilitySnapshot` | Cache entry with metadata |
| `OutcomeEpisode` | SONA feedback loop record |
| `LearningEvent` | Ruflo learning event |
| `VerificationPlan` | Post-deployment verification |

---

## Security

- **API key redaction**: All keys stripped from logs/telemetry
- **PII masking**: Email, phone, SSN, credit card patterns redacted
- **Private host blocking**: RFC1918, localhost, metadata endpoints blocked
- **Fail-closed**: Unknown availability = ineligible for protected work

---

## Performance Targets

| Operation | p50 | p99 |
|-----------|-----|-----|
| Gate check | 2 ms | 5 ms |
| Eligibility filter | 3 ms | 8 ms |
| Intelligence rank | 10 ms | 30 ms |
| Cache get (hit) | 0.5 ms | 1 ms |
| Cache get (miss + refresh) | 50 ms | 200 ms |
| Proxy forward | 5 ms | 20 ms |

---

## Related

- [Eligibility Gate Deep Dive](architecture/eligibility-gate.md)
- [Intelligence Service](architecture/intelligence-service.md)
- [Proxy Layer](architecture/proxy-layer.md)
- [Telemetry Loop](architecture/telemetry-loop.md)
