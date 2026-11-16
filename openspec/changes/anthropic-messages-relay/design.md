# Design: anthropic messages relay

## Decision

The transport-reuse gate (Linear BOD-102, 2026-09-28) prefers gateway delegation over a second protocol stack in Core. OmniRoute (installed 3.8.x) already serves `POST /v1/messages` and translates Anthropic requests/responses to the selected provider format (`detectFormatFromEndpoint` returns `claude` for `/messages` paths; translators in `open-sse/translator/`). Therefore Verdict adds only the boundary it must own:

1. A new surface `messages` on the existing `_relay_completion` path, so every request passes the same admission, proof, dispatch, receipt, and evidence lifecycle as chat/responses.
2. Anthropic-specific wire handling that the shared path cannot infer: `system` field context injection, Anthropic SSE terminal semantics, and an Anthropic-shaped error envelope.
3. Harness + docs so Claude Code's base URL points at Verdict, not OmniRoute.

## Protocol identity

`protocol_for_surface("messages")` returns `anthropic.messages`. Route identity endpoints resolve to `<gateway>/v1/messages`. The protocol id is recorded on every attempt and receipt; a chat-only proof never satisfies a messages dispatch (the proof is bound to the scope computed from the request profile, which includes the surface via the forwarded payload shape).

## Request handling

- Body limits, JSON validation, and reserved-field rejection are shared with the existing relay.
- Task text extraction already handles Anthropic block content (`_task_text` reads `content` lists with `text` items).
- `request_features` gains Anthropic tool shapes: a `tools` entry with `name` (Anthropic) counts as tools; an `image` content block with `source.type == base64` counts as vision.
- Local fields (`_RELAY_LOCAL_FIELDS`) are stripped before forwarding, unchanged.
- The client-supplied `model` is replaced with the admitted route's model, unchanged.

## Context injection

For `surface="messages"`, the compiled pack is prepended to the top-level `system` field:

- `system` absent → set to the compiled prompt string.
- `system` string → prepend with a blank-line separator.
- `system` block list → prepend a `{type: text, text: <prompt>}` block.
- Any other shape → skip with reason `unsupported_system_shape` (fail closed, no injection claimed).
- The client's `messages` array is never rewritten.
- The envelope digest is the sha256 of the compiled prompt, same as other surfaces, so receipts stay comparable.

## Streaming

Anthropic SSE terminal semantics (Claude Code subset):

- A stream is a checked success only when a `message_stop` event has been seen after at least one `content_block_stop` (or a `message_delta` with `stop_reason`).
- `event:` lines are framing metadata; `data:` lines carry JSON events (`message_start`, `content_block_start`, `content_block_delta`, `content_block_stop`, `message_delta`, `message_stop`, `ping`, `error`).
- An `error` event, EOF before `message_stop`, or data after `message_stop` fails closed (`protocol_error` / `abrupt_eof`), exactly like chat/responses.
- Bytes pass through unchanged; the parser only observes.

## Errors

Denied, unauthenticated, and upstream-failure responses on the messages surface use the Anthropic error envelope `{"type": "error", "error": {"type": <type>, "message": <text>}}` so Claude Code renders them. Other surfaces keep their existing envelopes.

## Authentication

Existing bearer middleware applies. Additionally the messages surface accepts `x-api-key` equal to the server-owned token (Claude Code sends `ANTHROPIC_API_KEY` as `x-api-key`). Conflicting credentials (bearer and x-api-key both present and different) are rejected. `x-api-key` is never forwarded upstream; the upstream request carries only server-owned credentials.

## Harness

`verdict harness claude enable` also sets `env.ANTHROPIC_BASE_URL` to the Verdict origin root (no `/v1`; Claude Code appends `/v1/messages`) and `env.ANTHROPIC_AUTH_TOKEN` is never written — the existing token-env marker is reused. Certification notes state the Messages path is supported through the gateway, and overall parity may rise from partial only when the live health probe passes. `disable` restores the backup byte-for-byte, unchanged.

## Out of scope

- `/v1/messages/count_tokens` and Anthropic beta endpoints: fail closed (404), documented.
- Prompt caching, extended thinking translation, and full feature parity beyond what Claude Code sends.
- Any change to admission membership, ranking, or proof lifetimes.
