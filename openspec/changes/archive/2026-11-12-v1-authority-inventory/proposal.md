# Proposal

## Why

Wave 2 of the V1 hardening plan makes the server the owner of task authority and makes the gateway inventory bounded and honest about what it does not know. The foundation change `verdict-core-v1-hardening` covers Wave 1 only. The complete 13-story umbrella plan is preserved in `docs/contracts/v1-hardening-umbrella-plan.md` and tracked in Linear under BOD-431. This change covers S02 (BOD-432) and S04 (BOD-345) only.

## What Changes

- **Server-owned task authority and price-invariant selection (S02 / BOD-432 / A07, A08, A25)**: `RouteRequest` carries typed task requirements (`task_family`, `min_context_tokens`, `reasoning_required`). Reserved weakening flags are rejected before selection or send. Offline catalog mode cannot authorize execution. The execution-path optimizer ranks offers by trusted `pool_receipt` shortlist position, not by price. Callers without a receipt build one from live probes with `build_live_probe_receipt`. ADR-035 is amended with this policy.
- **Bounded live inventory (S04 / BOD-345 / A01, A03, A05)**: `load_omniroute_admit_snapshot` in `verdict/free_tier_admit.py` enforces discovery bounds and fails closed with `incomplete_source` when a bound or the cursor walk is violated. The snapshot records `fetched_at`, `inventory_complete`, `inventory_diagnostics` and `unsupported_fields`, so unknown telemetry is not read as zero.
- **Test isolation (BOD-442)**: an autouse fixture in `tests/conftest.py` stubs the systemd runners so doctor tests do not read the host user manager. This changes no production behaviour and adds no requirement.
- **Wave scoping**: Stories S01 and S08 stay in `verdict-core-v1-hardening`. Stories S03 and S05-S13 enter `openspec/changes/` as separate scoped changes.

## Capabilities

### New Capabilities
- `task-authority`: server-owned task requirements, rejection of client-supplied policy weakening, and price-invariant selection from a trusted shortlist.
- `bounded-inventory`: bounded gateway catalog discovery that fails closed and keeps missing telemetry unknown.

### Modified Capabilities

## Impact

Affects `verdict/api.py`, `verdict/serve_path.py`, `verdict/execution_path.py`, `verdict/candidate_pool.py`, `verdict/intelligence.py`, `verdict/autodev_run.py`, `verdict/cli.py`, `verdict/subagent_resolver.py`, `verdict/actions/helpers.py`, `verdict/free_tier_admit.py`, `verdict/metadata_service.py`, `verdict/prove_at_rest_service.py`, `docs/adr/ADR-035-authorized-selected-route-dispatch.md`. No new dependency. Pre-merge implementation and automated tests are complete; live acceptance on merged main remains with the operator.
