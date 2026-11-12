# relay-anthropic-messages

## ADDED Requirements

### Requirement: Messages requests use the governed relay

`POST /v1/messages` SHALL be served by the same relay path as the OpenAI surfaces: body limits, JSON validation, reserved-field rejection, authoritative routing, admission narrowing, route-bound proof enforcement, evidence lifecycle, and receipts. A denied decision, a missing or stale proof, or an empty attempt set SHALL fail closed before any upstream byte is sent. The selected model SHALL replace the client-supplied model, and Verdict-local fields SHALL be stripped before forwarding.

#### Scenario: Denied decision sends nothing upstream

- **WHEN** a messages request is routed to a denied decision
- **THEN** the response is an error and no upstream request is made

#### Scenario: Missing proof fails closed

- **WHEN** the selected route has no fresh route-bound admission proof
- **THEN** the response is an error naming the denial and no upstream request is made

### Requirement: Protocol identity is anthropic.messages

The messages surface SHALL record protocol `anthropic.messages` on every attempt and receipt, and SHALL forward to the upstream `/v1/messages` endpoint. A chat or responses proof SHALL NOT authorize a messages dispatch.

#### Scenario: Forwarded path

- **WHEN** an admitted messages request is dispatched
- **THEN** the upstream request targets `/v1/messages` with the admitted model and protocol `anthropic.messages`

### Requirement: Context packs inject into the Anthropic system field

A hydrated or partial context pack SHALL be prepended to the top-level `system` field: a string is prepended, a block list gains a leading text block, and an absent field becomes the prompt. The client `messages` array SHALL NOT be rewritten. An unsupported `system` shape SHALL skip injection with a recorded reason and no claimed digest.

#### Scenario: String system preserved

- **WHEN** the request carries `system` as a string and a hydrated pack exists
- **THEN** the forwarded `system` starts with the compiled prompt followed by the original string, and `messages` is unchanged

#### Scenario: Unsupported shape skips

- **WHEN** `system` is neither a string nor a block list
- **THEN** injection is skipped with a recorded reason and the receipt claims no injection

### Requirement: Anthropic streams terminate on message_stop

A streamed messages response SHALL be a checked success only when a `message_stop` event follows content. End of stream before `message_stop`, an `error` event, or data after `message_stop` SHALL fail closed. Original bytes SHALL pass through unchanged.

#### Scenario: Clean stop succeeds

- **WHEN** the upstream stream ends with `message_stop` after content
- **THEN** the outcome is success and the client receives the original bytes

#### Scenario: Truncated stream fails closed

- **WHEN** the upstream stream ends without `message_stop`
- **THEN** the outcome is an error and no success receipt is written

### Requirement: Messages errors use the Anthropic envelope

Error responses on the messages surface SHALL use `{"type": "error", "error": {"type": ..., "message": ...}}`. Chat and responses error envelopes SHALL remain unchanged.

#### Scenario: Unauthenticated messages request

- **WHEN** a messages request lacks valid credentials
- **THEN** the body is an Anthropic error envelope

### Requirement: Claude Code base URL points at Verdict

The Claude harness enable command SHALL set `ANTHROPIC_BASE_URL` to the Verdict origin root without a `/v1` suffix and SHALL refuse an OmniRoute-looking URL unless forced. It SHALL NOT write token values. Disable SHALL restore the prior settings byte-for-byte. Documentation SHALL describe Verdict, not OmniRoute, as the Claude Code base URL.

#### Scenario: Enable writes the Verdict root

- **WHEN** the harness is enabled against a healthy Verdict
- **THEN** `ANTHROPIC_BASE_URL` is the Verdict origin root and no token value is written
