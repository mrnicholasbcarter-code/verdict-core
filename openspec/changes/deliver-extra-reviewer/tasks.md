# Tasks — bounding and choosing the ready gate's extra review

## 1. Prompt ceiling

- [ ] 1.1 `OpenCodeReviewer` takes an optional per-group prompt ceiling and passes `--max-tokens N` to the reviewer tool only when it is set. Verify: tests on the built command with and without it; the in-run reviewer's command is byte-identical to today's.
- [ ] 1.2 `verdict deliver` sets the ceiling to 200,000 by default; `--review-max-tokens N` changes it, 0 turns it off, a negative or non-integer value is refused. Verify: CLI and wiring tests.

## 2. Preferred reviewer routes

- [ ] 2.1 `--reviewer-route ROUTE` (repeatable) and `VERDICT_DELIVER_REVIEWER_ROUTES`: the gate's reviewer is chosen from the preferred routes that are admitted and outside the implementer families, else as today. Verify: tests with injected selectors (preferred and admitted; preferred but not admitted; preferred but in an implementer family; none set).
- [ ] 2.2 The CLI reference describes both options. Verify: the doc-link check and a help-text test.

## 3. Live check

- [ ] (post-merge) 3.1 On installed main, a delivery whose diff includes `verdict/orchestration/runtime.py` has a complete extra review, and `delivery.json` names the route. Verify: the delivery record and `deliver-review/ocr-raw.json`.
