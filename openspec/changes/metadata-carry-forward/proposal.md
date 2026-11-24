### Proposal: a refresh that skips a ratings source keeps that source's stored values

## Why

`verdict metadata refresh` without `--include-p1` skips the OpenRouter AA mirror
(`openrouter.artificial_analysis`). The refresh then rewrote the store with no
`aa_intelligence` rating on any record. The ADR-051 planner band is computed from
those ratings, so it emptied, and every orchestrate and autodev plan failed
closed until the next scheduled refresh (up to 6 hours). Seen live on
2026-11-23 (issue #1300): the real store held 0 rated records after one
unflagged refresh, and 70 again once the scheduled refresh (which passes
`--include-p1`) restored it.

A skipped source fetched nothing. That is no evidence that the values it wrote
earlier are wrong or gone, so the refresh should not erase them.

**Evidence boundary**

- KNOWN: `refresh_metadata` in `verdict/metadata/store.py` builds the new record
  set from this run's fetches only, then `_retain_failed_source_fields` restores
  last-good values for sources that errored or returned a partial page. It does
  nothing for a source with status `skipped`.
- KNOWN: the skipped status is set in `refresh_metadata` (OpenRouter AA mirror
  without `include_p1`) and by `skipped_p1` in `verdict/metadata/sources.py`
  (the other P1 score sources without a payload).
- KNOWN: the planner band reads ratings through their `fetched_at` and applies a
  12 hour freshness window (`verdict/planner_band.py`).
- INFERRED: carrying a value forward with its original `fetched_at` is enough
  for the 12 hour rule to keep deciding whether it counts; no change to
  `planner_band.py` is needed.
- NOT AVAILABLE: the writer of the 21:02Z wipe on 2026-11-23 is unknown, so this
  change does not claim to have found it.

## What Changes

- **`verdict/metadata/store.py`**: new `_carry_forward_skipped_source_fields`,
  called by `refresh_metadata` after `_retain_failed_source_fields`. For each
  capability and score field that the previous store holds, that this refresh
  left empty, and whose provenance names a source whose status is `skipped`, the
  previous value is kept unchanged (same value, source, version, raw id and
  `fetched_at`). The skipped source's `reason` gets a note saying how many values
  on how many records were carried forward.
- **`tests/test_model_metadata.py`**: `TestSkippedSourceCarryForward` with four
  tests (CLI refresh without `--include-p1`, carried ratings past 12 hours stay
  stale for the planner band, repeated skipped refreshes, a source that ran is
  not carried forward).
- **`docs/guides/model-metadata-store.md`**: one bullet under "Invent-never"
  describing carry-forward.

## Capabilities

### New Capabilities

- **metadata-carry-forward**: a refresh that skips a source keeps that source's
  stored values with their original provenance, and says so in the refresh
  output.

## Does not deliver

- The real-home write guard for tests (issue #1300 acceptance check 2). That
  belongs to issue #1306.
- Doctor's `planner_supply` row becoming an issue when 0 rated models are
  present (issue #1300, follow-up).
- The post-merge live check. It runs on installed main after merge.
- Finding what wrote the real store at 21:02Z on 2026-11-23.

Issue #1300 stays open after this change merges.

## Impact

Affected files: `verdict/metadata/store.py`, `tests/test_model_metadata.py`,
`docs/guides/model-metadata-store.md`.

Wire format and schema do not change: carried values are ordinary stored fields.
The only visible differences are that a skipped source's values survive a
refresh and its `reason` has an extra sentence when it carried something.

## Acceptance criteria

- A store holding fresh P1 ratings, refreshed without `--include-p1` in an
  isolated `VERDICT_HOME`, keeps those ratings with their original `fetched_at`.
- Ratings carried past 12 hours still count as stale for the planner band.
- The refresh output (the skipped source's `reason`) says values were carried
  forward.
- Repeated skipped refreshes keep the original `fetched_at` and add the
  carry-forward note once.
