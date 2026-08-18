# ADR-019: Runtime-negotiated passports for tools and protocol peers

- **Status:** Accepted — v1 contract implemented for #122
- **Date:** 2026-07-31
- **Related:** [ADR-010](ADR-010-fail-closed-capability-passports.md), [ADR-013](ADR-013-independent-protocol-surface-qualification.md), [ADR-014](ADR-014-tool-and-structured-output-qualification.md), [#122](https://github.com/mrnicholasbcarter-code/verdict-core/issues/122)

## Context

Model capability passports do not cover the runtime subjects a task actually
uses: tools, MCP servers/resources/prompts, A2A peers, ACP agents, skills, and
the transport/auth combination used to reach them. Installed or configured
metadata is useful inventory, but it does not prove a subject is reachable,
authenticated, schema-compatible, or able to complete the requested lifecycle.

## Decision

Verdict adds a versioned `RuntimeCapabilityPassport` contract. It records a
secret-free subject identity containing the subject kind and ID, provider,
protocol and version, transport, auth mode, scoped endpoint digest, and an
optional declared schema digest. The passport keeps three evidence maps apart:

- `declared`: inventory or manifest claims;
- `observed`: direct reachability or lifecycle observations;
- `negotiated`: direct schema/version/auth handshake evidence for a named
  capability.

Runtime admission requires a fresh direct `negotiated` observation. A claim or
ordinary observation alone cannot satisfy policy. Unsupported direct evidence
dominates optimistic negotiation, and expired, missing, inferred, or claimed
negotiation resolves to `unknown`. No protocol runtime, arbitrary binary
execution, credential storage, or provider-specific adapter is introduced by
this contract.

Policy can require runtime capabilities separately from model capabilities via
`required_runtime_capabilities`. Candidates carry scoped runtime passports;
policy only considers a passport whose scope matches the selected route
connection and never ranks around an unknown or unsupported runtime subject.

## Consequences

- MCP/A2A/ACP/tool/skill integrations share one evidence vocabulary without
  pretending their wire protocols are interchangeable.
- Transport and auth mode are part of the identity, so a qualified peer cannot
  be silently reused through a different connection.
- Future protocol adapters can emit negotiated evidence and receipts without
  changing policy semantics or exposing raw endpoints/secrets.
- The v1 contract does not claim support for any particular protocol runtime;
  unsupported host capabilities remain explicitly unavailable or unknown.

## Verification

`tests/test_runtime_passports.py` covers identity separation and redaction,
declaration/observation fail-closed behavior, direct negotiation admission,
negative precedence, expiry, strict parsing, canonical digest stability, and
JSON Schema validation. Existing protocol and tool qualification suites remain
independent and continue to produce evidence for this shared boundary.
