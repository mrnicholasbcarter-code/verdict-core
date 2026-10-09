# Design

## Context

Verdict already has:
- Provider/model routing through OmniRoute (`verdict/autodev_routing.py`)
- Availability states including `RATE_LIMITED` (`verdict/availability.py`)
- `NormalizedFailureClass` enum for failure classification (`verdict/gateway_adapters.py`)
- Bounded recovery with retry policy (`verdict/bounded_recovery.py`)
- Execution receipts (`verdict/orchestration/receipt.py`)
- Standard eligibility gates (`verdict/eligibility.py`)

Current gaps:
- No Codiv provider identification (no `codiv/` prefix in inventory)
- 429 maps to single `RATE_LIMIT` class (no quota vs rate-limit distinction)
- No 529 → provider overload mapping
- No Retry-After header parsing with bounds checking
- Receipts don't track intended vs executed provider/model

## Goals / Non-Goals

**Goals:**
- Enable Codiv as optional OmniRoute execution candidate
- Hermetic provider identity and failure classification (test fixtures)
- Backward-compatible receipt extensions
- Absence safety (missing key doesn't break other routes)

**Non-Goals:**
- OpenJev System One integration (BOD-199)
- Hard admission policy changes
- Live network calls in CI tests
- Mass provider migration

## Decisions

**Decision 1: Exact provider prefix matching for Codiv identity**

Rationale: Model names are not unique across providers. Same model (e.g.,
`diffusiongemma-26b-a4b-it`) exists under `nvidia/`, `bm/`, `bluesminds/`, and
potentially `codiv/`. Prefix-based matching prevents cross-provider confusion.

Alternative considered: Model name pattern matching.  
Rejected: Too fragile, prone to false positives.

**Decision 2: Extend NormalizedFailureClass rather than parallel taxonomy**

Rationale: Verdict already has `NormalizedFailureClass` enum for failure
classification. Extending it (add `QUOTA_EXHAUSTED` if missing, potentially
`PROVIDER_OVERLOAD` for 529) maintains consistency with existing recovery policy.

Alternative considered: Create Codiv-specific failure classes.  
Rejected: Creates parallel taxonomy, complicates recovery logic.

**Decision 3: Parse and clamp Retry-After header**

Rationale: Retry-After can be seconds (integer) or HTTP-date. Malicious or
misconfigured providers might send negative, huge, or garbage values. Clamping
(min 1s, max 300s) ensures bounded retry without hot loops or excessive delays.

Alternative considered: Trust header value unconditionally.  
Rejected: Security and availability risk.

**Decision 4: Backward-compatible receipt fields (optional with defaults)**

Rationale: Existing receipts don't have provider identity fields. Adding them
as optional fields with safe defaults ensures existing validation logic doesn't
break.

Alternative considered: Require fields in all new receipts.  
Rejected: Breaks backward compatibility during rollout.


## Risks / Trade-offs

**Risk: Malicious Retry-After header**  
Mitigation: Clamp to min 1s, max 300s. Prevents delay injection attacks.

**Risk: False Codiv identification**  
Mitigation: Exact provider prefix matching prevents cross-provider confusion.

**Risk: Receipt field bloat**  
Mitigation: Fields are optional, backward compatible. Only populated when relevant.

**Trade-off: Separate 429 classes vs unified**  
Decision: Separate QUOTA_EXHAUSTED and RATE_LIMITED classes for distinct recovery policies.
Accepted trade-off: Slightly more complexity in failure handling, but correct semantics.

**Trade-off: Hermetic tests vs live Codiv calls**  
Decision: Hermetic fixtures in CI, manual smoke test for live validation.
Accepted trade-off: No automated live coverage, but CI remains fast and deterministic.


## Routing / context / memory implications

Provider identity tracking does not change routing logic or context handling:
- Routes are selected by existing autodev_routing.py logic
- No new context requirements
- No memory layout changes

Receipts grow by ~100 bytes per execution (4 new optional string fields + 1 bool).
At 1M executions/month, this adds ~100MB/month to receipt storage (negligible).

Provider prefix matching is O(1) string comparison, no performance impact.

## Implementation

### Modules

**verdict/gateway_adapters.py**
- Extend `NormalizedFailureClass` with `QUOTA_EXHAUSTED` (if not present)
- Consider adding `PROVIDER_OVERLOAD` for 529 (or reuse `UPSTREAM`)

**verdict/autodev_routing.py**
- Enhance `normalize_failure` to parse 429 response body for quota vs rate-limit
- Extract Retry-After header for rate-limit case
- Map 529 to provider capacity class

**verdict/bounded_recovery.py**
- Add `parse_retry_after(header: str | None, min_sec=1, max_sec=300) -> int`
- Handle both seconds (int) and HTTP-date formats
- Clamp to bounds, return default on parse failure

**verdict/orchestration/receipt.py**
- Add optional fields to receipt dataclass or dict:
  - `intended_provider: str | None = None`
  - `intended_model: str | None = None`
  - `executed_provider: str | None = None`
  - `executed_model: str | None = None`
  - `provider_mismatch: bool = False`

**verdict/eligibility.py**
- Verify Codiv routes pass through standard gates (no code change if already correct)

### Test Strategy

**Hermetic (CI)**
- Fixture: inventory with `nvidia/diffusiongemma-26b-a4b-it` only → 0 Codiv candidates
- Fixture: inventory with `codiv/diffusiongemma-26b-a4b-it` → 1 Codiv candidate
- Fixture: 429 with quota-exhausted body → `QUOTA_EXHAUSTED` state, no retry
- Fixture: 429 with `Retry-After: 60` → `RATE_LIMITED` with 60s cooldown
- Fixture: 429 with `Retry-After: -1` → clamped to 1s
- Fixture: 429 with `Retry-After: 999999` → clamped to 300s
- Fixture: 429 with `Retry-After: garbage` → clamped to default
- Fixture: 529 → provider capacity class (not quality degradation)
- Fixture: missing CODIV_API_KEY → Codiv ineligible, other routes unaffected
- Fixture: protected work + Codiv + paid route → not forced to Codiv
- Receipt: intended vs executed identity captured correctly
- Receipt: mismatch flag set when fallback occurs

**Operational (manual)**
- Direct Codiv API call with CODIV_API_KEY (smoke test)
- OmniRoute-mediated call showing provider identity preservation
- Latency/cost/token sample collection

### Configuration

```python
# Environment variable or config
CODIV_PROVIDER_PREFIX = os.getenv("CODIV_PROVIDER_PREFIX", "codiv")
CODIV_API_KEY = os.getenv("CODIV_API_KEY")  # Optional, absence is safe
```

### Documentation

**docs/providers/codiv.md**
- What is proven hermetically (fixtures)
- What needs operator setup (CODIV_API_KEY, OmniRoute config)
- Smoke test script usage
- Example latency/cost/token data (when available)

**scripts/smoke_codiv.py**
```python
#!/usr/bin/env python3
import os
import sys

if "CODIV_API_KEY" not in os.environ:
    print("CODIV_API_KEY missing", file=sys.stderr)
    sys.exit(2)

# Smoke test logic (direct and OmniRoute calls)
```

## Migration / rollback

- No migration needed (new optional capability)
- Existing routes unaffected
- Codiv routes appear only when OmniRoute inventory includes `codiv/` prefix
- Gradual rollout: add CODIV_API_KEY and OmniRoute config when ready

## Security / trust implications

- Codiv credentials (CODIV_API_KEY) are optional
- Missing credentials → routes ineligible, no system-wide impact
- Retry-After header clamping prevents malicious delay injection
- Provider identity tracking enables audit of executed vs intended routes

## Concurrency

- No new concurrency patterns
- Existing bounded retry and cooldown logic applies to Codiv routes

## ADR Impact

No new ADRs required. This change follows existing patterns:
- Provider integration (similar to existing OpenAI, Anthropic, etc.)
- Failure classification (extends existing `NormalizedFailureClass`)
- Receipt extensions (backward-compatible field additions)
