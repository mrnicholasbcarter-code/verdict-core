# ADR-016: Deterministic policy and legal transition graphs

Status: Accepted  
Date: 2026-07-30

## Decision

Verdict compiles a versioned hard-policy document before ranking or execution.
Each candidate produces exactly `allow`, `deny`, or `unknown`; only `allow`
enters the ranking set. Unknown identity, availability, expired/contradictory
evidence, and missing required capability observations remain unknown and fail
closed for protected work. Explicit stale mode is non-protected only.

`TransitionCompiler` emits an inspectable graph for the initial attempt and all
retry/fallback candidates. Before response bytes, a retry or cross-route
fallback requires a safe-to-retry request, idempotency protection, a policy
allowed target, current route evidence, and a matching protocol. After bytes,
cross-route switching is forbidden; only verified same-route checkpoint/resume
can be represented as legal. Terminal execution is append-only and cannot
create another execution.

The compiler consumes #115 `RouteIdentity`, `CapabilityPassport`, and evidence
authority contracts. Legacy routing and fallback objects remain compatibility
adapters; their booleans are not policy authority. A policy digest or future
signature proves artifact integrity and issuer, not factual correctness.
