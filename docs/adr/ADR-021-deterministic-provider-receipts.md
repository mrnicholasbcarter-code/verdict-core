# ADR-021: Deterministic Provider Evaluation Receipts

- **Status:** Accepted — Implemented in PRO-001
- **Date:** 2026-08-03
- **Scope:** Provider receipts for Verdict Risk (`verdict-risk`), Verdict Strategy (`verdict-edge`), and Verdict Backtest (`verdict-backtest`)

## Context

Domain provider libraries (`verdict-risk`, `verdict-strategy`, `verdict-backtest`) perform deterministic mathematical evaluations (drawdown limits, EV payout gates, Monte Carlo simulations). To participate in the Verdict evidence chain without acquiring policy or routing authority, each provider must emit portable, evidence-compatible evaluation receipts.

## Decision

1. **Portable ProviderReceipt Schema:** All domain providers implement `build_provider_receipt` emitting a standardized `ProviderReceipt` payload with:
   - `schema_version`: `"1"`
   - `run_id`: Unique execution identifier
   - `provider`: Provider identifier (`"verdict-risk"`, `"verdict-edge"`, `"verdict-backtest"`)
   - `provider_version`: Package version
   - `inputs_hash`: SHA-256 digest of canonicalized input payload
   - `config_hash`: SHA-256 digest of canonicalized configuration
   - `outcome`: Evaluation outcome string (e.g., `"approved"`, `"denied"`)
   - `provenance`: Redacted provenance mapping
   - `evidence_refs`: Tuple of linked evidence digests
2. **Immutable & Privacy-Safe:**
   - Provenance and details dictionaries are frozen recursively on receipt creation to prevent post-hoc mutation.
   - Sensitive keys (`api_key`, `authorization`, `password`, `secret`, `token`) are strictly rejected at the receipt boundary.
3. **Non-Authoritative:** Provider receipts provide verifiable audit evidence for downstream decision logging, but providers CANNOT grant execution authority.

## Consequences

- Risk, strategy, and backtest evaluations produce tamper-evident, privacy-safe audit trails.
- Identical evaluation inputs produce identical `inputs_hash` and `config_hash` values, enabling deterministic replay.
- Domain engines remain pure mathematical evaluation libraries with zero network or orchestration overhead.
