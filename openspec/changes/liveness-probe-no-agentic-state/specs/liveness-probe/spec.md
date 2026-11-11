## ADDED Requirements

### Requirement: A reply cut off by the liveness token limit is alive and flagged
The 1-token chat liveness probe SHALL count a reply as alive only when it is an HTTP 200 chat
completion whose top-level `error` is absent or null, exactly one choice whose `finish_reason` is `length`,
an assistant message whose `content` is present and null, empty or whitespace-only and which has
no refusal and no tool-call payload of any kind (`refusal`, `tool_calls` and the legacy
`function_call` each absent or null), and whose served identity verifies as the
requested route with no proof-voiding anomaly. It SHALL flag the reply as `empty_content`, and as
`zero_output_tokens` when usage reports 0 completion tokens.

#### Scenario: Captured empty-content replies
- **WHEN** a route answers 200 with `finish_reason: "length"`, empty or null content and matching identity headers
- **THEN** the probe result is healthy and carries the `empty_content` flag

#### Scenario: Genuinely malformed reply
- **WHEN** the body is not JSON, has no choices or more than one, has a non-assistant role, a finish reason other than `length` (including `stop`), a refusal, tool calls, a top-level error, non-empty or non-string content, a missing content key, or no verified identity
- **THEN** the probe result is `malformed_response`

### Requirement: Liveness evidence never reads or writes agentic proof
A failed chat-liveness observation from the ladder confirmation or the worker launch probe SHALL
mark the route unhealthy for chat and SHALL leave the route's agentic proof, session-agentic
proof and `tool_ok` unchanged. The set of categories that preserve agentic proof for other callers SHALL NOT include
`malformed_response`.

#### Scenario: Ladder probe fails on a proven route
- **WHEN** the ladder's liveness probe records `malformed_response` for a route holding agentic proof
- **THEN** the route is unhealthy for chat and its `agentic_ok`, `agentic_checked_at` and `tool_ok` are unchanged
- **AND** no reader that requires a healthy, fresh or chat-ok entry selects it

#### Scenario: Other callers keep today's behaviour
- **WHEN** a non-liveness caller records `malformed_response`
- **THEN** agentic proof is cleared as before
