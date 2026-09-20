> **Current boundary:** the obsolete `verdict.swarm_observability` telemetry sink
> was deleted with the swarm architecture. Verdict does not currently ship a
> general-purpose telemetry emitter or a telemetry-consent CLI switch. Decision
> and outcome logs are local execution receipts, not remote telemetry.

# Telemetry Consent Policy

## What telemetry contains

Telemetry is limited to operational, aggregate signals needed to explain and
measure execution: event type, correlation identifiers, task or runtime
identifiers, bounded timing/cost counters, status, and other fields explicitly
allowed by the relevant adapter contract. Prompt text, model output, tool
arguments, credentials, API keys, cookies, authorization headers, and private
keys are not telemetry fields.

Telemetry records are redacted before they are written to the local JSONL sink.
The sink is an observability aid; it is not a source of routing or security
authority.

## Consent states for any future telemetry sink

Telemetry is **opt-in**:

- **Opt-out / no consent (default):** no telemetry event is written or
  transmitted. The sink may be constructed, but an emit request is discarded
  before it reaches the output file.
- **Opt-in / explicit consent:** the caller must pass an explicit boolean
  consent decision when constructing the telemetry sink. Events are written to
  the configured local sink after sensitive-value redaction.

Consent is not inferred from the presence of a configuration file, an API key,
a provider selection, or a prior unrelated permission. A caller may revoke
consent by constructing a sink without consent; subsequent events are not
emitted by that sink.

## Verification

There is no current `tests/privacy/test_telemetry_consent.py`; documentation must
not claim that deleted coverage is a live release gate. The present regression
gate is `tests/test_bod17_obsolete_architecture_absent.py`, which ensures the
removed swarm telemetry modules are not reintroduced. If a canonical telemetry
sink is added, it must add blocking tests that prove default opt-out emits zero
records and explicit opt-in emits only redacted operational fields before this
document may claim an implemented consent surface.
