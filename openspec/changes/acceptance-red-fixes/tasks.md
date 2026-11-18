# Tasks

## 1. Specification

- [x] 1.1 Write proposal.md, tasks.md and the spec delta with scenarios
- [x] 1.2 `openspec validate acceptance-red-fixes --strict` passes

## 2. Tests first

- [x] 2.1 Health probe and ladder: captured refusal is unhealthy and route-scoped
- [x] 2.2 Forced reprobe and proof invalidation, both directions
- [x] 2.3 `lease_held` reason and exit status; acceptance wait, single retry, no retry for `no_eligible_route`
- [x] 2.4 Docs refresh started when stale, not when ready; doctor repair hint
- [x] 2.5 Relay failover after a refusal without an idempotency key

## 3. Implementation

- [x] 3.1 `openai_health_probe` and ladder scope
- [x] 3.2 `prove-at-rest once --reprobe` and billing-negative proof invalidation
- [x] 3.3 `lease_held` outcome, acceptance retry, identify screen calendar
- [x] 3.4 Docs-refresh setup action and doctor hint
- [x] 3.5 Relay edge after an in-band refusal

## 4. Live checks

- [ ] (post-merge) 4.1 `verdict acceptance run` on the installed build is green, or red only for a reason outside this change
- [ ] (post-merge) 4.2 `verdict prove-at-rest once --allow-live-probe --reprobe <route> --json` lists the route under `reprobe.queued`
- [ ] (post-merge) 4.3 `systemctl --user cat verdict-identify-screen.timer` shows `OnCalendar=*-*-* 00/6:20:00`
