# Agentic evidence and background census

## Why

> **Superseded in part by BOD-342 (2026-10-21):** Verdict's selection is now
> price-free (strongest proven-capable route first); admission no longer
> defaults to free-first. The agentic qualification evidence this change adds
> is still needed for any route, free or paid, and is unaffected.

Free-first admission requires exact-route agentic qualification evidence.
The existing health cache and controller session ledger already carry evidence.

## What Changes
- Import trusted controller outcomes through the existing health-cache merge lock.
- Add explicit systemd user setup for the existing consented, budgeted daemon.
- Show full-cache health state and fresh agentic qualification by capacity in eligibility.

## Impact
No changes to admission, freshness, identity, review, or budget authority.
Session import never refreshes liveness or grants capacity.
No new probing mechanism or dependencies.
