# ADR-042: Dynamic Role-Based Model Selection

**Status:** Proposed
**Date:** 2026-11-17
**Deciders:** Operator
**Amends:** ADR-036 (Planner and Reviewer selection)
**Related:** ADR-0001 (Control-plane invariants), ADR-032 (Core model metadata store)

## Context

Verdict currently uses a fixed or manual model selection for its orchestration roles. While the eligibility ladder in ADR-036 ensures basic qualification, it does not dynamically optimize for the specific requirements of different roles (Planner, Implementer, Reviewer, Controller) based on live measured strength, capacity, and quota. To maximize efficiency and quality, Verdict needs a role-aware selection pipeline that leverages the Core metadata store and measured performance.

## Decision

Verdict implements a dynamic model selection pipeline for orchestration roles. The selection process is governed by a strict multi-stage pipeline contract.

### 1. Pipeline Contract

The selection for any role follows these stages in order:

- **S0: Inventory** - Fetches live `/v1/models` rows (or a fixture labelled `fixture=true`). Output: **H0**.
- **S1: Admission** - Applies the existing admission ladder and eligibility floors (as defined in `verdict/admission.py`, `eligibility.py`, `orchestration/eligibility.py`). Opaque `auto/*` and `combo/*` routes are removed. Unknown runtimes are never healthy. Output: Canonical **AdmittedSet (H1)**. Dynamic selection never adds to this set.
- **S2: Role Narrowing (Remove-Only)** - Iteratively removes candidates. Each removal must carry a skip reason.
    - **Capacity**: Role policy over capacity class + confidence.
    - **Quota**: `QuotaView` check for cooldown/reserve.
    - **Pool**: Keeps the best-ordered route per backend pool.
    - **Family**: Enforces reviewer independence, controller fences, and implementer exclusions.
    - **Measured Demotion**: Removes routes with a history of non-completion for the specific role.
    - **Operator Override**: Applies explicit exclusions from the operator override file.
    - Output: **H2** plus skip list `[{route_id, stage, reason}]`.
- **S3: Advisory Order** - Applies the `role_strength` signal through `advise_order`. If confidence is low (stale or missing), the legacy order is preserved. Output: Ordered ladder **H3**.

**Assertions:**
- `H3 set == H2 set`
- `H2 subset of H1`
- Every route in `H3` must be present in `AdmittedSet`.
- An empty `H3` results in `LadderResult(status=FAIL_CLOSED, reason=...)`. It never defaults silently and never hangs.

### 2. Capacity Precedence

Capacity is the primary filter for economics. The single enforcement point is `capacity_policy.role_capacity_allowed`. 

- **Strength never admits**: A high strength score cannot bypass a capacity or eligibility gate.
- **Narrowing only removes**: The pipeline can only narrow the set, never re-admit.
- **Set Hierarchy**: `H3 ⊆ H2 ⊆ H1`.

### 3. Reliability and Quota Contract

- **Reliability Observation**: Decisions are based on a reliability observation contract drawing from call-log evidence.
- **Quota Sharing**: Managed via named config fields with defaults.
- **Unknowns**: Unknown runtimes stay explicit and are never healthy.
- **Pay-per-token**: `METERED` capacity is never the default.

### 4. Shadow Evaluation and Flag Modes

To ensure safety, the dynamic selection is deployed via flag modes:
- **Off**: Permanent fail-safe. Uses legacy selection paths.
- **Shadow**: Runs the dynamic pipeline and records the decision in a `role_ladder.shadow` event, but dispatches the route selected by the `off` path.
- **On**: Uses the dynamic pipeline for dispatch.

**Entry Point Defaults:**
- `/v1/route`, Relay, and `verdict route` (CLI): Unchanged.
- Orchestration: Default is `off`.

### 5. Role Specifics

- **Reviewer Independence**: The reviewer model must be served by a family different from the author model (verified via call logs).
- **Controller Role**: The controller uses a dedicated role ladder and is fenced from worker roles.
- **Inventory Dispositions**: Managed through the Core metadata store.
- **Cleanup**: T12 defines limits for cleaning up legacy constants once `off` mode is proven stable.

### 6. Invariants (from ADR-0001)

The following invariants remain unchanged and are strictly enforced:
- Admission comes before ranking.
- Unknown is never healthy.
- Opaque routes are never candidates.
- Attempts are bounded.
- Review runs on a separate route.
- A receipt is bound to the SHA-256 of `events.jsonl`.
- Advisory signals may reorder but never authorize.
- Fixture data never satisfies live proof.

## Consequences

- **Deterministic Selection**: Every selection is provenance-rich and replayable via the `role_ladder.shadow` event.
- **Safe Deployment**: Shadow mode allows validation of the dynamic pipeline against the legacy baseline without risking execution.
- **Reduced Waste**: Measured demotion and quota awareness reduce failed attempts.
- **Strict Safety**: The `FAIL_CLOSED` behavior for empty sets prevents silent failures or hangs.
