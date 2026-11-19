# review-findings-lost: dropped review comments are never a clean PASS

## Why

Issue #1137. In live run `20261119T000727Z` the independent reviewer (OCR v1.12.9) made one `code_comment` tool call carrying two non-blocking comments (one medium, one low). The call's JSON arguments were malformed, OCR recorded it under `tool_calls.failure_by_tool.code_comment` and `tool_calls.failure_details`, dropped it, and reported `comments: []`. Verdict read only `comments`, so it recorded PASS with 0 findings. That breaks the invariant "Skipped or empty review is an error, not a pass": the review's findings are unknown, not absent.

## What Changes

- `OpenCodeReviewer._interpret` counts failed `code_comment` tool calls (the larger of `failure_by_tool.code_comment` and the matching `failure_details` entries). A review with no blocking parsed finding and one or more lost calls is ERROR with detail `findings_lost: <n> code_comment call(s) dropped by the review tool`. A parsed blocking finding still yields FAIL; the lost count is recorded either way.
- `OpenCodeReviewer.review` reruns the review once on the same route, inside the existing `max_reviewer_attempts` bound, when the first result is `findings_lost`. It is not a provider failure: no cooldown and no reselection. If the rerun also loses findings, the result fails closed.
- `ReviewResult` gains `findings_lost: int = 0`; `passed` is false while it is non-zero.
- The `review` event, the `review_attempt` events and the run receipt's `review` block carry `findings_lost` only when it is non-zero, so earlier events and receipts keep their exact shape and still verify.

## Safety

- Lost findings are never invented and never counted as blocking findings.
- The retry is bounded: at most one extra launch, and never beyond `max_reviewer_attempts`.
- Tests use stub OCR output and a redacted fixture of the live payload; no provider calls.

## Capabilities

### Modified Capabilities
- `orchestration-review-coverage`: a review whose comment calls were dropped is ERROR (`findings_lost`), retried once, never PASS.

## Impact

- Runs that hit a malformed reviewer comment call now end BLOCKED with a named reason instead of COMPLETE with a false clean review.
- No JSON Schema or TypeScript contract describes the receipt review block, so neither changes.

## Acceptance Criteria

- [x] The redacted live payload (one failed `code_comment` call, `comments: []`) yields ERROR `findings_lost`, never PASS, after exactly one rerun.
- [x] A clean rerun after a lost-findings review yields PASS with no route cooled.
- [x] `max_reviewer_attempts=1` allows no rerun.
- [x] Parsed non-blocking findings with no failed comment call still PASS with the findings counted.
- [x] A failed `file_read` call is not lost findings.
- [x] The receipt review block records `findings_lost` only when positive.
- [x] `tests/test_orch_review.py`, `tests/test_orch_receipt.py` and `tests/test_orch_runtime.py` (including `test_real_skipped_ocr_result_blocks_runtime_completion`) pass.

## Evidence Boundary

- **KNOWN**: the live run's `review/ocr-raw.json` shows `tool_calls.failure_by_tool.code_comment = 1`, a `failure_details` entry with error `invalid character '}' after array element`, `summary.comments = 0` and `comments: []`; Verdict recorded PASS with 0 findings.
- **INFERRED**: OCR reports every unparseable `code_comment` call the same way, since its parse error is raised before comments are stored.
- **NOT AVAILABLE**: a live rerun that reproduces a malformed reviewer call on demand; the fix is proven on the redacted fixture of the captured payload.
