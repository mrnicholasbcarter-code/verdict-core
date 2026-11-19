# Spec Delta: dropped review comments are never a clean PASS

## ADDED Requirements

### Requirement: Lost review findings SHALL fail closed

`OpenCodeReviewer` SHALL count the `code_comment` tool calls that OCR reports as failed in `tool_calls.failure_by_tool` or `tool_calls.failure_details`. When the count is positive and no parsed finding is blocking, the result SHALL be ERROR with detail starting `findings_lost` and SHALL record the count as `findings_lost`; it SHALL NOT be PASS. A parsed blocking finding SHALL still yield FAIL with the count recorded. Lost findings SHALL NOT be invented or counted as blocking findings. The reviewer SHALL rerun the review once on the same route within `max_reviewer_attempts`, with no provider cooldown and no reselection, and SHALL fail closed if the rerun also loses findings. The `review` event, `review_attempt` events and the run receipt's review block SHALL carry `findings_lost` when it is positive and SHALL omit it otherwise.

#### Scenario: Malformed comment call is not a clean pass
- **WHEN** OCR completes with `comments: []` and one failed `code_comment` call, and the rerun returns the same
- **THEN** the result is ERROR with detail `findings_lost`, `findings_lost` is 1, no findings are recorded, and exactly two launches occurred

#### Scenario: A clean rerun recovers
- **WHEN** the first review loses findings and the rerun is a clean covered review
- **THEN** the result is PASS, attempts record `findings_lost` then a clean attempt, and no route is cooled

#### Scenario: The retry is bounded
- **WHEN** `max_reviewer_attempts` is 1 and the review loses findings
- **THEN** exactly one launch occurs and the result is ERROR with `findings_lost`

#### Scenario: Parsed non-blocking findings with no dropped calls still pass
- **WHEN** OCR reports parsed medium and low comments and no failed `code_comment` call
- **THEN** the result is PASS with both findings counted and `findings_lost` 0

#### Scenario: A failed non-comment tool call is not lost findings
- **WHEN** only a `file_read` call failed
- **THEN** `findings_lost` is 0 and a clean covered review is PASS

#### Scenario: The receipt records the loss
- **WHEN** a run's review recorded `findings_lost`
- **THEN** the receipt review block and its attempts carry `findings_lost`, and a clean run's receipt omits the key
