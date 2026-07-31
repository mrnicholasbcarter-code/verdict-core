# ADR-018: Evidence-gated shadow and counterfactual evaluation

**Status:** Accepted  
**Related:** [#119](https://github.com/mrnicholasbcarter-code/verdict-core/issues/119),
[ADR-010](ADR-010-fail-closed-capability-passports.md),
[ADR-016](ADR-016-deterministic-policy-and-transition-graphs.md),
[ADR-017](ADR-017-durable-privacy-safe-receipt-ledger.md)

## Decision

Evaluation artifacts are versioned and payload-free. Every observation binds an
exact executable route, task fingerprint, variant, repeat seed, independent
verification receipt, freshness window, and canonical failure class. Reports
are durably recorded before they can produce a promotion decision.

Promotion is evidence-gated: the controller recomputes report integrity and
requires an exact route, suite, policy, and fresh capability-passport digest.
The controller accepts only a decision it issued for the recorded report and
policy context; caller-supplied `allowed=true` is not authority. Lifecycle
transitions remain explicit and fail closed.

Counterfactual results are linked to a scoped source receipt whose task and
observed-route metadata match the request. They are replay-only and cannot
authorize training or promotion. Rollback records the selected known-good
report when available, but leaves the route degraded until a new promotion is
approved. A kill switch quarantines the route and blocks rollback/promotion.

Quality confidence intervals describe the mean bounded quality score; they are
not binary success intervals. Operational failures are counted separately and
never converted into quality evidence.

The public Python compatibility surface is `verdict.evaluation`; the artifact
schema is `schemas/evaluation.v1.json` and all serialized artifacts carry
`schema_version: "1"`.
