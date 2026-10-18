# Proposal

## Why

The V1 hardening plan establishes reliable runtime controls across shared gateway binding, scheduled metadata maintenance, trusted inventory, model qualification, and feedback. This active change implements and verifies the foundation wave (Wave 1: S01 / BOD-416 and S08 / BOD-436). The complete 13-story umbrella plan across all 7 waves is preserved byte-for-byte in `docs/contracts/v1-hardening-umbrella-plan.md` and tracked in Linear under BOD-431.

## What Changes

- **Canonical Credential and Gateway Binding (S01 / BOD-416 / A06)**: Unify credential and gateway binding across discovery and transport callers. Enforce explicit precedence (configured URL > environment URL > default `http://127.0.0.1:20128/v1`), bind canonical stored credentials to both callers identically, and ensure secret-free error diagnostics.
- **Scheduled Metadata Refresh with Provenance (S08 / BOD-436 / A24)**: Provide one optional 6-hour metadata refresh timer (`verdict-metadata-refresh.timer` / `verdict-metadata-refresh.service`) with a 5-minute boot delay, explicit setup opt-in, non-destructive dry-run, source-level provenance, partial-failure retention of valid last-good facts, concurrent execution lock, and doctor completion and freshness reporting.
- **Wave Scoping**: Scope active OpenSpec requirements in this change to Wave 1 foundation capabilities. Future waves (S02–S13) remain tracked in `docs/contracts/v1-hardening-umbrella-plan.md` and enter `openspec/changes/` as separate scoped changes.

## Capabilities

### New Capabilities
- `v1-model-control`: Foundation requirements for canonical credential and gateway binding and scheduled metadata maintenance with source provenance.

### Modified Capabilities

## Impact

Affects `verdict.core.credentials`, `verdict.core.transport`, `verdict.cli.setup`, and `verdict.cli.doctor`. No public API or external dependency is introduced. Pre-merge implementation and automated test verification are complete; live gateway verification and full DoD remain subject to operator acceptance on merged main.
