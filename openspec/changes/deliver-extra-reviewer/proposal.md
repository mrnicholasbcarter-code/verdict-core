# The ready gate's extra review finishes: a preferred reviewer and a prompt ceiling

## Why

`verdict deliver` marks a pull request ready only after a cross-family review when the run's own review was route-level. That extra review picks the cheapest admitted route of another family, which today is one free route, and passes the reviewer tool no per-request prompt ceiling. In six deliveries (2026-11-26 and 27) it did not finish twice: 3.38 M and 4.15 M tokens, budget exceeded, pull request left a draft, on changes the controller's separate review passed. Both times the diff touched large modules (GitHub issue #1367, item 12).

The gate failing closed is right. Failing on half the clean changes is not, because every such pull request then needs a person or the controller to mark it ready.

## What Changes

**`verdict/orchestration/review.py`**

- `OpenCodeReviewer` can pass the reviewer tool a per-group prompt token ceiling (`ocr review --max-tokens`). Off unless the caller sets it, so the in-run review is unchanged.

**`verdict/orchestration/deliver_review.py`, `verdict/commands/parsers_deliver.py`**

- The extra review uses a ceiling of 200,000 tokens by default (`--review-max-tokens N`; 0 turns it off).
- The operator can name preferred reviewer routes (`--reviewer-route ROUTE`, repeatable, or `VERDICT_DELIVER_REVIEWER_ROUTES`). The gate first looks for a reviewer among the preferred routes that the ladder admits and that are in none of the implementer families; if there is none it chooses as it does today.

## Capabilities

### New Capabilities

- `story-delivery-extra-review`: how the ready gate's extra cross-family review is bounded and how its reviewer is chosen.

## Impact

`verdict/orchestration/review.py`, `deliver_review.py`, `verdict/commands/parsers_deliver.py`, `dispatch.py`, `docs/CLI_REFERENCE.md`, tests. With no preference set, the only behaviour change is the ceiling on the extra review. Nothing about when a pull request is marked ready changes.

Invariants (AGENTS.md): a preference reorders among admitted routes and never admits or authorises one; review stays on a route outside the implementer families; a skipped, empty or incomplete review is still an error.

## Evidence boundary

- **KNOWN** (all on #1367, one run per cell, `ocr` v1.12.9, `--effort low`):
  - Six production extra reviews, all on `openrouter/baidu/ernie-5.5-flash-preview:free`: four complete (75 k to 1.10 M tokens), two partial with the budget exceeded (3.38 M, 4.15 M).
  - The same route on the same two ranges with `--max-tokens 200000`: complete 2 of 2 (1.93 M) and 8 of 8 (0.33 M); with 60,000: complete both (0.75 M, 0.62 M).
  - `nvidia/nemotron-3.5-super-vl-120b-a12b` with the 200,000 ceiling: complete on four ranges (122 k to 579 k tokens, 2 to 4 minutes). On three of them a stronger reviewer had found a real defect; it found none of the three.
  - `build_selector(route_ids=...)` narrows the admitted set to named routes, so a preference can be tried without bypassing admission.
- **INFERRED:** the reviews fail on file size, not on the route: the failures all include large modules, and the ceiling alone made the failing route finish.
- **NOT AVAILABLE:** run-to-run spread (the same route returned 0 and 5 findings on one diff); whether 200,000 is the best ceiling; whether a ceiling lowers what a review finds; any measure of how good the extra review is as a check.

## Acceptance criteria

- The extra review passes the ceiling to the reviewer tool; the in-run review's command is unchanged.
- With preferred routes set, an admitted preferred route of another family reviews; a preferred route that is not admitted, or is in an implementer family, is never used; with none usable the choice is today's.
- `delivery.json` names the route that reviewed (`extra_review_route`, already recorded).
- (post-merge) On installed main, a delivery whose diff includes `verdict/orchestration/runtime.py` has a complete extra review.
