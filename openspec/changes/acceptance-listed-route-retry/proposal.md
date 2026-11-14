# Acceptance listed-route check: retry once before counting a failure

## Why

Hourly `verdict acceptance` went RED on 2026-11-12 15:00Z because one sampled listed route
(`antigravity/gemini-3.1-flash-lite`) returned HTTP 502 on turn 1, dropping listed precision
to 87.5% below the 95% bar. A transient single-turn failure should not fail acceptance: the
same route deserves one retried attempt before it counts against precision, with both attempts
logged so flaky routes stay visible. The 95% bar, the 8-route listed sample, the precision
formula and the recall trend are unchanged. Approved by the operator 2026-11-11 as change D of
the acceptance-stabilisation plan (Linear BOD-345 comments).

## What Changes

- `verdict/acceptance_toolcheck.py::measure`: when a listed route's first probe fails
  (non-account outcome), retry it once before counting it as a failure. The route's final
  outcome is pass if either attempt passes; it counts as failed only when both attempts fail.
  Both attempts are logged in `checks` (tagged `attempt`), so flaky routes stay visible.
- `tests/test_acceptance.py`: red-first tests for retry-once (second attempt pass counts,
  both attempts fail counts as fail, both attempts logged), then green.
- No precision/recall/formula change; `precision_min` 0.95 and `LISTED_SAMPLE` 8 unchanged.
- Unlisted recall sample keeps a single attempt (the retry is specific to the listed-route
  precision check).
- No new numeric caps, no model calls in tests (fake gateway), no network in unit tests.

## Impact

`verdict/acceptance_toolcheck.py`, `tests/test_acceptance.py`. Acceptance can stay green
through a single transient turn-1 failure on a listed route instead of flipping RED.
