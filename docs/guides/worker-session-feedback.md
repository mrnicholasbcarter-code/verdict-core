# Worker-session failure feedback

The production DAG runtime and planner failure handlers call the selection
ladder's `record_session_failure` hook. Real adapter-attested availability
failures become negative records in the existing shared health cache. A new
run reads those records before ranking. Existing selectors reload the cache
before assessment and dispatch, and after a refresh hook.

- Account quota, rate limit, authentication and permission failures block the
  canonical credential pool when classifier scope is provider. The ladder
  records this as a `pool:<pool>` cooldown; there is no separate provider-wide
  key, so other pools of the same provider stay eligible.
- Explicit route-scoped failures do not block healthy credential siblings.
- A later success on the same route clears that route's own cooldown but never
  an active pool capacity cooldown.
- Verification, ownership, review, context-size and unknown failures do not
  write provider health evidence.
- Injected, scripted and unknown executor provenance never supply live evidence.
- Negative expiry leaves an unprobed route, not a healthy route. A subsequent
  probe must validate health before launch.
- Available real turn counts are recorded; missing counts stay null/unknown.
  Provenance is a digest, not an error, transcript or credential.
- Persistence errors are visible as controller events with
  `phase=session_feedback`, `recorded=false` and an exception-class reason.
  The existing classified failure and bounded rerouting still occur.

This is failure learning, not positive capability qualification or learned
ranking. The session ledger remains the task-outcome store. The advisory ranker
and admission membership are not changed.

## Evidence boundary

Focused offline regressions cover runtime-handler writes, another-run selection,
pool scope, route scope, expiry, external writes, refresh negatives and task/fault
exclusion. They do not prove a live self-hosted completed story. Live execution
and cross-family release review must still pass before release.
