# Design

## Context

Verdict is moving from a fixed-model or simple-eligibility approach to a dynamic, role-based selection system. This requires a pipeline that balances measured performance (strength) with hard constraints (capacity, quota, family independence).

## Goals / Non-Goals

**Goals:**
- Deterministic selection based on the pipeline contract (Inventory -> Admission -> Narrowing -> Order).
- Side-effect-free shadow evaluation.
- Strict enforcement of ADR-0001 invariants.
- Fail-closed behavior for empty candidate sets.

**Non-Goals:**
- Changing the fundamental Admission ladder (S1).
- Introducing non-deterministic "AI-picked" routes without a hard eligibility gate.
- Modifying the public API for simple routing.

## Decisions

### Pipeline Implementation
The pipeline is implemented as a sequence of filters. S2 (Narrowing) is strictly remove-only.

### Capacity Policy
The `capacity_policy.role_capacity_allowed` is the single point of truth for whether a capacity class (Free, Subscription, Metered, Unknown) is permitted for a given role.

**D7 Rule (Decided, operator, 2026-11-17)**: For critical roles (planner, reviewer, controller, escalation), an `assumed-FREE` status (absence of evidence) SHALL be treated as UNKNOWN. This is enforced only in `capacity_policy.role_capacity_allowed`. The legacy `capacity.classify()` result (absence of paid evidence is FREE) is kept for existing callers. This ensures critical roles are not populated by unverified free routes, regardless of `VERDICT_ALLOW_UNKNOWN_CAPACITY`.

### Reliability and Quota
Reliability is tracked via `ReliabilityObservation` derived from call logs. Quota is managed via a `QuotaView` that handles cooldowns and reserves.

### Reviewer Independence
To prevent self-review, the Reviewer role filter explicitly excludes the family of the model that served the author role for that node.

### Flag Modes
- `off`: Uses legacy logic.
- `shadow`: Calculates dynamic result, records it, but uses `off` result for dispatch.
- `on`: Uses dynamic result for dispatch.

## Risks / Trade-offs

- **Identity Mapping**: T1 research shows only ~35% coverage. This is mitigated by the `unique_leaf` join mechanism and the fact that unmapped routes are treated as `UNKNOWN` (which are never healthy/eligible for critical roles).
- **Overhead**: Shadow evaluation adds minor computation but no network calls beyond those already required for dispatch.

## Security / trust implications

The system adheres to the "Strength never admits" rule. A model cannot be selected simply because it is "strong" if it fails the hard eligibility or capacity gates.

## Routing / context / memory implications

The selection uses the Core metadata store. It does not introduce new stateful memory but relies on the `events.jsonl` for reliability observations.

## Migration / rollback

The `off` flag provides an immediate rollback to the previous behavior. T12 describes the long-term cleanup of legacy constants.

## ADR impact

Amends ADR-036 to replace the simple economics ordering with the role-based pipeline. Reaffirms ADR-0001 invariants.
