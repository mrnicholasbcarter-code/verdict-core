# BOD-102: governed Anthropic Messages boundary

## Intent and authority

Claude Code must reach the same fail-closed Verdict control plane as OpenAI clients. The user authorizes autonomous engineering decisions, implementation, independent review, and merging only after proof. This design implements the BOD-102 issue scope recovered from the previous session and the user's current explicit acceptance criteria. Live Linear reconciliation remains pending connector access; delivery status must not be inferred from the recovered snapshot.

BOD-104 owns strategy and execution-path selection; BOD-55 owns bounded recovery; BOD-67 owns dispatch; BOD-89 owns canonical proof. Context compilation, security, qualification and admission remain in Verdict. A gateway is optional transport and cannot select a different model, bypass admission or fabricate provenance.

## Transport-reuse decision

Three approaches were considered:

1. Implement a complete Anthropic-to-provider translator inside Core. Rejected: duplicates a substantial provider protocol stack and expands the maintenance/security boundary.
2. Point Claude Code directly at a gateway after a setup-time check. Rejected: subsequent requests bypass Verdict admission, context and receipts.
3. Add an Anthropic surface to the existing governed HTTP relay. Selected: Core intercepts every request and transparently delegates the Messages wire protocol to a configured compatible upstream. The same integration works without OmniRoute; no gateway router is an authority.

The supported execution subset is Anthropic Messages with text, client tool definitions and tool-use/result blocks, buffered JSON and SSE. Preserve compatible extension fields and original response bytes. Explicitly deny unsupported capability requirements; never silently strip them or label OpenAI qualification as Messages qualification. Full provider feature parity is outside BOD-102.

## Core responsibilities

- Expose POST `/v1/messages` through the same authenticated relay, body limits, request/correlation IDs, authoritative routing, named drops and evidence lifecycle.
- Support Claude Code's documented bearer authentication. If `x-api-key` is accepted for this surface, compare it only with the server-owned Verdict token, reject conflicting credentials, and never forward it upstream. Existing surfaces keep their authentication contracts.
- Route identity uses `anthropic.messages` and the configured `/messages` endpoint. A client-supplied model is a request preference, not execution authority; the upstream model is the selected concrete route.
- Qualify and confirm the actual Messages protocol and required tool/stream capabilities. A chat-only passport or successful chat probe is insufficient. Reuse existing qualification storage and gate authority with protocol-bound evidence; do not create an independent permissive gate.
- Preserve user messages, tool schemas, tool results and supported request extensions. Strip Verdict-local controls before forwarding. Extract task text and capability requirements from Anthropic blocks without confusing tool results with instructions or ignoring tool use for retry safety.
- Inject hydrated context into the top-level Anthropic `system` string/block array, preserving existing system content and the unchanged user messages. Record the actual injected envelope digest using the existing injection receipt. Never insert a `role: system` Messages item.
- Send only explicitly selected protocol headers (version and supported beta semantics), server-owned upstream credentials, and safe idempotency metadata. Caller credentials, cookies and arbitrary headers cannot cross this boundary.
- Preserve upstream JSON/error bodies and SSE bytes. Validate `message_stop` as successful stream termination, reject malformed/truncated streams and error events as failures, and close upstream resources on completion, error or cancellation. No retry after emitted bytes; tool-bearing requests retain conservative retry safety.
- Correlate decision, execution outcome, route identity and receipts. Missing cost/route evidence remains unknown. Redact secrets and do not publish task contents in evidence.

## Harness and documentation

Update the Claude harness integration to the real Anthropic base URL path, preserving backup/rollback and never writing token values. Keep certification truthful: transport support alone is not a live passport or an installed/authenticated working Claude session. Document the supported subset, upstream requirements, environment variable names, qualification procedure, and fail-closed diagnostics.

## Validation and completion boundary

Use TDD for buffered and streaming requests, protocol-bound route/passport rejection, missing/stale confirmation, malformed bodies, auth failures/conflicts, local-field/header stripping, selected-model enforcement, context digest identity, tool round trips, stream error/truncation/cancellation, bounded retry and evidence redaction. Include a real IntelligenceService integration path, not only a fixed-decision stub. Keep OpenAI regression tests passing.

Run targeted and canonical full proof, independent review, PR CI, merge, and post-merge proof. A real Claude Code session is attempted only with an already available no-cost authorized route and credentials; do not spend money. If unavailable, retain an explicit live-proof blocker and do not claim BOD-102 fully Done from mocks. This does not prevent shipping independently verified implementation with its limitation stated.
