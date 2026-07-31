# ADR-020: Versioned Responses compatibility at the HTTP executor boundary

- **Status:** Accepted — v1 implementation for #125
- **Date:** 2026-07-31
- **Related:** [#125](https://github.com/mrnicholasbcarter-code/verdict-core/issues/125), [ADR-019](ADR-019-runtime-negotiated-passports.md)

## Context

Codex can send `prompt_cache_key` on the OpenAI Responses surface. Strict
non-Codex NVIDIA HTTP Responses routes reject that field even though the rest
of the request is valid. The compatibility behavior must be narrow enough that
it cannot silently change other providers, model IDs, or protocol surfaces.

## Decision

Apply a versioned rule immediately before the HTTP Responses transport. The v1
rule matches all three route identity dimensions exactly: provider `nvidia`,
one of the four qualified NVIDIA model IDs, and protocol `openai.responses`.
It removes only `prompt_cache_key`; `truncation`, `client_metadata`, tools,
streaming, and unknown request fields remain unchanged. The caller's payload is
never mutated.

The rule is represented in source and in the checked-in
`tests/fixtures/responses-compatibility-v1.json` service/config fixture. Its
version is carried in response metadata and privacy-safe execution evidence so
operators can distinguish adapted attempts after a restart or reload.

## HTTP versus WebSocket boundary

This compatibility rule is HTTP-only. Non-Codex NVIDIA IDs use the ordinary
`/responses` HTTP endpoint; they must not be sent through the Codex-only
WebSocket bridge. The relay's `openai.responses` route identity and the proxy's
HTTP `/responses` transport make that boundary explicit. WebSocket behavior is
not inferred or enabled by this rule.

## Consequences

- Matching is fail-closed and ambiguous rule sets are rejected.
- Deterministic compatibility-related 400/404/422 responses are returned
  unchanged and are not retried or failed over with the same request.
- Successful streaming and buffered Responses responses retain their upstream
  wire bytes and expose the applied rule version only through Verdict metadata.
- Adding another provider/model exception requires a new explicit rule and
  versioned fixture rather than broadening this NVIDIA rule.
