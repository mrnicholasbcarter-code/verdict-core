# Tasks

## 1. Wave 1 — shared gateway binding and metadata schedule

- [ ] 1.1 **S01 / BOD-416** (related BOD-385), acceptance **A06**: unify credential and gateway binding across discovery and transport; verify canonical/stored credentials bind both callers identically, explicit URL precedence, safe default, and secret-free errors using the S01 focused tests.
- [ ] 1.2 **S08 / BOD-436** (related BOD-108, BOD-411), acceptance **A24**: install one six-hour metadata refresh timer with explicit source provenance; verify setup/doctor, partial-source retention, freshness, and recovery with isolated tests.

## 2. Wave 2 — task authority and bounded inventory

- [ ] 2.1 **S02 / BOD-432** (residual of BOD-417), depends on S01, acceptance **A07, A08, A25**: preserve server-owned task requirements through public entrypoints and make admitted ranking price-invariant; verify reserved-flag rejection, task-fit enforcement, and price-only invariance with the S02 focused tests.
- [ ] 2.2 **S04 / BOD-345** (related BOD-387, BOD-407), depends on S01, acceptance **A01, A03, A05**: bound inventory and parse scoped telemetry without healthy defaults; verify completeness, caps/cursors, aliases, unknowns, and observed zeros with the S04 focused tests.

## 3. Wave 3 — durable dispatch authority

- [ ] 3.1 **S03 / BOD-433** (related BOD-417, BOD-223), depends on S02 and S04, acceptance **A04, A10**: bind final dispatch permits to durable evidence epochs and retry handoffs; verify before/after-commit revocation, restart replay, scope isolation, expiry, and duplicate-attempt fencing with the S03 focused tests.

## 4. Wave 4 — shared qualification and protocol correctness

- [ ] 4.1 **S05 / BOD-297** (related BOD-292, BOD-310, BOD-350), depends on S03 and S04, acceptance **A09, A12, A13, A14, A15, A16**: route all qualification producers through one scoped coordinator and shared budgets; verify fairness, multi-process caps, crash recovery, fencing, joins, cancellation, and actual safe concurrency with S05 focused tests.
- [ ] 4.2 **S06 / BOD-434** (related BOD-417, BOD-353, BOD-84), depends on S02 and S03, acceptance **A17, A18, A19, A20, A21, A22, A23**: preserve SSE terminal semantics and route identity with close-once cleanup; verify fragmented streams, failure terminals, identity, bounded retries, cancellation, and independent check behavior with S06 focused tests.

## 5. Wave 5 — public projection and joined outcomes

- [ ] 5.1 **S07 / BOD-435** (related BOD-417, BOD-291, BOD-387), depends on S04, S05, and S06, acceptance **A02, A03, A11**: publish verified public models and recover readiness without request-time probes; verify exact freshness boundaries, unavailable startup/recovery, and zero catalog model calls with S07 focused tests.
- [ ] 5.2 **S09 / BOD-437** (related BOD-353, BOD-415), depends on S03 and S06, acceptance **A18, A26, A28**: join every attempt and independent checker result in the existing ledger; verify success/reject/retry/failure/cancel/incomplete joins, append-only checker links, and rejection of duplicate, future, tampered, or wrong-scope evidence with S09 focused tests.

## 6. Wave 6 — task history and installed runtime provenance

- [ ] 6.1 **S10 / BOD-438** (related BOD-304, BOD-353), depends on S02, S08, and S09, acceptance **A25, A27, A28**: consume checked task-specific history using the specified 30-day window, seven-day half-life, smoothing, and minimum-three demotion; verify restart/idempotency, transient exclusions, exact scores, and next-shortlist consumption with S10 focused tests.
- [ ] 6.2 **S11 / BOD-414** (related BOD-411, BOD-421, BOD-410), depends on S05, S07, and S08, acceptance **A29**: align installed acceptance and maintenance runtimes to the reviewed build; verify recorded build/config provenance and diagnose unexpected parallel loops with isolated installed-state checks.

## 7. Wave 7 — live and complete acceptance

- [ ] 7.1 **S12 / BOD-410** (related BOD-355), depends on S05, S06, S07, S10, and S11, acceptance **A23, A29, A30**: demonstrate bounded live maintenance across three cycles on merged main; record date, exact SHA, commands, raw results, freshness, and parent rerun evidence using the existing proof gate.
- [ ] 7.2 **S13 / BOD-439** (related BOD-355), depends on S01–S12, acceptance **A01–A30**: verify every matrix row against its owning evidence and close delivery only when required merged-main and independent parent proof is present; verify with the complete acceptance matrix and DoD checker.
