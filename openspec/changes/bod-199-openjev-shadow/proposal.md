# Proposal: OpenJev System-One DecisionSignalProvider (SHADOW mode)

## Why

Verdict currently makes frontier planning and delegation decisions using only hard structural gates. OpenJev System-One offers fast, typed decision signals (complexity, ambiguity, decomposability, frontier_worthy, security_sensitive) that can provide trustworthy evidence for these decisions while preserving Verdict's deterministic authority.

**KNOWN**: BOD-199 (Linear issue 121f1ddf) acceptance criteria require `/v1/systemone` endpoint (not chat), typed versioned schemas, SHADOW-only mode, probability/confidence/usage persistence in receipts, provider unavailable -> UNKNOWN (never block), structured evidence outranks OpenJev, and hermetic fixtures.

**KNOWN**: BOD-198 established NormalizedFailureClass (QUOTA, RATE_LIMIT, OVERLOADED, etc.), normalize_failure with now= parameter, parse_retry_after bounds, and AdapterFailureSignal/NormalizedFailure contracts.

**INFERRED**: DecisionSignalProvider protocol must never raise; failures return as signals with failure_class set and signals=None.

**NOT AVAILABLE**: Production OpenJev latency/accuracy data (requires OPENJEV_API_KEY and live calibration owned by BOD-203).

## What Changes

- Add `verdict/decision_signals/contracts.py` with `DecisionSignalSetV1` (typed schema), `DecisionSignalProvider` Protocol (never raises)
- Add `verdict/decision_signals/openjev.py`: OpenJevSystemOneProvider that POSTs to `/v1/systemone`, validates typed response, maps failures with existing normalize_failure
- SHADOW integration at ONE decision point (frontier planning in run.py): call provider, emit `decision_signals` event to EventLog with mode="SHADOW" and actual_decision, carry into receipt as optional list
- Feature flag `VERDICT_DECISION_SIGNALS_MODE` (default OFF; allowed OFF|SHADOW)
- Hermetic fixtures (confident, uncertain, malformed, timeout, quota, rate-limit, overload, key missing)
- JSON Schema file under `schemas/` with parity test
- Documentation: `docs/decision-signals/openjev.md` and `scripts/smoke_openjev.py`

## Capabilities

### New Capabilities
- `decision-signals/contracts`: Typed, versioned decision signal abstraction with DecisionSignalProvider protocol (never raises)
- `decision-signals/openjev`: OpenJev System-One adapter with injectable transport, existing normalize_failure mapping, missing credentials → UNKNOWN
- `receipts/decision-signals`: EventLog and Receipt extension for SHADOW-mode signal observation with ZERO behavioral change

### Modified Capabilities
<!-- No existing capability requirements are changing -->

## Acceptance criteria

- [ ] OpenJev adapter calls `/v1/systemone` directly (test asserts endpoint, not /v1/chat/completions)
- [ ] Typed question/answer schemas are versioned and validated
- [ ] DecisionSignalSetV1 from_dict/to_dict strict: rejects unknown fields, NaN/inf, probabilities outside [0,1], wrong schema_version, missing required fields
- [ ] DecisionSignalProvider protocol defined: signals(question, *, now) -> DecisionSignalSetV1, NEVER raises
- [ ] OpenJev unavailable/key missing → DecisionSignalSetV1 with failure_class=UNKNOWN, signals=None, never exception or global block
- [ ] Structured HTTP/runtime evidence outranks OpenJev for failure classification (test: HTTP 429 quota + OpenJev disagrees → QUOTA wins)
- [ ] Unit fixtures cover confident, uncertain, malformed JSON, schema violation, timeout, 429 quota, 429 rate-limit with Retry-After, 529 overload, key missing
- [ ] SHADOW mode only: `decision_signals` event with mode="SHADOW", signals recorded next to actual_decision (e.g. OpenJev frontier_worthy=0.08 vs actual frontier_invoked=true), ZERO behavioral effect
- [ ] Test: SHADOW mode with frontier_worthy=0.0 vs 1.0 vs provider failure → identical Verdict decision, only receipt differs
- [ ] Feature flag VERDICT_DECISION_SIGNALS_MODE (default OFF; allowed OFF|SHADOW; invalid → OFF with warning)
- [ ] JSON Schema at schemas/decision-signals-v1.json with parity test
- [ ] New tests pass, full suite 0 failed, mypy --strict verdict (count > 222)
- [ ] ruff check . (exit 0), ruff format --check . (exit 0)
- [ ] `verdict openspec admit bod-199-openjev-shadow` (exit 0)
- [ ] `scripts/smoke_openjev.py` exits 2 without OPENJEV_API_KEY, exits 3 on unimplemented live steps, never exits 0 without real call

## Impact

Affected modules:
- `verdict/orchestration/run.py` (frontier planning decision integration point, ~line 172)
- `verdict/orchestration/receipt.py` (optional decision_signals list field)
- Reuses existing: `verdict/gateway_adapters.py` (NormalizedFailureClass), `verdict/autodev_routing.py` (normalize_failure, parse_retry_after)

New files:
- `verdict/decision_signals/__init__.py`
- `verdict/decision_signals/contracts.py`
- `verdict/decision_signals/openjev.py`
- `schemas/decision-signals-v1.json`
- `tests/fixtures/openjev/` (fixture directory)
- `tests/test_intelligence_decision_signals.py`
- `tests/test_intelligence_openjev.py`
- `tests/test_shadow_integration.py`
- `docs/decision-signals/openjev.md`
- `scripts/smoke_openjev.py`

Not affected:
- Hard admission policy (Guardrail: no OpenJev signal may bypass BOD-104 eligibility authority)
- Calibration/promotion to ADVISORY/BOUNDED_SKIP (BOD-203 owns)
- Any existing Verdict decision logic (SHADOW mode guarantees identical behavior)
