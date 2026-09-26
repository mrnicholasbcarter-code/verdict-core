# Worker admission authority correction

**Goal:** Make Verdict the single authority for live worker admission and replacement so an unavailable subscription route cannot be launched, an unauthorized provider family cannot enter the pool, and the first terminal provider failure advances to the next admitted route.

## Root causes

1. `verdict.worker_runtime` bypasses the canonical availability pipeline and builds its own candidate pool from catalog + Prime visibility + probes.
2. Worker dispatch does not require usable quota/usage evidence even though `verdict.availability` and `verdict.omniroute` already model quota, budget, token headroom, cooldowns, lockouts, and runtime eligibility.
3. Worker route-family scope is not a hard admission gate in the original runtime.
4. Controller identities are partly hard-coded instead of excluding the active controller decision.
5. Prime retry and Verdict replacement can both retry a failed child route, delaying or obscuring first-failure reassignment.

## Task 1 — Hard provider and controller boundary

- Add explicit `allowed_route_prefixes` and dynamic `excluded_route_ids` to worker requirements.
- Reject out-of-scope routes before probing or spawning.
- Preserve named exclusion reasons.
- Regression: `antigravity/*` is never probed or spawned when the operation authorizes only `cc/*,kr/*`.

## Task 2 — Canonical availability gate for worker dispatch

- Reuse `OmniRouteAvailabilityAdapter` / `OmniRouteHTTPTransport`; do not introduce another quota parser.
- Extend canonical requirements with an opt-in `require_usage_evidence` hard gate for live worker admission.
- Configure every documented runtime source that the available credentials permit: health always; management rate-limit/cooldown sources when a management token exists; budget/token-limit sources when both management token and usage key id exist.
- Worker dispatch requires measured usable capacity. `quota_remaining_pct <= 0`, `token_headroom == 0`, explicit `eligible=false`, lockout/cooldown, or missing required usage evidence must exclude the route before spawn.
- Intersect the canonical admitted IDs with Prime visibility and the operation's route-family/controller boundary. Ranking may only order survivors.

## Task 3 — Single retry authority

- Verdict owns worker replacement. Prime executes one exact admitted route per Verdict attempt.
- Project Prime settings must not independently retry a failed worker provider/model before Verdict receives the terminal failure.
- A terminal 400/401/402/403/429/5xx/timeout/malformed worker result is classified once, cooldown/exclusion is persisted, cleanup is confirmed, and the next admitted route is tried immediately.
- Regression: first route 429/403 -> second admitted route once; no repeated first-route execution and no escape to an unapproved family.

## Task 4 — Dynamic controller exclusion and runtime config

- Pass the active controller route into dispatch policy rather than relying on a static controller-model list.
- Normalize task config set-valued fields on the worker-runtime boundary.
- The active controller cannot be admitted as a worker even when it is in an allowed route family.

## Task 5 — Verification

Run targeted tests for availability, OmniRoute transport, subagent selection, worker runtime, Prime integration, and the new regressions. Then run Ruff/format, mypy, and the full pytest suite through CI. Do not merge while any required check is red.
