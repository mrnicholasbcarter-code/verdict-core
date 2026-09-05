# Phase 1 Data Model: Eligibility Filtering

Source of truth: `verdict/eligibility.py`. This feature extends the existing dataclasses in
place — no new files.

## EligibilityVerdict (existing, `verdict/eligibility.py:28`)

`str` Enum of outcome verdicts (`admitted`, `excluded`, etc. — unchanged by this feature).

## EligibilityRecord (extended)

| Field | Type | Notes |
|---|---|---|
| `model_id` | `str` | existing |
| `provider` | `str` | existing |
| `admitted` | `bool` | existing — Eligible (`True`) / Ineligible (`False`), per spec's Key Entity |
| `verdict` | `str` | existing |
| `state` | `str` | existing — availability state string (`eligible`, `degraded`, `unknown`, `error`, `unavailable`, ...) |
| `source` | `str` | existing |
| `reason` | `str \| None` | existing, default `None` |
| `confidence` | `float` | **new** — 0.0–1.0, derived from `state` (see research.md mapping). Backs FR-003's "cannot be established with confidence." |

Validation: `confidence` MUST be in `[0.0, 1.0]`. Derivation is a pure function of `state`
(`healthy → 1.0`, `degraded → 0.5`, all other states → `0.0`), so no independent input
validation is needed beyond the existing `state` string.

## EligibilityResult (unchanged shape)

| Field | Type | Notes |
|---|---|---|
| `admitted` | `list[ModelInfo]` | existing — final admitted candidates |
| `records` | `list[EligibilityRecord]` | existing — now each record carries `confidence` |

## EligibilityGate (behavior extended, no new fields)

Constructor and `evaluate()` signature are unchanged (`availability_source`,
`protected_fail_closed`, `allow_unverified_in_dev`, `clock`; `evaluate(candidates, protected,
dev_mode, now)`).

- `protected: bool` and `dev_mode: bool` (existing, per-call caller-supplied parameters) *are*
  the "per-request-type flag/attribute" referenced by FR-007 — no new parameter is introduced.
- `evaluate()` remains a single synchronous call producing both the admitted set and each
  record's `confidence`; `decision_kernel.decide()` consumes this same call's output directly
  for final selection, satisfying FR-006's re-check-before-final-selection requirement (see
  research.md).

## State / Lifecycle

No persisted lifecycle — `EligibilityRecord`/`EligibilityResult` are computed fresh on every
`evaluate()` call from the caller-supplied `candidates` and the `availability_source` callback;
there is no cross-call caching to go stale.

## Relationships

```text
decision_kernel.decide(candidates, protected, dev_mode)
    -> EligibilityGate.evaluate(candidates, protected, dev_mode)
         -> for each ModelInfo candidate: build EligibilityRecord (admitted, state, confidence, reason)
         -> EligibilityResult(admitted=[...], records=[...])
    -> decide() selects final choice from EligibilityResult.admitted
    -> Routing Explanation (spec Key Entity) is rendered from EligibilityResult.records
```
