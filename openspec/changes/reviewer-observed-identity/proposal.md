# Proposal: an independent review needs an observed reviewer identity

## Why

A review with positive coverage and no usable `manifest.execution.model` was recorded as
`PASS` with an empty observed model. The independence claim then judged the selected
reviewer route, not the identity the review event recorded. An unknown reported identity
was confirmed independence.

## What Changes

- `OpenCodeReviewer._interpret`: positive coverage with a missing, blank, non-string, or
  opaque OCR-reported model is `ERROR` with detail `identity_unobserved`. That error is
  reviewer-scoped and reselects another reviewer inside the existing attempt budget.
  Coverage vetoes still do not reselect. `identity_mismatch` is unchanged.
- `_claim_independent_review`: a missing, blank, non-string, or opaque `observed_model`
  is `NOT_OBSERVED`, including event streams that predate the field. The usable observed
  identity joins the selected route and review-attempt routes when checking shared
  identity, family, and aliased pool. Receipt bytes are not rewritten.
- `manifest.execution.model` is what OCR reports. It is not, by itself, a verified
  gateway-served identity.

## Impact

- Changed: `verdict/orchestration/review.py`, `verdict/orchestration/claims.py`.
- New: `tests/test_reviewer_executed_identity.py`.
- Positive review fixtures that omitted a reported model now supply one, or assert the
  new error. Retained live proof artifacts are unchanged.
