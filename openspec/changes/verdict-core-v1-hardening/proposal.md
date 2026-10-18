# Proposal

## Why

The V1 hardening plan establishes reliable runtime controls across shared gateway binding, scheduled metadata maintenance, trusted inventory, model qualification, and feedback. This active change implements and verifies the foundation wave (Wave 1: S01 / BOD-416 and S08 / BOD-436). The complete 13-story umbrella plan across all 7 waves is preserved byte-for-byte in `docs/contracts/v1-hardening-umbrella-plan.md` and tracked in Linear under BOD-431.

## What Changes

- **Canonical Credential and Gateway Binding (S01 / BOD-416 / A06)**: Unify credential and gateway binding across discovery and transport callers. Enforce explicit precedence for gateway URL (explicit `base_url` argument > `OMNIROUTE_BASE_URL` environment > default `http://127.0.0.1:20128` normalized to `/v1`). Resolve credentials via `resolve_gateway_key` (environment `VERDICT_OMNIROUTE_API_KEY` then `OMNIROUTE_API_KEY`, then `credentials.env`, then owner-only key file, then Prime auth), binding callers identically without leaking secrets in errors.
- **Scheduled Metadata Refresh with Provenance (S08 / BOD-436 / A24)**: Provide one optional 6-hour metadata refresh timer (`verdict-metadata-refresh.timer` / `verdict-metadata-refresh.service`) using `OnUnitInactiveSec=6h` (six hours after previous run completes) with a 5-minute boot delay (`OnBootSec=5m`). Support explicit opt-in via interactive `verdict setup` prompt or `verdict metadata install-service`, non-destructive dry-run, per-store lock covering read/merge/atomic replacement for refreshes and manual saves, source provenance, partial-failure retention of valid last-good facts, and doctor completion/freshness reporting even when service Result has its default success value.
- **Wave Scoping**: Scope active OpenSpec requirements in this change to Wave 1 foundation capabilities. Future waves (S02–S13) remain tracked in `docs/contracts/v1-hardening-umbrella-plan.md` and enter `openspec/changes/` as separate scoped changes.

## Capabilities

### New Capabilities
- `v1-model-control`: Foundation requirements for canonical credential and gateway binding and scheduled metadata maintenance with source provenance.

### Modified Capabilities

## Impact

Affects `verdict/credentials_store.py`, `verdict/api.py`, `verdict/actions/helpers.py`, `verdict/metadata/store.py`, `verdict/metadata_service.py`, `verdict/doctor_diagnostics.py`, `verdict/home_setup_render.py`, and CLI parser/dispatch. No external public API or dependency is introduced. Pre-merge implementation and automated test verification are complete; live gateway verification and full DoD remain subject to operator acceptance on merged main.
