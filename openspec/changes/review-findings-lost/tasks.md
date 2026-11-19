# Tasks

## 1. OpenSpec Change Artifacts

- [x] 1.1 Write proposal.md, design.md and the spec delta for orchestration-review-coverage
- [x] 1.2 Validate strictly with the vendored OpenSpec validator

Verification method (how to verify): `uv run python verdict/openspec_vendor/validate_openspec_change.py openspec/changes/review-findings-lost` passes.

## 2. Tests (red first)

- [x] 2.1 Redacted fixture `tests/fixtures/ocr/sample-findings-lost.json` from the live payload
- [x] 2.2 Tests in `tests/test_orch_review.py` and `tests/test_orch_receipt.py` for every scenario

Verification method (how to verify): the new tests failed before the fix and pass after it.

## 3. Implementation

- [x] 3.1 `ReviewResult.findings_lost` and `passed`
- [x] 3.2 `_interpret` counts failed `code_comment` calls; `review` reruns once
- [x] 3.3 `review` event, `review_attempt` events and receipt carry `findings_lost`

Verification method (how to verify): `uv run python -m pytest -q tests/test_orch_review.py tests/test_orch_receipt.py tests/test_orch_runtime.py` passes.

## 4. Verification

- [x] 4.1 Focused tests, ruff, mypy --strict, doc links, reachability, lint gates

Verification method (how to verify): each command exits 0 and vgate passes on push.
