# Proposal: Fail-closed review coverage and integration command evidence

## Why

**KNOWN**: BOD-288 follows the two LOW findings from the 2026-10-08 Interview Clean closeout: `OpenCodeReviewer._empty_review_reason` in `verdict/orchestration/review.py` rejects explicit skipped/empty signals, but `_interpret` can label `{}` or `{"findings": []}` as PASS without coverage evidence; the integrate verify event in `verdict/orchestration/runtime.py` records only the raw command. Close these gaps after the current certification evidence is retained, without changing historical receipts or widening public proof claims.

## What Changes

- Require positive coverage evidence before OCR output can reach a PASS: a non-empty list at `manifest.coverage.selected`, a non-empty list at `manifest.coverage.completed`, or a valid `summary.files_reviewed` count greater than zero. Preserve the existing explicit skipped, empty-selection, invalid-count, and zero-count vetoes.
- Return ERROR with detail `review coverage missing` when no positive signal exists. Missing or non-list coverage fields cannot establish list-based evidence. Treat this as an input/coverage error, not a reviewer-provider failure; do not reselect the reviewer.
- Keep real, complete OCR v1.12.9 outputs passing when they have coverage, no blocking findings, and no existing execution or identity error.
- Record integration verification `command` with `shlex.join` over the original argv and `executed_command` with `shlex.join` over the resolved argv, matching worker verification. Keep optional `resolved_argv0` and the other event fields unchanged.
- Preserve receipt replay and verification for old logs that lack `executed_command` or retain the old command rendering. Do not re-interpret historical raw OCR output.

## Capabilities

### New Capabilities

These are new formal capability contracts for existing behavior surfaces; `openspec/specs/` has no existing capability specs to modify.

- `orchestration-review-coverage`: Positive OCR coverage evidence, explicit veto precedence, and non-provider ERROR handling.
- `orchestration-integration-verification-evidence`: Original and executed argv evidence on integration verify events, with historical receipt compatibility.

## Impact

**KNOWN**: Implementation is limited to `verdict/orchestration/review.py`, `tests/test_orch_review.py`, `verdict/orchestration/runtime.py`, and `tests/test_orch_runtime.py`. The two implementation units own disjoint file pairs. The final integration check also runs the existing `tests/test_orch_receipt.py` without editing it. No new dependencies, receipt schema, routing authority, or provider calls are required.

**KNOWN**: The complete ground-truth payload at `docs/proof/dogfood-bod-273-2026-09-28/review/ocr-raw.json` has one selected item, one completed item, `files_reviewed: 1`, and no comments. The complete payload at `/home/nick/.verdict/evidence/final-6254a59/runs/clean/20261008T041611Z/review/ocr-raw.json` has two selected items, two completed items, `files_reviewed: 2`, and no comments. Both were read for this design; they are compatibility evidence, not a new live rehearsal.

**KNOWN**: The current core README says raw non-skipped coverage is needed to claim semantic review and distinguishes recorded PASS from full semantic coverage. Those narrowed claims remain accurate. This change does not edit the README or profile or claim full-file coverage, authenticated evidence, or recertification.

**INFERRED**: Producers that previously relied on absent coverage metadata to obtain PASS will now return ERROR. The two inspected complete outputs are compatible, but they do not prove compatibility with every OCR producer version.

**NOT AVAILABLE**: No new live-provider rehearsal, post-implementation certification result, or independently inspected profile-repository text is available for this proposal. Public wording changes, if later required, need a separate scoped change; they cannot be silently added to these four owned files.

## Acceptance criteria

- [ ] `{}` and `{"findings": []}` return ERROR, never PASS, with exact detail `review coverage missing`.
- [ ] Missing/non-list coverage without another positive signal returns the same ERROR. Positive selected-only, completed-only, and summary-only cases can PASS when no existing veto applies.
- [ ] Explicit skipped status/terminal state, `selected: []`, invalid file count, and file count at or below zero still return their existing ERROR reasons even if another signal is positive.
- [ ] Missing coverage does not classify as a reviewer-provider failure; one OCR launch returns ERROR without reviewer reselection or cooldown.
- [ ] Both inspected real complete OCR outputs remain PASS, with a portable retained copy of the external complete payload in `tests/test_orch_review.py`; tests do not read a developer-home path. Existing finding, identity, timeout, and every-item-failed checks retain their behavior.
- [ ] Integration verify events contain `command = shlex.join(original_argv)` and `executed_command = shlex.join(resolved_argv)` for successful and failed commands, matching the worker path.
- [ ] Historical receipts still verify by replaying recorded events without new command rendering, new required fields, or raw OCR re-interpretation.
- [ ] Unit commands pass: `python -m pytest -q tests/test_orch_review.py` and `python -m pytest -q tests/test_orch_runtime.py`; the final check passes `python -m pytest -q tests/test_orch_review.py tests/test_orch_runtime.py tests/test_orch_receipt.py`.
- [ ] `openspec validate bod-288-review-coverage-fail-closed --strict --json` reports valid with no issues, and `verdict openspec admit bod-288-review-coverage-fail-closed --json` reports admitted. These checks establish spec conformance/admission, not implementation correctness or certification.
