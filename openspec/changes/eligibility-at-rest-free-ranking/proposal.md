# Eligibility that stays true at rest, with FREE routes ranked for agent work

## Why
On the operator's gateway (2026-10-13) `verdict eligibility --json` listed 0
eligible routes while the cache held 13 FREE routes with a passing agentic
probe. Three defects caused this, and one more made the selected route wrong:

- Liveness decayed after 600 s (cache) or 300 s (ladder), and nothing re-checked
  it, so proven routes fell back to `unprobed`.
- Every prober negative was unscoped, so `_cooled_pools` treated one 401/403 as
  a whole-pool cooldown. One "only available on agentic harnesses" 403 cooled
  `openrouter-free` (26 routes, kilocode `:free` included). One 401 that came
  after a 200 cooled `nvidia` (37 routes). Live run 3 then probed only
  bluesminds catalog ghosts: 19 probed, 0 passed.
- `select()` confirmed fresh routes first and round-robined providers. It
  SELECTED a llama-3.2-11b-vision route at rank 15 over gemma4, codestral and
  command-a.
- FREE routes ranked by id order and capability slack, not by fitness for agent
  work; later, one subscription session pass outranked every FREE route.
- Prober chat probes that the gateway shed with 503 "resource pressure" under
  host load wrote 15-minute route negatives for working FREE routes.

## What Changes
- One 15-minute liveness window (`FRESH_SECONDS = 900`) for the cache, the
  ladder and admission. A FREE route with an agentic proof <= 24 h that the
  gateway's synced catalog serves and that is not blocked is eligible, with
  reason `liveness_at_select`. Selection confirms candidates in rank order with
  a 1-token check and falls through on failure. After 2 failed checks in one
  select, a provider's remaining routes wait behind every other provider's
  routes. Failed routes leave the ranking, and the selected route is rank 0.
- The prober records `failure_scope` on every failure. Account markers in the
  provider message (key limit, API key, credits, billing, account) write a
  `pool:<id>` cooldown, and the prober sends no further request to that pool.
  A model-named error, an error after an answer, or an error while another
  route of the pool succeeded stays on the route. Negatives without a scope
  block only their own route.
- Ranking (operator rule, free-first not free-only): routes with failing session
  evidence go last, then capacity class (FREE first), then session score; plan
  and review work rank proven > unknown > failing before capacity class. Inside
  FREE capacity: the gateway success-rate bucket
  (`/api/usage/model-latency-stats`), capability metadata (size, coding,
  context >= 256k, vision-only penalty), context, latency. Quota never ranks
  FREE routes.
- A gateway overload (503/429 with the gateway's own "resource pressure",
  "heap pressure" or "chat_admission_busy" body) is not route evidence: no
  health write, no cooldown; 5 in a row end the prober cycle; select moves on.
- The prober caps half-open retries at 25% of a cycle's chat requests, least
  recently tried first. The daemon clears the open cursor after each cycle.
- `capacity_class_of` uses the ladder's `classify_capacity`, so kilocode
  `:free` routes are FREE for the prober too.

## Impact
Admission is unchanged: listing never probes and never grants launch
authority; select still confirms the exact route. Paid capacity keeps its
cheapest-sufficient order after session outcomes.
