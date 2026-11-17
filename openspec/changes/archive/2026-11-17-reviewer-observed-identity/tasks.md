# Tasks

## 1. Interpretation

- [x] 1.1 Positive coverage with no usable OCR-reported model is `ERROR`, detail
  `identity_unobserved`, not `PASS`: `tests/test_reviewer_executed_identity.py`
- [x] 1.2 Blank, non-string, and opaque reported models are the same error and are not
  stringified into an identity
- [x] 1.3 `identity_unobserved` reselects another reviewer inside the existing budget;
  coverage vetoes do not

## 2. Independence claim

- [x] 2.1 Missing, blank, non-string, or opaque `observed_model` is `NOT_OBSERVED`,
  including a legacy stream with no key
- [x] 2.2 Observed identity equal to a worker executed identity is `CONTRADICTED` even
  when the selected reviewer route differs
- [x] 2.3 Shared family or aliased pool on the observed identity is `NOT_OBSERVED`
- [x] 2.4 A genuinely independent observed reviewer stays `VERIFIED`
