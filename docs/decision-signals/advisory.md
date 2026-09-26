# OpenJev ADVISORY Mode

ADVISORY is an opt-in extension of SHADOW mode that may **reorder** the
admitted candidate set before a routing pick is made. It never adds, restores,
or drops a candidate. All hard gate outcomes (capability, budget, privacy,
capacity, availability, policy) are fixed before advisory runs.

## What it does

When enabled, every `verdict route` / `/v1/route` call:

1. Collects a `DecisionSignalSetV1` from the configured OpenJev provider
   (with a configurable timeout, default 1500 ms).
2. Classifies the task as one of three **profiles**:
   - **economy** – `frontier_worthy < 0.4` AND `complexity < 0.4`:
     prefers the cheapest admitted model. Cost is derived from `ModelInfo.pricing`
     (`input + output` cost per 1k tokens) when present; falls back to `cost_per_1k`,
     then `capability_tier` as a proxy (higher tier = cheaper/weaker). Tier is always
     the secondary tiebreak. Both thresholds and price weights are uncalibrated .
   - **strength** – `frontier_worthy >= 0.6` OR `complexity >= 0.6`:
     prefers the strongest (highest quality_confidence, lowest tier number)
     admitted model.
   - **inconclusive** – neither condition met: no change to order.
3. Records the influence in `RoutingDecision.safety_flags`:
   - `advisory:<profile>` (e.g. `advisory:economy`)
   - `advisory_baseline:<model_id>` (first candidate before advisory)
   - `advisory:skipped:<reason>` when advisory is bypassed.
4. If the first advisory-ordered candidate differs from the baseline pick,
   it becomes the selected model.

## How to opt in

Set the environment variable:

```
VERDICT_DECISION_SIGNALS_MODE=ADVISORY
```

And supply an OpenJev provider (via `factory.provider_from_env()` once
the OpenJev provider (real Codiv API shapes) lands, or inject any `DecisionSignalProvider`-compatible object as
`IntelligenceService(decision_signal_provider=...)`.

## Hard skip conditions (fail open to baseline)

Advisory is skipped — and the baseline order is used unchanged — when:

| Condition | Recorded flag |
|-----------|---------------|
| Task is critical-tier (`criticality=critical`) | Not called |
| Task privacy is `restricted` or `trusted_upstream` | `advisory:skipped:privacy_restricted` |
| Signal collection times out | `advisory:skipped:timeout` |
| Provider raises an exception | `advisory:skipped:error` |
| Signals have `failure_class` set | `advisory:skipped:failure_class_set` |
| Signal `confidence < VERDICT_ADVISORY_MIN_CONFIDENCE` | `advisory:skipped:low_confidence` |
| Signals are empty / None | `advisory:skipped:no_signals` |
| Mode is OFF or SHADOW | Not called |
| No provider configured | Not called |
| `ExecutionPathDecision` is present | Not called (returns before advisory) |

## Cut-off values (uncalibrated — decision-signal calibration)

| Variable | Default | Meaning |
|----------|---------|---------|
| `VERDICT_ADVISORY_MIN_CONFIDENCE` | `0.6` | Skip if signal confidence below this |
| `VERDICT_DECISION_SIGNALS_TIMEOUT_MS` | `1500` | Timeout for provider call (ms) |

The economy/strength thresholds (0.4 / 0.6) are fixed initial values.
Calibration is tracked in decision-signal calibration.

## Data sent per call

In ADVISORY mode, a scrubbed task summary (first 500 chars of `task_str`)
plus a `complexity_hints: {}` dict are sent to the OpenJev provider as a
`DecisionQuestionV1`. No credentials, user PII, or model internals are
included. See PRIVACY_POLICY.md for the full privacy notice (owned by the OpenJev provider (real Codiv API shapes)).

## Live-admit path (OmniRoute)

When a route call goes through the live-admit path (`_offload_free_tier`), the
admitted set contains string IDs, not `ModelInfo` objects, so advisory reordering
cannot apply pricing or tier data. In ADVISORY mode, the decision's
`safety_flags` records `advisory:skipped:admit_path_not_supported` so monitoring
can see that advisory was skipped on this path, not missing.

This is NOT a silent gap — it is a documented limitation. Full advisory support
on the live-admit path is a follow-up (BOD-TBD).

## How to turn it off

```
VERDICT_DECISION_SIGNALS_MODE=OFF  # (default)
```

or simply unset the variable. SHADOW mode collects signals for observability
without any reordering.

## Orchestration

Orchestration (`verdict orchestrate`) stays SHADOW-only. Planning requires a
frontier model; advisory reordering at that stage would contradict the
frontier requirement.

## Determinism

The same inputs (candidates, signals, cut-offs) always produce the same
advisory order. Signal set identity is bound by `input_digest` (SHA-256 of
the canonical `DecisionQuestionV1`). The decision kernel's `decision_id`
binds admitted **membership**, not order, so advisory-on and advisory-off
produce a byte-identical `decision_id` (FR-005 / decision_kernel.py).

## Mutation test proof

Each behavioral claim in `tests/test_decision_signals_advisory.py` carries
a `#MUTATION_PROOF` comment. The membership invariant test runs through the
real `decision_kernel.decide()` path with an injected `AdvisoryRanker`.

RESULT: PASS
