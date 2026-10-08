# Spec Delta: Orchestration review coverage

## Purpose

Require positive coverage evidence before new OCR output can count as a passing independent review, while preserving existing fail-closed errors and provider-recovery boundaries.

## ADDED Requirements

### Requirement: OCR PASS SHALL require positive coverage evidence

After existing explicit coverage vetoes, `OpenCodeReviewer._interpret` SHALL permit a payload to reach the findings-based PASS decision only if at least one signal is positive: `manifest.coverage.selected` is a non-empty list, `manifest.coverage.completed` is a non-empty list, or `summary.files_reviewed` is a count greater than zero under the existing count conversion. Absent fields, malformed containers, and non-list selected/completed values SHALL NOT establish list-based evidence. If no signal is positive, the result SHALL be ERROR with exact detail `review coverage missing`, never PASS. Positive coverage SHALL NOT override execution, identity, every-item-failed, or blocking-finding checks.

#### Scenario: Empty object fails closed
- **WHEN** a successful OCR process returns `{}`
- **THEN** interpretation returns ERROR with detail `review coverage missing`, never PASS

#### Scenario: Findings-only object fails closed
- **WHEN** a successful OCR process returns `{"findings": []}` without coverage evidence
- **THEN** interpretation returns ERROR with detail `review coverage missing`, never PASS

#### Scenario: Missing or malformed coverage cannot establish evidence
- **WHEN** selected/completed fields are absent, or are strings, mappings, numbers, booleans, or null rather than lists, and no positive file count exists
- **THEN** interpretation returns ERROR with detail `review coverage missing` without raising a container-access exception

#### Scenario: A selected-only positive signal is sufficient
- **WHEN** `manifest.coverage.selected` is a non-empty list, completed and summary evidence are absent, findings are nonblocking, and no existing error applies
- **THEN** interpretation returns PASS

#### Scenario: A completed-only positive signal is sufficient
- **WHEN** `manifest.coverage.completed` is a non-empty list, selected and summary evidence are absent, findings are nonblocking, and no existing error applies
- **THEN** interpretation returns PASS

#### Scenario: A summary-only positive signal is sufficient
- **WHEN** a successful OCR process returns `{"summary": {"files_reviewed": 1}, "findings": []}` without a manifest, with no existing error
- **THEN** interpretation returns PASS

#### Scenario: Wrong-type list fields do not replace summary evidence
- **WHEN** selected/completed fields are non-list values, `summary.files_reviewed` is a valid positive count, findings are nonblocking, and no existing veto applies
- **THEN** interpretation can return PASS based on the summary, not on the non-list fields

### Requirement: Existing explicit coverage vetoes SHALL take precedence

Explicit skipped status or manifest terminal state, explicitly empty selected lists, invalid file counts, and file counts at or below zero SHALL remain ERROR with the existing details. These vetoes SHALL apply before considering positive alternative evidence.

#### Scenario: Skipped status or terminal state defeats positive evidence
- **WHEN** status or manifest terminal state is `skipped`, even with non-empty coverage lists and a positive file count
- **THEN** interpretation returns ERROR with detail `review skipped: no items reviewed`

#### Scenario: Empty selected list defeats completed or summary evidence
- **WHEN** selected is `[]`, even with a non-empty completed list or a positive file count
- **THEN** interpretation returns ERROR with detail `review skipped: no items selected`

#### Scenario: Zero or negative file count defeats list evidence
- **WHEN** `summary.files_reviewed` parses to zero or a negative count, even with a non-empty selected or completed list
- **THEN** interpretation returns ERROR with detail `review skipped: zero files reviewed`

#### Scenario: Invalid file count defeats list evidence
- **WHEN** a present `summary.files_reviewed` cannot be parsed by the existing count conversion, even with a non-empty selected or completed list
- **THEN** interpretation returns ERROR with detail `review coverage invalid: files_reviewed is not a count`

### Requirement: Coverage errors SHALL NOT trigger reviewer-provider recovery

A `review coverage missing` result SHALL be an input/coverage ERROR and SHALL NOT be classified as a reviewer-provider failure. The reviewer SHALL return this result after the first OCR launch without reselection, provider cooldown, or pool-exhaustion substitution. Existing skipped-review handling and genuine provider-failure recovery SHALL remain unchanged.

#### Scenario: Missing coverage ends review after one launch
- **WHEN** the selected reviewer returns `{}` or `{"findings": []}` with a successful process exit and another independent route is eligible
- **THEN** the result is ERROR with `review coverage missing`, the provider-failure predicate is false, and exactly one review launch/attempt is recorded with no reselection or cooldown

#### Scenario: A genuine reviewer timeout remains recoverable
- **WHEN** the reviewer process times out and another independent reviewer is eligible within the existing retry budget
- **THEN** existing timeout classification and bounded reviewer-provider recovery remain in effect

### Requirement: Real complete OCR output SHALL remain compatible

Complete retained OCR v1.12.9 payloads with positive coverage, the selected model identity, no blocking findings, and no other existing error SHALL still yield PASS. Regression evidence SHALL use their real manifest, summary, comments, and groups shapes rather than a findings-only success approximation, and SHALL be portable without a developer-home dependency.

#### Scenario: Retained one-item complete review still passes
- **WHEN** the complete payload from `docs/proof/dogfood-bod-273-2026-09-28/review/ocr-raw.json` is interpreted for its recorded `codex/gpt-6-sol` route
- **THEN** its one selected item, one completed item, `files_reviewed: 1`, and empty comments produce PASS

#### Scenario: Retained two-item complete review still passes
- **WHEN** a portable complete copy of the inspected Interview Clean payload is interpreted for its recorded `cx/gpt-5.5` route
- **THEN** its two selected items, two completed items, `files_reviewed: 2`, and empty comments produce PASS without reading a developer-home path

#### Scenario: Blocking findings still reject covered output
- **WHEN** an otherwise valid covered payload contains a blocking finding
- **THEN** interpretation returns FAIL rather than allowing positive coverage to override the finding

#### Scenario: Identity substitution and all-items failure retain their errors
- **WHEN** covered output has an observed model different from the selected route or all selected items failed
- **THEN** the applicable existing ERROR and reviewer-provider recovery behavior remain unchanged
