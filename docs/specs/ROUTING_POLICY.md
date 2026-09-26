# Routing Policy

**Status:** Active
**Authority:** This policy governs the Verdict Core routing surface. Changes to
its safety invariants require an ADR.
**Related:** [ADR-036](../adr/ADR-036-goal-to-receipt-orchestration.md) defines
the current goal-to-receipt orchestration path.

## 1. Routing invariant

Verdict filters candidates before ranking. `EligibilityGate` is the shared
authority for the routing path and explain surface, so a downstream ranker
cannot reintroduce a candidate it excluded.

The gate evaluates live availability evidence supplied through the availability
source. Candidates in the eligible or ready state can enter the pre-ranking
set. For protected work, absent, unknown, error, unavailable, timeout,
malformed, or unauthorized live truth excludes a candidate. Non-protected work
in development mode can admit an unverified candidate only when the gate is
configured to allow it; the decision remains marked as unverified.

A candidate denied by live eligibility, including quota, rate-limit, policy,
or circuit conditions, is not admitted.

## 2. Routing and explain surfaces

| Surface | Current behavior |
|---|---|
| `POST /v1/route` | Routes one task through the intelligence service and records route evidence. |
| `GET /v1/route/explain` | Explains cached availability and eligibility. It can also retrieve an immutable execution-evidence record by its supported selector. |
| `GET /v1/models` | Returns Verdict's locally filtered upstream model catalog. Catalog membership is discovery information, not availability proof. |
| `EligibilityGate` | Produces the pre-ranking eligible set and per-candidate exclusion records. |
| `AvailabilityCache` | Provides bounded freshness, stale-while-revalidate behavior, and explainable cache state. |
| `ProbeRunner` | Supports bounded liveness probes where the caller has enabled live probing. |

The route decision and execution are separate concerns. A routing result is not
proof that an upstream call completed correctly.

## 3. Dynamic catalog rule

Do not hardcode worker provider allowlists or static model tiers into the
routing path. Use the configured catalog and live availability evidence. A
model name or a catalog row alone does not prove capability, entitlement,
health, quota, or task eligibility.

The legacy classifier is not a substitute for live admission evidence. Keep
selection and ranking downstream of the eligibility filter.

## 4. Protected work

Protected work fails closed when required live availability truth is absent.
The protected decision path must not turn an unknown, stale, failed, or denied
candidate into an eligible one. Development-mode unverified admission does not
apply to protected work.

## 5. Current orchestration pointers

Goal-to-receipt execution uses the ADR-036 orchestration runtime:

- `verdict eligibility` renders the orchestration eligibility ladder and can
  lazily probe candidates with `--probe`.
- `verdict orchestrate` plans and runs a goal, then writes a receipt.
- `verdict supervise` provides bounded controller supervision and resume.
- `verdict watch` renders a run; `verdict run-receipt` shows and verifies its
  receipt.

See [ADR-036](../adr/ADR-036-goal-to-receipt-orchestration.md) and the
[orchestration golden path](../guides/orchestration-golden-path.md) for the exact
runtime contract and command examples. [ADR-023](../adr/ADR-023-governed-swarm-supervision.md)
is superseded historical material.

## 6. Verification

Before claiming a routing change is complete, run the checks that cover the
changed path. The standard repository baseline is:

```bash
uv run --extra dev --extra dashboard --extra server ruff check .
uv run --extra dev --extra dashboard --extra server ruff format --check .
uv run --extra dev --extra dashboard --extra server mypy verdict --strict
uv run pytest -q
git diff --check
```

Also exercise affected API or CLI paths and record any unavailable external
dependency as a limitation. Do not call a timed-out or unrun check green.
