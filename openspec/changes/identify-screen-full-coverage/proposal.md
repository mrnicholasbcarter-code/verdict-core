# Full coverage for stale route screening

## Why

Agentic proof expires after 24 hours, while the former installed screen sampled only eight routes daily. A full inventory can therefore leave stale free and subscription routes unchecked.

## What Changes

- Run the scheduled screen every six hours with caps of 16 routes per provider and 40 routes across free and subscription capacity.
- Select stale routes in task-rating order and retain the existing consent, provider-cap, route-cap, and wall-budget behavior.
- Report routes not reached as `skipped_cap` or `skipped_deadline`; a later run can reach them.
- Keep the 30-minute systemd timeout as a backstop above the screen's built-in 600-second wall budget.

## Evidence boundary

- KNOWN: The screen implementation shares one `--max-routes` counter across free and subscription routes, applies `--max-per-provider`, orders candidates by strongest task rating first, and reports bounded skips.
- KNOWN: The screen's built-in wall budget is `prove_at_rest.DEFAULT_MAX_WALL_SECONDS = 600`; its wall-cap result is reported while the command exits 0.
- KNOWN: On 2026-11-17, 36 free routes needed an agentic probe. The installed 00:00 UTC scheduled run with caps 1/8 and capacity free,subscription checked 8 and left 582 `skipped_cap` routes (129 free, 453 subscription).
- INFERRED: Caps 16/40 and a six-hour cadence improve coverage without claiming that one run clears the backlog. Forty routes per run does not clear that backlog in one run.
- NOT AVAILABLE: A post-install scheduled run from merged main; this remains a post-merge task.

## Acceptance criteria

- The scheduled unit renders max-per-provider 16, max-routes 40, `*-*-* 00/6:00:00`, and a 30-minute timeout.
- Documentation describes shared caps, task-rating order, the 600-second built-in wall budget, and bounded skip reporting.
- The manual CLI requirement that both caps are explicitly positive remains unchanged.

## Capabilities

### New Capabilities

- `identify-screen-full-coverage`: scheduled screening runs every six hours with larger bounded caps for stale free and subscription routes.

## Impact

Identify service rendering, setup text, documentation, OpenSpec, and unit tests. Doctor behavior and screen logic are unchanged.
