# Telemetry Consent Policy

## Current boundary

Verdict does not currently ship a general-purpose telemetry emitter or a
telemetry-consent CLI switch. Routing evidence and orchestration receipts are
local execution records, not remote telemetry.

## Requirements for any future telemetry sink

Telemetry must be limited to operational, aggregate signals needed to explain
and measure execution: event type, bounded correlation or runtime identifiers,
bounded timing or cost counters, status, and fields explicitly allowed by the
adapter contract. Prompt text, model output, tool arguments, credentials, API
keys, cookies, authorization headers, and private keys are not telemetry
fields.

Any future telemetry record must be redacted before it is written or
transmitted. Such a sink would be an observability aid, not a source of routing
or security authority.

## Consent states for any future telemetry sink

Telemetry must be opt-in:

- **No consent (default):** no telemetry event is written or transmitted.
- **Explicit consent:** the caller supplies an explicit consent decision to the
  telemetry sink. Only redacted, allowed operational fields may be emitted to
  its configured destination.

Consent must not be inferred from a configuration file, API key, provider
selection, or an unrelated permission. Revocation must stop subsequent events
from that sink.

## Verification before implementation claims

This policy does not describe an implemented telemetry surface. A future
canonical sink must add blocking tests showing that the default emits zero
records and explicit consent emits only redacted allowed fields before this
document can claim implementation.
