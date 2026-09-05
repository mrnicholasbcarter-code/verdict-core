# Phase 0 Research: Eligibility Filtering

No `NEEDS CLARIFICATION` markers remain in Technical Context — `/speckit-clarify` already
resolved the three spec-level ambiguities (FR-007 policy mechanism, Eligibility Status enum,
late-transition re-check). The research below covers implementation-level decisions needed to
carry those answers into the existing codebase.

## Decision: Confidence score representation

**Decision**: Add `confidence: float` (0.0–1.0) to `EligibilityRecord`, derived from the
existing `state` field via a fixed mapping: `eligible` (including the `READY` enum alias) →
`1.0`, `degraded` → `0.5`, and `unknown`/`error`/`unavailable`/timeout states → `0.0`.
FR-003's "cannot be established with confidence" is `confidence == 0.0` combined with
`protected=True`.

**Rationale**: `eligibility.py` already computes a state string per candidate from
`AvailabilityReport`; a static mapping requires no new upstream data source and keeps the
change additive (existing `state` field is preserved).

**Alternatives considered**: Sourcing confidence directly from availability-check latency/error
rates (rejected — no such continuous signal exists today, would require a new dependency);
three-way enum without a numeric score (rejected — clarify session explicitly chose the
numeric-score option to keep partial-confidence states like `degraded` distinguishable from
outright `unknown`).

## Decision: Per-request-type flag for FR-007

**Decision**: Reuse the existing `dev_mode: bool` parameter already threaded through
`decide()` / `EligibilityGate.evaluate()` as the per-request-type flag. No new parameter is
introduced; `dev_mode` is documented as *the* mechanism callers use to declare that a request
may proceed on best-available eligibility information.

**Rationale**: `decide()` already accepts `dev_mode` and `protected` as caller-supplied,
per-call booleans — this is functionally identical to the "per-request-type flag/attribute"
the clarify session selected. Introducing a second, differently-named flag would duplicate the
concept.

**Alternatives considered**: New `allow_best_available: bool` parameter (rejected — redundant
with `dev_mode`, which already exists and is understood by existing callers/tests).

## Decision: Re-check before final selection (FR-006)

**Decision**: No new re-check call is added. `decision_kernel.decide()` calls
`EligibilityGate.evaluate()` exactly once, synchronously, as part of the same call that produces
the final admitted/chosen candidate — there is no separate later step where eligibility could
go stale between "evaluation" and "final selection." This satisfies FR-006 as clarified.

**Rationale**: Verified by reading `decision_kernel.py` (`evaluate()` call site, ~line 360-440):
the admitted set from `evaluate()` feeds directly into the same call's ranking/selection logic
with no intervening await, cache, or persistence step.

**Alternatives considered**: Adding an explicit second `evaluate()` call immediately before
final pick (rejected — redundant given the atomic single-pass call; would only matter if a
future change introduces an async gap between evaluation and selection, which is out of scope
here).
