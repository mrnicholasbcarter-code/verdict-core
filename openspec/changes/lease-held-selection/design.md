# Design

## Context

The six-hourly screen (`verdict identify --screen`) acquires a cross-process flock lock (`verified-refresh.lock`) for about 10 minutes while it refreshes the verified model cache. During this window, the hourly acceptance run (00/06/12/18 UTC) also runs and calls into selection. One code path checks `_JobLock(_lock_path).live()` and, when true, disables all candidate evaluation:

1. `EligibilityLadder.select` (eligibility.py ~1214-1222): `probe_order = []` — this empties the only loop where `chosen` gets assigned, so even healthy, launchable, fresh-proof routes are never selected.

The BOD-297 comment at this site says the intent was only "never probe while another producer owns the job". The fix must preserve that intent while allowing already-proven routes to be selected.

## Goals / Non-Goals

**Goals:**
- When the lease is held, skip probe dispatch only. Confirmation calls are already skipped by the existing BOD-297 path in `admit_prove_confirm.py`.
- Routes that are already healthy, launchable (admitted), and have fresh agentic proof are still chosen/admitted.
- Routes that would need a probe get reason `lease_held` (eligibility).
- `judge_choose` extracts failure reason from stdout JSON first, then last non-"outcome history:" stderr line.

**Non-Goals:**
- Change the screen, the lock, or timers.
- Change the coordinator's refresh behavior.
- Add new locks or coordination primitives.

## Decisions

### EligibilityLadder.select

1. Keep `probe_order` as computed (do not empty it when lease is held).
2. Detect lease held once before the loop: `lease_held = health_cache is not None and _JobLock(_lock_path).live()`.
3. In the probe loop (lines ~1223-1290), the branch that sends a probe is guarded by `if a.health != "healthy" or confirming or proof_stale:`. Inside that branch, before incrementing `probes_used` and calling `self._probe`, add: `if lease_held: a.reason = "lease_held"; continue`.
4. This means:
   - Healthy + launchable + fresh proof: never enters the probe branch, gets chosen as before.
   - Unhealthy / not launchable / stale proof: enters probe branch, sees `lease_held`, gets `reason="lease_held"`, continues to next candidate without probing.
   - If all candidates need a probe, `chosen` remains `None`, selection fails closed with `lease_held` reasons.

### judge_choose

1. Current code (line 285): `first = err.strip().splitlines()[-1] if err.strip() else ""` — takes last stderr line.
2. Change to: iterate stderr lines from the end, return the first line that does not start with `outcome history:`. If none, fall back to last line (or empty).
3. Keep stdout JSON parsing first (line 283-284).

## Risks / Trade-offs

- A route that is healthy but not launchable (needs confirmation) will get `lease_held` reason during the screen window. This is correct: we cannot confirm it without the lease, and we shouldn't probe. The hourly acceptance may not select a route during those ~2 minutes, but that's acceptable — the screen is a known maintenance window.
- The `lease_held` reason is new; ensure it doesn't conflict with existing reason strings. Search shows no existing `lease_held` reason.
- `judge_choose` change is defensive: if stdout JSON is absent/malformed, we still get a meaningful error from stderr.

## Security / trust implications

- No new trust boundaries. The lease is already a cross-process coordination primitive.
- Skipping probes/confirmations during the lease window means we rely on cached proof. The proof validity window (`PERMIT_PROOF_MAX_VALIDITY_SECONDS`) already bounds staleness.
- No anonymous auth bypass, no change to API auth.

## Routing / context / memory implications

- Selection receipts will show `lease_held` reasons for routes that needed probes during the screen window. This is observable and auditable.
- No routing policy, context pipeline, or memory store changes.

## Migration / rollback

- Revert the two files (`eligibility.py`, `acceptance.py`) to restore the previous behavior (empty `probe_order`).
- No data migration needed.

## ADR impact

- ADR-0001 (control plane invariants): Admission before ranking still holds — we still admit fresh-proof routes first, then would probe (but skip due to lease). The admitted set narrows correctly.
- ADR-036 (goal-to-receipt): Receipts will record `lease_held` reasons, maintaining traceability.
- No ADR file changes required.