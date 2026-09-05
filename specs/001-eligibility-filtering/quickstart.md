# Quickstart: Validating Eligibility Filtering

## Prerequisites

```bash
cd /home/nick/dev/verdict-core
uv sync --extra dev --extra dashboard --extra server
```

## Run existing + new eligibility tests

```bash
uv run pytest tests/test_eligibility_gate.py tests/test_passport_eligibility.py -q
```

Expected: all pass, including new cases for the `confidence` field and the FR-007/FR-006
clarifications (added during `/speckit-implement`).

## Manual validation scenarios

1. **P1 — ineligible candidate excluded (protected, fail-closed)**
   - Build an `EligibilityGate` with `protected_fail_closed=True`.
   - Call `evaluate(candidates, protected=True, dev_mode=False)` where one candidate's
     `availability_source` returns an unknown/error state.
   - Expect: that candidate is absent from `result.admitted`; its `EligibilityRecord` has
     `admitted=False` and `confidence < 1.0`.

2. **P2 — explainable decision**
   - Inspect `result.records` for the excluded candidate.
   - Expect: `record.reason` is non-empty and `record.confidence` reflects the low-confidence
     state that drove exclusion.

3. **P3 — consistency across entry points / FR-007 best-available posture**
   - Call `evaluate(candidates, protected=False, dev_mode=True)` with the same unknown-state
     candidate.
   - Expect: candidate is admitted (`admitted=True`), `confidence` still reflects the
     underlying state (e.g. `0.0`), and the explanation records that it was admitted under the
     best-available/`dev_mode` posture.

4. **Re-check before final selection (FR-006)**
   - Confirm via `decision_kernel.decide()` call site that only one `evaluate()` call occurs
     per `decide()` invocation, and that `decide()`'s final pick is drawn from that same call's
     `EligibilityResult.admitted` — no separate later admission check exists to go stale.

## Full baseline (per CLAUDE.md)

```bash
uv run pytest -q
uv run --extra dev --extra dashboard --extra server ruff check .
uv run --extra dev --extra dashboard --extra server ruff format --check .
uv run --extra dev --extra dashboard --extra server mypy verdict --strict
git diff --check
```
