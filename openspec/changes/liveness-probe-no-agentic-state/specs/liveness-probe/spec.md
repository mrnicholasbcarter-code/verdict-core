## ADDED Requirements

### Requirement: A reply cut off by the liveness token limit is alive and flagged
The 1-token chat liveness probe SHALL count a reply as alive when it is an HTTP 200 chat completion
whose assistant message has empty or null text, whose `finish_reason` is `length` or `stop`, and
whose served identity verifies as the requested route with no proof-voiding anomaly. It SHALL flag
the reply as `empty_content`, and as `zero_output_tokens` when usage reports 0 completion tokens.

#### Scenario: Captured empty-content replies
- **WHEN** a route answers 200 with `finish_reason: "length"`, empty or null content and matching identity headers
- **THEN** the probe result is healthy and carries the `empty_content` flag

#### Scenario: Genuinely malformed reply
- **WHEN** the body is not JSON, has no choices, has a non-assistant role, an unusual finish reason, or no verified identity
- **THEN** the probe result is `malformed_response`

### Requirement: Liveness evidence never reads or writes agentic proof
A failed chat-liveness observation from the ladder confirmation or the worker launch probe SHALL
mark the route unhealthy for chat and SHALL leave the route's agentic and session-agentic proof
unchanged. The set of categories that preserve agentic proof for other callers SHALL NOT include
`malformed_response`.

#### Scenario: Ladder probe fails on a proven route
- **WHEN** the ladder's liveness probe records `malformed_response` for a route holding agentic proof
- **THEN** the route is unhealthy for chat and its `agentic_ok` and `agentic_checked_at` are unchanged

#### Scenario: Other callers keep today's behaviour
- **WHEN** a non-liveness caller records `malformed_response`
- **THEN** agentic proof is cleared as before
