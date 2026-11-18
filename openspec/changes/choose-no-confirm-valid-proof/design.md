# Design: choose --no-confirm accepts valid proof older than 10 minutes

## Context

The `verdict choose --no-confirm` command is a read-only selector: it must never send a model confirmation or write selection evidence. Previously, it inferred read-only mode from `max_probes_per_select=0` but still applied the 10-minute `PERMIT_PROOF_MAX_VALIDITY_SECONDS` freshness window from `health_cache.py`. This caused routes with valid (unexpired, authorized, eligible) passports older than 10 minutes to be marked `proof_stale`, requiring a probe that the zero budget could not satisfy — resulting in `probe_budget_exhausted` and no route selected.

The `passport_confirmation` function in `admit_prove_confirm.py` already defines passport validity correctly: a passport is valid when it exists, is authorized, eligible, not expired, and has no newer negative health evidence. The 10-minute window is a dispatch-time concern (for permit issuance), not a validity concern.

## Goals / Non-Goals

**Goals:**
- In read-only mode (`--no-confirm`), select routes with valid passports regardless of `qualified_at` age.
- Provide specific skip reasons for invalid passports (`passport_missing`, `passport_unauthorized`, `passport_ineligible`, `passport_expired`, `passport_superseded`).
- Preserve lease-held behavior (`lease_held` reason wins).
- Normal mode (confirm allowed) behavior unchanged.

**Non-Goals:**
- Change dispatch-time proof freshness (the 10-minute window still applies for permit issuance).
- Modify `passport_confirmation` or the admit gate.
- Allow read-only mode to probe or write evidence.

## Decisions

1. **Explicit `read_only` flag**: Instead of inferring read-only from `max_probes=0`, pass `read_only=no_confirm` from `build_selector`. This separates probe budget from freshness policy and makes the intent clear.

2. **One validity test, shared**: `read_only_valid_proof()` in `verdict/orchestration/eligibility.py` keeps a route when `passport_confirmation([route], now=passport.qualified_at)["admitted"]` holds and `expires_at` is after now. Evaluating the normal test at the passport's own `qualified_at` waives only the 10-minute permit window; authorization, eligibility and newer negative health evidence (which `passport_confirmation` already applies by dropping the passport) are unchanged.

3. **Both read-only call sites use it**: `build_selector(no_confirm=True)` seeds the admitted set's launch evidence from it, and `EligibilityLadder.select()` uses it only where normal mode found the proof stale. There is no extra rejection stage: a route that never reaches the proof check is assessed exactly as in normal mode (first f1f7fea2 attempt rejected 35 such routes at AVAILABLE; regression test `test_read_only_mode_does_not_reject_unpassported_route_at_available`).

4. **Named skip reasons**: a read-only route that would otherwise need a probe gets `passport_missing`, `passport_superseded`, `passport_expired`, `passport_unauthorized`, `passport_ineligible` or `passport_stale` instead of `probe_budget_exhausted`, at the same stage. `lease_held` is checked first and still wins.

## Risks / Trade-offs

- **Risk**: A route with a valid-but-old passport might have degraded capability since the passport was issued. **Mitigation**: `passport_confirmation` drops a passport with newer negative health evidence, so a recently failed route is excluded.
- **Risk**: Divergence between read-only and normal mode validity definitions. **Mitigation**: Normal mode unchanged; read-only explicitly ignores only the 10-minute window, not authorization/expiry/health.
- **Trade-off**: Adding `read_only` flag increases API surface slightly, but prevents future bugs where `max_probes=0` is used for other purposes.

## Security / trust implications

No new trust boundaries. Read-only mode still never probes or writes. The health cache used for negative evidence is the same one used by the admit gate and dispatch. Passport validity (auth_state, availability_state, expiry) is unchanged.

## Routing / context / memory implications

- Routing: `--no-confirm` may now return a route that normal mode would probe. This is the intended fix.
- Context: No change to context packing or worker prompts.
- Memory: No change to session ledger or memory integration.

## Migration / rollback

- **Migration**: No data migration needed. The change is purely behavioral in the selector.
- **Rollback**: Revert `read_only_valid_proof`, the `read_only` flag in `EligibilityLadder.select()` and its use in `build_selector`. No state format changes.

## ADR impact

No new ADR required. The change aligns `--no-confirm` behavior with the existing `passport_confirmation` validity definition (ADR-036 goal-to-receipt orchestration, ADR-0001 invariants). The 10-minute window remains the dispatch-time permit freshness bound per `health_cache.py`.