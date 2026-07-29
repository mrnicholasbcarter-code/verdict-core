# ADR-012: Consent and budgets are mandatory for live qualification probes

- **Status:** Accepted — implemented in #170
- **Date:** 2026-07-29
- **Related:** [ADR-001](ADR-001-evidence-ledger.md), [ADR-007](ADR-007-omniroute-catalog-qualification.md), [ADR-010](ADR-010-fail-closed-capability-passports.md), [ADR-011](ADR-011-omniroute-catalog-qualification-baseline.md), [#106](https://github.com/mrnicholasbcarter-code/verdict-core/issues/106), [#170](https://github.com/mrnicholasbcarter-code/verdict-core/issues/170)

## Context

Catalog membership is identity and claimed-metadata evidence, while a liveness
probe is an external side effect against a provider route. Unbounded or
implicit probes can spend provider resources, expose credentials through
diagnostics, and turn cancellation or partial results into optimistic
readiness. Hermetic transports must remain usable in CI without hosted
credentials or network access.

## Decision

Verdict requires explicit operator consent before a CLI or catalog operation
constructs and executes a live network probe. Injected transports remain
available for hermetic tests and are marked non-live in run diagnostics.

Each run names its provider and applies fail-closed request, fixed one-token,
aggregate response-byte, and wall-clock budgets. Duplicate IDs, cooldowns,
quarantined routes, cancellation, and budget exhaustion never launch another
transport call. Cancellation produces non-ready `cancelled` observations.

The OpenAI-compatible transport bounds response reads before JSON parsing.
Responses, prompts, credentials, and query-bearing URLs are never retained in
observations or diagnostics. Versioned diagnostics contain only sanitized
accounting: provider, consent/live state, budgets, counts, statuses, hashes
owned by the caller, and UTC timing.

Probe liveness remains separate from catalog qualification, capability
passports, protected-work eligibility, and future strength qualification.

## Consequences

### Positive

- Live provider side effects require an auditable operator choice.
- CI can exercise the full scheduler with injected transports.
- Request, token, byte, timeout, cancellation, cooldown, and quarantine
  decisions are bounded and replayable without raw provider data.
- Oversized, malformed, cancelled, and partial responses fail closed.

### Negative

- A caller that wants live qualification must pass an explicit consent flag.
- Conservative aggregate byte accounting can reduce concurrency for providers
  with unknown response sizes.
- Probe diagnostics are not a capability receipt or durable qualification
  authority.

## Verification

`tests/test_probes.py` covers consent, hermetic execution, request/token/byte
budgets, cancellation, cooldown/quarantine, response-size limits, redaction,
and diagnostics. `tests/test_cli_inprocess.py` covers CLI consent and live
diagnostic output. Catalog probe tests continue to use injected transports and
remain network-free.
