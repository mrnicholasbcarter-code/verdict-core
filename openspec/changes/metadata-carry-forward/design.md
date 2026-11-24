# Design: metadata-carry-forward

## Context

`refresh_metadata` builds records from the sources it fetched in this run, then
merges the previous store in two ways today: `_retain_failed_source_fields`
keeps last-good values when a source errored or returned a partial page, or when
the old value is newer than the new one. A source with status `skipped` (the
caller did not ask for it) gets neither, so its stored values vanish from the
rewritten store.

## Goals / Non-Goals

**Goals:**
- A skipped source's stored values survive a refresh with their original
  provenance and `fetched_at`.
- The refresh output says what was carried forward.
- Freshness stays the reader's decision (ADR-051's 12 hour window for planner
  ratings).

**Non-Goals:**
- Changing `planner_band.py` or any freshness rule.
- Guarding tests against writing the real home (#1306).
- Changing doctor's `planner_supply` row.
- Carrying forward values of sources that errored (already handled by
  `_retain_failed_source_fields`).

## Decisions

1. **Carry forward only fields the new refresh left empty.** A value that this
   run produced always wins, so a carried value can never shadow fresh data.
2. **Select by the field's own provenance source, not by field name.** A field is
   carried only when its `provenance.source` is a source whose status is
   `skipped`. Values from sources that ran stay as that run wrote them, so a
   refresh still removes a value its own source no longer returns.
3. **Keep the value object unchanged.** The same `ProvenancedField` goes into the
   new store, so `fetched_at`, `version` and `raw_id` are the original ones. The
   12 hour rule then treats a carried value exactly like a stored one of that age.
4. **Say it in the existing `reason` field.** The note is appended to the skipped
   source's reason (`... ; carried forward N value(s) on M record(s) from the
   previous store with their original fetched_at`), so `verdict metadata refresh`
   output and `--json` show it with no schema change. The reason is rebuilt from
   the current run's status each time, so repeated skipped refreshes add the note
   once.
5. **Run after `_retain_failed_source_fields`.** Both functions only fill empty
   fields, and the two sets of sources do not overlap (a source is either
   skipped or errored), so the order does not change the result.

## Risks / Trade-offs

- **A record that left models.dev loses its carried ratings.** Carry-forward is
  by record id and only for records present in the new P0 set. A record that no
  longer exists has nothing to attach to. That matches today's behaviour for
  every other field.
- **Stale values stay in the store.** That is intended: age is decided by the
  reader, and the test pins that ratings carried past 12 hours do not count for
  the planner band.
- **`reason` is free text.** Consumers that match on the exact skipped reason
  would miss the appended note. A search of the repository found only a test that
  asserts the status, which still passes.

## Security / trust implications

None. No new network or file access; the previous store is already read under the
same lock.

## Routing / context / memory implications

The planner band (ADR-051) reads the same fields as before. It sees the carried
ratings with their original `fetched_at` and applies its own freshness window.

## Migration / rollback

Rollback: revert the two files. A store written while the change was live is a
normal store.

## ADR impact

No new ADR. ADR-051 already says the 12 hour freshness rule decides whether a
rating counts.
