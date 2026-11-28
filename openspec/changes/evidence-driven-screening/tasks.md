# Tasks — screening that learns, and proof from real work

## 0. Before building

- [ ] 0.1 The operator answers the two open questions in `design.md` (probing account-tied connections; the deny list). Verify: the answers are recorded on #1387 and in `design.md`.
- [ ] 0.2 On the live gateway, confirm what a probe sees when the gateway answers from another model, and that the served-model check rejects it. Verify: the request and response headers are recorded on #1387.

## 1. Queue

- [ ] 1.1 The queue is keyed on the catalog's `owned_by` and `root`; the same id of a model is chosen in every run, and a model is not probed while either id holds a fresh proof. The design names which id the existing alias rule keeps. Verify: unit tests with a catalog that lists one model under two ids, over two runs, and a ladder test that the other id stays unproven.
- [x] 1.2 A route that does not declare tool support is skipped as `skipped_no_tools`. Verify: unit tests on the queue builder.
- [x] 1.3 The per-provider record over seven days, and the back-off with one probe after each period; the stored result kind decides what counts (pass, tool failure and model-not-served count; gateway refusals, rate limits, timeouts and 5xx do not). Verify: unit tests over a sequence of runs with a fake clock, including a refusal-only provider and a provider whose catalog is stale.
- [x] 1.4 Three tiers, ordered inside by the provider's pass rate and then the rating; one probe in eight for the third tier; a rescreen hint no longer outranks. Verify: unit tests with fixed inputs, including a provider with no record.
- [ ] 1.5 At most two probes per run for a provider whose connections sign in as a person, read from the `authType` of the connection rows already used for capacity. Verify: unit tests with recorded connection rows, including a provider with both kinds of connection.

## 2. Evidence

- [ ] 2.1 A bounded, fail-closed reader for the free catalog's terms rating; a failed read leaves the rating blank and changes nothing else. Verify: tests with recorded, malformed and timed-out payloads.
- [ ] 2.2 No passive signal grants eligibility. Verify: a ladder test with every signal favourable and no proof.

## 3. Policy and report

- [ ] 3.1 `unknown` in the timer's capacity classes, as first-tier candidates only; providers on the operator's deny list skipped as `skipped_denied`. Verify: tests on the installed unit text and the skip reason.
- [ ] 3.2 Proven models by capacity, the backlog, the per-provider record by result kind with its terms rating, and the providers in back-off, in the screen output and a doctor row. Verify: tests on the report and the doctor row.

## 4. Proof from real work

- [ ] 4.1 A VALIDATED tool-using node on a live worker with a verified served model records session-grade tool evidence for its exact route. Verify: runtime tests for the positive case and for each condition failing (no tool use, fault-injected executor, unverified served model, failed verification, alias route).
- [ ] 4.2 The `session_feedback` rule text and the `passive-health-refresh` requirement are amended to say when a verified task counts; an ADR note records it. Verify: `openspec validate` and the doc-link check.

## 5. Live checks

- [ ] (post-merge) 5.1 On installed main, the count of free and unknown-capacity models holding a tool proof is recorded daily for a week and compared with 2026-11-27 (10 free models proven after three runs; none eligible for a worker node), with each provider's pass rate against its rate the week before. Verify: the screen reports.
- [ ] (post-merge) 5.2 A `verdict orchestrate` run restricted to free routes dispatches a worker node. Verify: the run's events.
