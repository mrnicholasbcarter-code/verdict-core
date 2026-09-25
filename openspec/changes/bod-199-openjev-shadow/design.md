# Design

## Context

Verdict already has:
- EventLog append-only event stream (`verdict/orchestration/receipt.py`)
- Frontier planning decision in `plan_with_failover` (`verdict/orchestration/run.py` ~line 172)
- Autodev delegation decision enforcement (`verdict/autodev_run.py` line 204)
- NormalizedFailureClass enum (QUOTA, RATE_LIMIT, OVERLOADED, etc.) (`verdict/gateway_adapters.py`)
- normalize_failure protocol (`verdict/gateway_adapter_runtime.py`, `verdict/autodev_routing.py`)
- parse_retry_after with bounds (`verdict/autodev_routing.py`)
- AdapterFailureSignal/NormalizedFailure contracts
- Receipt structure for carrying execution metadata

Current gaps:
- No decision signal abstraction (complexity, ambiguity, frontier_worthy)
- No OpenJev System-One integration
- No SHADOW capability for passive signal observation

## Goals / Non-Goals

**Goals:**
- Hermetic DecisionSignalProvider protocol (never raises; failures as signals with failure_class)
- Typed, versioned DecisionSignalSetV1 with strict validation
- OpenJev adapter using existing normalize_failure infrastructure
- SHADOW-only integration: signals recorded in EventLog and receipt, ZERO behavioral change
- Injectable transport for network-free tests

**Non-Goals:**
- Any change to Verdict decision logic (BOD-203 owns promotion)
- Production calibration/thresholds
- Direct model output → provider/model selection (deterministic policy only)

## Decisions

**Decision 1: Use frontier planning decision point in run.py::plan_with_failover (~line 172)**

Rationale: Clear, single structural decision point where OpenJev signals (frontier_worthy, complexity) are most relevant. Alternative was autodev delegation decision in autodev_run.py, but frontier planning is the primary decision authority.

**Decision 2: POST to /v1/systemone endpoint (NOT /v1/chat/completions)**

Rationale: Enforces typed question/answer contract, prevents confusion with chat API, makes OpenJev usage explicit and auditable.

**Decision 3: Reuse existing normalize_failure for HTTP/runtime failures**

Rationale: Avoids duplicate failure classification logic. Existing normalize_failure already handles 429 quota → QUOTA, 429 → RATE_LIMIT + cooldown, 529 → OVERLOADED, timeout, network errors.

**Decision 4: Missing credentials → UNKNOWN failure_class, never exception**

Rationale: Codiv/OpenJev is free experimental hosted service with no production uptime guarantee. Missing credentials must never block Verdict operation. UNKNOWN signals that provider was unavailable, not that request failed.

**Decision 5: Structured evidence precedence over OpenJev signals**

Rationale: HTTP status/runtime evidence (429 → QUOTA, 529 → OVERLOADED) is authoritative. OpenJev signals are advisory; they cannot override concrete provider failure evidence.

**Decision 6: SHADOW mode with ZERO behavioral change**

Rationale: Calibration and threshold tuning belong in BOD-203. This story establishes trustworthy signal collection without operational risk. Test enforcement: provider returning frontier_worthy=0.0 vs 1.0 must produce identical Verdict decision.

## Risks / Trade-offs

**Risk: OpenJev unavailable during live run**  
Mitigation: Missing credentials or base URL → UNKNOWN failure_class, never exception or global block. Verdict operates normally without OpenJev.

**Risk: Malformed OpenJev response**  
Mitigation: Strict validation with typed errors. Malformed JSON or schema violation → failure_class set, signals=None. Never crashes Verdict.

**Risk: OpenJev signals contradict HTTP evidence**  
Mitigation: Structured evidence precedence. Test: HTTP 429 quota + OpenJev says "not quota" → QUOTA wins.

**Trade-off: Frontier planning vs autodev delegation decision point**  
Decision: Frontier planning in run.py (~line 172) is the primary decision authority. Autodev delegation can be added later if needed.

**Trade-off: SHADOW mode overhead**  
Decision: Single synchronous HTTP call at frontier planning decision. Latency budget: < 500ms typical. If OpenJev times out, failure_class=TIMEOUT and Verdict proceeds normally.

## Routing / context / memory implications

**Routing**: No change. SHADOW mode never alters route selection.

**Context**: DecisionQuestionV1 built from frontier planning context (task summary, complexity hints). No additional context hydration required.

**Memory**: EventLog event (JSON, ~1KB typical) and receipt.decision_signals list entry. Negligible for typical run volumes.

## Implementation

### Modules

**New:**
- `verdict/decision_signals/__init__.py`
- `verdict/decision_signals/contracts.py`: DecisionSignalSetV1, DecisionQuestionV1, DecisionSignalProvider protocol
- `verdict/decision_signals/openjev.py`: OpenJevSystemOneProvider with injectable transport

**Modified:**
- `verdict/orchestration/run.py`: plan_with_failover integration (SHADOW mode check, provider call, EventLog emit)
- `verdict/orchestration/receipt.py`: Add optional decision_signals: list[dict[str, Any]] | None field

**Reused:**
- `verdict/gateway_adapters.py`: NormalizedFailureClass enum
- `verdict/autodev_routing.py`: normalize_failure, parse_retry_after

### Test Strategy

**Unit tests:**
- `tests/test_intelligence_decision_signals.py`: DecisionSignalSetV1 validation (probabilities, NaN/inf, unknown fields, schema_version, JSON Schema parity)
- `tests/test_intelligence_openjev.py`: OpenJevSystemOneProvider with injectable transport (confident, uncertain, malformed, timeout, 429 quota, 429 rate-limit, 529 overload, key missing)
- `tests/test_shadow_integration.py`: SHADOW mode enforcement (frontier_worthy=0.0 vs 1.0 → identical decision, provider failure → same decision), structured evidence precedence

**Fixtures:**
- `tests/fixtures/openjev/`: confident, uncertain, malformed, schema violation, timeout, quota, rate-limit, overload, key missing

**Test requirements:**
- All OpenJev tests use injectable transport (no network)
- Every test must call production code and FAIL if code is removed/broken
- No tests that only assert on literals or fixtures the test itself built

### Configuration

**Environment variables:**
- `VERDICT_DECISION_SIGNALS_MODE`: default "OFF"; allowed "OFF"|"SHADOW"; invalid → "OFF" + warning
- `OPENJEV_BASE_URL`: optional, OpenJev endpoint
- `OPENJEV_API_KEY`: optional, OpenJev API key

Missing base URL or API key → UNKNOWN failure_class, never exception.

### Documentation

**New:**
- `docs/intelligence/openjev-shadow.md`: SHADOW mode semantics, feature flag, rollback, BOD-203 ownership, operator requirements
- `scripts/smoke_openjev.py`: No key → exit 2; unimplemented live steps → exit 3; never exit 0 without real call

**Schema:**
- `schemas/decision-signals-v1.json`: JSON Schema for DecisionSignalSetV1

## Migration / rollback

**Rollback**: Set `VERDICT_DECISION_SIGNALS_MODE=OFF` (default). No provider calls, no EventLog events, no receipt extension. Immediate.

**Forward migration**: Set `VERDICT_DECISION_SIGNALS_MODE=SHADOW` after verifying OPENJEV_BASE_URL and OPENJEV_API_KEY configured. Signals recorded, ZERO behavioral change.

**Promotion to ADVISORY/BOUNDED_SKIP**: BOD-203 owns. Requires calibration, thresholds, benchmarks.

## Security / trust implications

**Trust boundary**: OpenJev signals are UNTRUSTED ADVISORY EVIDENCE, never authoritative. Structured HTTP/runtime evidence outranks OpenJev for failure classification.

**Credential exposure**: OPENJEV_API_KEY in environment. Not logged in EventLog or receipt (only provider, model, version, request_id). Missing key → UNKNOWN, never exception.

**Injection risk**: Typed question/answer contract prevents injection. DecisionQuestionV1 built from Verdict internal state, not external input. Response validation rejects malformed/schema-violating JSON.

**Denial of service**: Timeout → failure_class=TIMEOUT, Verdict proceeds. OpenJev unavailable/slow does not block Verdict operation.

**Audit trail**: EventLog "decision_signals" event records mode="SHADOW", signals, actual_decision. Receipt.decision_signals carries signals for post-run analysis.

## Concurrency

**Synchronous call**: Single HTTP POST at frontier planning decision. Not async/parallel. Latency budget: < 500ms typical, timeout → TIMEOUT failure_class.

**EventLog safety**: EventLog already thread-safe with lock. emit("decision_signals") follows existing pattern.

**Receipt safety**: Receipt constructed in run.py, no concurrent modification. decision_signals list appended during plan_with_failover.

## ADR Impact

**New ADRs**: None (SHADOW-only scope, no policy change).

**Affected ADRs**: None (no change to existing admission policy, eligibility gates, or routing logic).

**Future ADRs (BOD-203)**: Promotion to ADVISORY/BOUNDED_SKIP will require ADR for threshold/calibration policy.
