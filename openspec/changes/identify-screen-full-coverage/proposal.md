# Full coverage for stale route screening

## Why

The installed screen timer checks a fixed small sample, while agentic proof expires after 24 hours. A full inventory can therefore leave stale free and subscription routes unchecked.

## What Changes

- Increase the scheduled screen caps to cover the current route inventory while retaining explicit CLI caps for manual runs.
- Run the scheduled screen every six hours with a bounded timeout suitable for a full screen.
- Preserve existing consent and budget controls; the screen probes only routes whose agentic proof is missing or stale.

## Evidence boundary

- KNOWN: The current service renders caps of 1 route per provider and 8 routes overall, with a daily calendar and a 12-minute timeout.
- KNOWN: A manual screen of 36 routes completed in about 10 minutes.
- INFERRED: A 16-route-per-provider and 40-route overall scheduled cap covers the current inventory while preserving positive CLI bounds.
- NOT AVAILABLE: A post-install scheduled run from merged main; this remains a post-merge task.

## Acceptance criteria

- The scheduled unit renders max-per-provider 16, max-routes 40, a six-hour OnCalendar expression, and a 30-minute timeout.
- The manual CLI requirement that both caps are explicitly positive remains unchanged.
- Existing consent and budget rules remain unchanged.

## Capabilities

### New Capabilities

- `identify-screen-full-coverage`: scheduled screening covers every stale free and subscription route within a wall-clock budget.

## Impact

Identify service unit rendering and its unit tests. Doctor behavior is unchanged.
