# Tasks

## 1. DecisionSignal Foundation

- [x] 1.1 Create `verdict/decision_signals/` module directory
- [x] 1.2 Implement `verdict/decision_signals/contracts.py`:
  - [x] 1.2.1 `DecisionSignalSetV1` frozen dataclass with schema_version="decision-signals/v1"
  - [x] 1.2.2 Fields: provider, model, version, request_id, purpose, signals {complexity, decomposability, ambiguity, frontier_worthy, security_sensitive: float [0,1]; verification_strength, context_need: float [0,1]}, confidence: float [0,1], latency_ms: int, usage {input_tokens, output_tokens}, input_digest: str (sha256 hex), observed_at: str (ISO8601), failure_class: NormalizedFailureClass | None, mode: str = "SHADOW"
  - [x] 1.2.3 Strict `from_dict`/`to_dict`: reject unknown fields, NaN/inf, probabilities outside [0,1], wrong schema_version, missing required fields
  - [x] 1.2.4 Typed error for validation failures
  - [x] 1.2.5 `DecisionSignalProvider` Protocol: `signals(question: DecisionQuestionV1, *, now: datetime) -> DecisionSignalSetV1` (NEVER raises)
  - [x] 1.2.6 `DecisionQuestionV1` typed input (purpose, task_summary, complexity_hints)
- [x] 1.3 JSON Schema file `schemas/decision-signals-v1.json`
- [x] 1.4 Test: valid signal set round-trips through from_dict/to_dict
- [x] 1.5 Test: probability outside [0,1] → validation error
- [x] 1.6 Test: NaN/inf → validation error
- [x] 1.7 Test: unknown field → validation error
- [x] 1.8 Test: wrong schema_version → validation error
- [x] 1.9 Test: JSON Schema parity with Python dataclass

## 2. OpenJev System-One Adapter

- [x] 2.1 Implement `verdict/decision_signals/openjev.py`:
  - [x] 2.1.1 `OpenJevSystemOneProvider` class
  - [x] 2.1.2 Constructor: base_url, api_key (both optional), transport (callable, injectable)
  - [x] 2.1.3 `signals(question, *, now)` POSTs to `{base_url}/v1/systemone` (NOT /v1/chat/completions)
  - [x] 2.1.4 Typed question/answer validation
  - [x] 2.1.5 Map HTTP/runtime failures with existing `normalize_failure` from `verdict.autodev_routing`
  - [x] 2.1.6 429 quota → QUOTA, 429 → RATE_LIMIT + cooldown, 529 → OVERLOADED, timeout → TIMEOUT, network → TRANSPORT, malformed → INVALID_REQUEST
  - [x] 2.1.7 Key missing or base URL missing → DecisionSignalSetV1 with failure_class=UNKNOWN, signals=None
  - [x] 2.1.8 NEVER raises; all failures return as signal set with failure_class set
- [x] 2.2 Test: POST goes to `/v1/systemone` (assert endpoint in test)
- [x] 2.3 Test: confident response → valid DecisionSignalSetV1
- [x] 2.4 Test: uncertain (low confidence) → valid signal set with confidence < 0.5
- [x] 2.5 Test: malformed JSON → failure_class set, signals=None
- [x] 2.6 Test: schema violation → failure_class set, signals=None
- [x] 2.7 Test: timeout → failure_class=TIMEOUT
- [x] 2.8 Test: 429 quota exhausted → failure_class=QUOTA
- [x] 2.9 Test: 429 rate-limit with Retry-After → failure_class=RATE_LIMIT, cooldown_seconds set
- [x] 2.10 Test: 529 overload → failure_class=OVERLOADED
- [x] 2.11 Test: key missing → failure_class=UNKNOWN, signals=None, never exception
- [x] 2.12 All tests use injectable transport (no network)

## 3. SHADOW Integration

- [x] 3.1 Add `VERDICT_DECISION_SIGNALS_MODE` environment variable (default "OFF"; allowed OFF|SHADOW; else OFF + warning)
- [x] 3.2 Integrate at frontier planning decision in `verdict/orchestration/run.py::plan_with_failover`:
  - [x] 3.2.1 When mode=SHADOW: instantiate OpenJevSystemOneProvider from env (OPENJEV_BASE_URL, OPENJEV_API_KEY)
  - [x] 3.2.2 Build DecisionQuestionV1 from frontier planning context
  - [x] 3.2.3 Call provider.signals(question, now=...)
  - [x] 3.2.4 Emit EventLog event: type="decision_signals", data={mode:"SHADOW", signals: signal_set.to_dict(), actual_decision: {...}}
  - [x] 3.2.5 Carry signal set into receipt as optional `decision_signals` list field
- [x] 3.3 Test: SHADOW mode with frontier_worthy=0.0 → same Verdict decision as mode=OFF
- [x] 3.4 Test: SHADOW mode with frontier_worthy=1.0 → same Verdict decision as mode=OFF
- [x] 3.5 Test: SHADOW mode with provider failure → same Verdict decision, event contains failure_class
- [x] 3.6 Test: mode=OFF → no provider call, no decision_signals event
- [x] 3.7 Test: mode=INVALID → treated as OFF with warning

## 4. Structured Evidence Precedence

- [x] 4.1 Test: HTTP 429 quota + OpenJev says "not quota" → failure_class=QUOTA (HTTP wins)
- [x] 4.2 Test: HTTP 529 + OpenJev says "not overloaded" → failure_class=OVERLOADED (HTTP wins)

## 5. Fixtures

- [x] 5.1 Create `tests/fixtures/openjev/` directory
- [x] 5.2 Fixture: confident (frontier_worthy=0.95, confidence=0.98)
- [x] 5.3 Fixture: uncertain (frontier_worthy=0.48, confidence=0.35)
- [x] 5.4 Fixture: malformed JSON
- [x] 5.5 Fixture: schema violation (missing required field)
- [x] 5.6 Fixture: timeout (transport raises timeout exception)
- [x] 5.7 Fixture: 429 quota exhausted
- [x] 5.8 Fixture: 429 rate-limit with Retry-After: 120
- [x] 5.9 Fixture: 529 overload
- [x] 5.10 Fixture: key missing (OPENJEV_API_KEY unset)

## 6. Documentation

- [x] 6.1 Create `docs/intelligence/openjev-shadow.md` with SHADOW semantics, feature flag, rollback, BOD-203 ownership, operator requirements (shipped at docs/decision-signals/openjev-shadow.md; path corrected 2026-09-28)
- [x] 6.2 Create `scripts/smoke_openjev.py`: no key → exit 2; unimplemented → exit 3; never exit 0 without real call
- [x] 6.3 Create JSON Schema `schemas/decision-signals-v1.json` with parity test
- [x] 6.4 Verify documentation files exist at specified paths
- [x] 6.5 Test: `scripts/smoke_openjev.py` exits 2 without OPENJEV_API_KEY
- [x] 6.6 Test: `scripts/smoke_openjev.py` exits 3 for unimplemented live steps

## 7. Acceptance

- [x] 7.1 New decision_signals tests pass
- [x] 7.2 New openjev tests pass
- [x] 7.3 SHADOW integration tests pass
- [x] 7.4 Full test suite: 0 failed
- [x] 7.5 `mypy --strict verdict` (count > 222)
- [x] 7.6 `ruff check .` (exit 0)
- [x] 7.7 `ruff format --check .` (exit 0)
- [x] 7.8 `verdict openspec admit bod-199-openjev-shadow` (exit 0)
- [x] 7.9 `scripts/smoke_openjev.py` exits 2 or 3 (not 0 without key)
