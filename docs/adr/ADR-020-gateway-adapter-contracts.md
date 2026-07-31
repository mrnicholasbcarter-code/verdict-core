# ADR-020: Provider-neutral gateway adapter contracts

- **Status:** Accepted — v1 contract implemented for #120
- **Date:** 2026-07-31
- **Related:** [ADR-010](ADR-010-fail-closed-capability-passports.md), [ADR-019](ADR-019-runtime-negotiated-passports.md), [#120](https://github.com/mrnicholasbcarter-code/verdict-core/issues/120)

## Context

Verdict needs to interoperate with multiple gateways without making gateway
configuration, credentials, undocumented databases, or private APIs part of
the policy kernel. Existing transports and probes provide useful seams, but
each adapter must expose the same explicit evidence boundary.

## Decision

Verdict defines a versioned, provider-neutral adapter contract. An adapter
manifest declares its protocol, implementation identity, supported/unsupported/
unknown capabilities, discovery metadata, and telemetry allowlist. Requests
preserve the caller's requested alias and protocol; translated requests remain
secret-free and do not expose adapter-local URLs or headers.

Adapters may provide discovery, request translation, route attestation,
streaming, cancellation, normalized failures, and allowlisted telemetry. A
conformance runner exercises these methods with bounded synthetic data and
produces a deterministic report. Missing or unknown capabilities never become
permission; optional capabilities must be explicitly declared unsupported to
pass the compatibility check.

## Consequences

- Generic OpenAI-compatible, OmniRoute, and future gateway adapters can share
  one compatibility harness without sharing implementation details.
- Resolved and actually served route identities remain distinct and can be
  bound to the runtime passports and policy gates.
- Credentials, raw headers, endpoints, prompts, responses, and private gateway
  state remain outside the contract and are rejected at the boundary.
- The v1 slice does not implement a gateway runtime, request retries, provider
  authentication, streaming transport, or configuration management.

## Verification

`tests/test_gateway_adapters.py` covers canonical serialization, capability
negotiation, secret and unknown-field rejection, telemetry allowlisting, route
attestation, and conformance failure reporting. The JSON Schema is packaged in
both source and wheel locations and is checked by the contract validation
script.
