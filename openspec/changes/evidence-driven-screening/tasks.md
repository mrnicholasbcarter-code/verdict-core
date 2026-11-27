# Tasks — screening that learns, and proof from real work

## 1. Queue

- [ ] 1.1 Candidates are passively admitted free and unknown-capacity routes plus frontier subscription routes; the rest are reported as skipped without a probe. Verify: unit tests on the queue builder with recorded evidence.
- [ ] 1.2 Expected-value order from the prefix record, the passive signals and the rating; a rescreen hint no longer outranks a better score. Verify: unit tests with fixed inputs, including a prefix with no record and a route with no rating.
- [ ] 1.3 Back-off for a prefix that keeps failing, with one probe after each period. Verify: unit tests over a sequence of runs with a fake clock.

## 2. Evidence readers

- [ ] 2.1 Bounded, fail-closed readers for model-health, the routing quality state, model-latency stats, the capability filter, the `best-free` channel, the free catalog and the provider rankings; the latest discovery registry is read from disk. Verify: tests with recorded payloads, malformed payloads and timeouts; a failed reader changes order only.
- [ ] 2.2 No passive signal grants eligibility. Verify: a ladder test with every signal favourable and no proof.

## 3. Policy and report

- [ ] 3.1 `unknown` in the timer's capacity classes; the terms rating in the report; providers on the operator's deny list skipped. Verify: tests on the installed unit text, the report and the skip reason.
- [ ] 3.2 Proven-pool size by capacity, backlog and per-prefix record in the screen output and a doctor row. Verify: tests on the report and the doctor row.

## 4. Proof from real work

- [ ] 4.1 A VALIDATED tool-using node on a live worker with a verified served model records session-grade tool evidence for its exact route. Verify: runtime tests for the positive case and for each condition failing (no tool use, fault-injected executor, unverified served model, failed verification, alias route).
- [ ] 4.2 The `session_feedback` rule text and the `passive-health-refresh` requirement are amended to say when a verified task counts; an ADR note records it. Verify: `openspec validate` and the doc-link check.

## 5. Live checks

- [ ] (post-merge) 5.1 On installed main, the count of free and unknown-capacity routes holding a tool proof is recorded daily for a week and compared with 2026-11-27 (5 free routes task-eligible without a task; none for a worker node). Verify: the screen reports.
- [ ] (post-merge) 5.2 A `verdict orchestrate` run restricted to free routes dispatches a worker node. Verify: the run's events.
