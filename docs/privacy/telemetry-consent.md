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

## Consent states

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

The blocking tests are in `tests/privacy/test_telemetry_consent.py`. Run them
with:

```bash
uv run pytest tests/privacy/test_telemetry_consent.py -v
```

The tests assert zero output in the default opt-out state and one redacted
operational event in the explicit opt-in state.
