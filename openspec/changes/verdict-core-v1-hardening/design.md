# Design

## Context

This change defines the architecture for the Wave 1 Foundation of V1 hardening, covering S01 / BOD-416 (shared gateway binding) and S08 / BOD-436 (scheduled metadata maintenance). The full 13-story umbrella plan spanning Waves 1–7 (acceptance criteria A01–A30) is preserved byte-for-byte in `docs/contracts/v1-hardening-umbrella-plan.md`.

## Goals / Non-Goals

**Goals:**
- Bind discovery and transport callers to canonical credentials and explicit URL precedence with default `http://127.0.0.1:20128/v1`.
- Provide an optional 6-hour metadata refresh timer with 5-minute boot delay, explicit setup opt-in, non-destructive dry-run, source provenance, partial-source retention, concurrent lock, and doctor diagnostics.
- Record accurate pre-merge code implementation and automated test verification for foundation tasks.

**Non-Goals:**
- Activate requirements for future waves (S02–S13: task authority, durable dispatch, shared qualification, SSE terminal semantics, joined outcome ledger, task history demotion, or complete acceptance matrix); these remain in `docs/contracts/v1-hardening-umbrella-plan.md`.
- Claim full story DoD, Done status, or archive this change prior to operator live verification on merged main.
- Authorize live host service installation or mutate running systemd units during build/test.

## Decisions

1. **Foundation Scope**: Scope `v1-model-control` in this active change strictly to Wave 1 foundation capabilities. Subsequent waves enter OpenSpec in dedicated follow-on PRs.
2. **Canonical Credential and Gateway Resolution**: Enforce strict precedence: configured URL > environment variable > default `http://127.0.0.1:20128/v1`. Canonical stored credentials bind both discovery and transport identically. Errors sanitize and omit secret keys.
3. **Scheduled Metadata Refresh**: Install one optional 6-hour timer (`OnUnitActiveSec=6h`) with 5-minute boot delay (`OnBootSec=5m`). Setup requires explicit opt-in (`verdict setup --install-service` or interactive confirmation). Dry-run mode performs no persistent file or systemd writes. Scheduled runs acquire an exclusive file lock to prevent concurrent executions.
4. **Source Provenance and Partial Failure**: Metadata refreshes record source identity, fetch timestamps, method, and scale. When a single source fails or is malformed, valid last-good facts for other sources are retained within original deadlines. Unsupported sources are never fabricated as live integrations.
5. **Doctor Diagnostic Freshness**: `verdict doctor` reports timer installation status, completion evidence, and source freshness. If the timer is not installed, doctor emits an opt-in warning without failing unrelated checks. If the timer triggered without valid completion evidence, doctor reports an incomplete refresh even if unit status is active.
6. **Task and Lifecycle Accounting**: Tasks in `tasks.md` reflect pre-merge code and automated test verification. Stories S01 and S08 remain open in Linear and this change remains unarchived until the parent verifies live commands on merged main.

## Risks / Trade-offs

- [Partial metadata failure leaves stale facts] → Retain valid last-good facts within original deadlines and record explicit source provenance.
- [Active change divergence from umbrella plan] → Full 13-story plan is preserved byte-for-byte with cryptographic hashes in `docs/contracts/v1-hardening-umbrella-plan.md`.
- [Premature DoD claim] → Clear disclaimer that pre-merge automated tests do not constitute live host acceptance (`test != Done`).

## Migration Plan

Deliver Wave 1 foundation code and test verification. Subsequent waves S02–S13 will be delivered in their respective follow-on PRs according to `docs/contracts/v1-hardening-umbrella-plan.md`. Do not archive this change until live verification is complete.
