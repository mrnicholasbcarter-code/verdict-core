# OpenJev System-One Decision Signals (SHADOW mode)

## Overview

OpenJev System-One provides fast, typed decision signals for Verdict intelligence decisions. BOD-199 introduces SHADOW-only integration: signals are recorded in EventLog and receipts with ZERO behavioral change.

## SHADOW Mode

**SHADOW** means:
- OpenJev provider is called at frontier planning decision
- Signals (complexity, ambiguity, frontier_worthy, etc.) are recorded in EventLog and receipt
- Signals have **ZERO effect** on Verdict decision logic
- Verdict behavior is IDENTICAL with provider confident/uncertain/failing/unavailable

SHADOW mode exists for calibration and observation. Promotion to ADVISORY or BOUNDED_SKIP is owned by BOD-203.

## Feature Flag

Environment variable: `VERDICT_DECISION_SIGNALS_MODE`

- Default: `"OFF"` (no provider calls, no events)
- Allowed: `"OFF"`, `"SHADOW"`
- Invalid value → treated as `"OFF"` with warning

## Configuration

For SHADOW mode, set:

```bash
export VERDICT_DECISION_SIGNALS_MODE=SHADOW
export OPENJEV_BASE_URL=https://api.openjev.dev
export OPENJEV_API_KEY=<your-key>
```

Missing `OPENJEV_BASE_URL` or `OPENJEV_API_KEY` → provider returns `failure_class=UNKNOWN`, never blocks.

## Rollback

Immediate rollback:

```bash
export VERDICT_DECISION_SIGNALS_MODE=OFF
```

or unset the variable. No provider calls, no EventLog events, no receipt extension.

## What BOD-203 Owns

BOD-203 (calibration and promotion) owns:

- Calibration: comparing OpenJev signals to actual Verdict outcomes
- Thresholds: determining when signals are trustworthy enough to influence decisions
- Promotion to ADVISORY mode: signals can inform (but not override) hard gates
- Promotion to BOUNDED_SKIP mode: signals can skip certain checks within safety bounds
- Benchmarks: measuring OpenJev accuracy, latency, and cost

BOD-199 scope: SHADOW collection only.

## Operator Requirements

For live SHADOW run:

1. Obtain OpenJev API key from https://openjev.dev or your internal deployment
2. Set `OPENJEV_BASE_URL` (e.g., `https://api.openjev.dev`)
3. Set `OPENJEV_API_KEY`
4. Set `VERDICT_DECISION_SIGNALS_MODE=SHADOW`
5. Run Verdict normally
6. Inspect EventLog events (`type="decision_signals"`) and receipt `decision_signals` field

## Decision Signals

OpenJev returns probabilities [0,1] for:

- **complexity**: Task complexity
- **decomposability**: Whether task can be broken into subtasks
- **ambiguity**: Requirement ambiguity
- **frontier_worthy**: Whether frontier model is needed
- **security_sensitive**: Security/privacy sensitivity
- **verification_strength**: Recommended verification rigor
- **context_need**: Context/hydration needs

Plus:
- **confidence**: Provider confidence in signals [0,1]
- **latency_ms**: Signal generation latency
- **usage**: Token counts

## Failure Handling

OpenJev failures are classified with existing `NormalizedFailureClass`:

- Missing credentials → `UNKNOWN` (never blocks)
- Timeout → `TIMEOUT`
- HTTP 429 quota → `QUOTA`
- HTTP 429 rate-limit → `RATE_LIMIT` (with cooldown from Retry-After)
- HTTP 529 → `OVERLOADED`
- Malformed response → `INVALID_REQUEST`
- Network error → `TRANSPORT`

Structured HTTP/runtime evidence outranks OpenJev signals for failure classification.

## Receipt Extension

When mode=SHADOW, receipts gain optional field:

```python
decision_signals: list[dict[str, Any]] | None
```

Each entry is a `DecisionSignalSetV1.to_dict()` with:

- `schema_version`: `"decision-signals/v1"`
- `provider`, `model`, `version`, `request_id`
- `purpose`: Decision purpose (e.g., `"frontier_planning"`)
- `signals`: Dict of probabilities (or `None` if provider failed)
- `confidence`: Provider confidence [0,1]
- `latency_ms`, `usage`
- `input_digest`: SHA256 of question
- `observed_at`: ISO8601 timestamp
- `failure_class`: Failure class if provider failed (or `None`)
- `mode`: `"SHADOW"`

## EventLog Event

EventLog event `type="decision_signals"`:

```json
{
  "seq": 42,
  "at": "2026-09-25T10:00:00Z",
  "type": "decision_signals",
  "node_id": "",
  "data": {
    "mode": "SHADOW",
    "signals": { /* DecisionSignalSetV1.to_dict() */ },
    "actual_decision": {
      "decision_type": "frontier_planning",
      "outcome": "frontier_invoked",
      "selected_route": { /* ... */ }
    }
  }
}
```

## Safety

- OpenJev is experimental hosted service with no production uptime guarantee
- Signals are UNTRUSTED ADVISORY EVIDENCE, never authoritative
- Structured evidence (HTTP status, runtime) outranks OpenJev for failure classification
- No OpenJev signal may bypass BOD-104 eligibility authority
- SHADOW mode guarantees identical Verdict behavior regardless of OpenJev response

## See Also

- BOD-199: This story (SHADOW-only integration)
- BOD-203: Calibration and promotion to ADVISORY/BOUNDED_SKIP
- `scripts/smoke_openjev.py`: Smoke test (exits 2 without key, 3 for unimplemented)
