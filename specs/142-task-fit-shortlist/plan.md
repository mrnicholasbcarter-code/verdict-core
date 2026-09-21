# BOD-142 implementation plan

1. Add contract-first tests for task-fit strength, latency non-dominance, hard drops, unknown evidence, dynamic discovery, determinism, diversity, and shortlist-only probing.
2. Refactor `build_candidate_pool()` so its internal probe ladder can only inspect the bounded pre-probe Top-K and cannot scan all hard-eligible survivors.
3. Add a deterministic adapter from the already-created `TaskProfile` to `TaskFingerprint`, preserving the TaskProfile digest in the pool receipt.
4. Add an `IntelligenceService` candidate-pool stage after free/active/spend/capability admission and before passport/confirm. Build health/cost/latency evidence from verified snapshot/passport inputs, reduce the receipt to the pool shortlist, append named exclusions, and attach the pool receipt as compatible evidence.
5. Keep the existing BOD-104 authority path unchanged. Prove `ExecutionPathRequest.pool_receipt` constrains offers and the launch boundary still rejects a missing/invalid decision.
6. Run focused regressions, full strict mypy, Ruff/format, diff check, independent review, exact-head CI, merge, and clean-main verification.

No new ADR is needed: this completes the already-defined BOD-122/BOD-140/BOD-104 pipeline and does not create a new authority or public protocol.
