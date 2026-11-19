## MODIFIED Requirements

### Requirement: A reply cut off by the liveness token limit is alive and flagged
The 1-token chat liveness probe SHALL count a reply as alive only when it is an HTTP 200 chat
completion whose top-level `error` is absent or null, exactly one choice whose `finish_reason` is `length`,
an assistant message which has no refusal and no tool-call payload of any kind (`refusal`,
`tool_calls` and the legacy `function_call` each absent or null), whose text is one of the two
shapes below, and whose served identity verifies as the requested route with no proof-voiding
anomaly. The first shape is a `content` key that is present and null, empty or whitespace-only;
the probe SHALL flag it as `empty_content`. The second shape is an absent `content` key; the
probe SHALL accept it only when `usage.completion_tokens` is an integer of 0 or more and
`reasoning` and `reasoning_content` are each absent, null or blank, and SHALL flag it as
`missing_content`, never as `empty_content`. For both shapes the probe SHALL add
`zero_output_tokens` when usage reports 0 completion tokens. Any condition that cannot be read
SHALL leave the result `malformed_response`.

#### Scenario: Captured empty-content replies
- **WHEN** a route answers 200 with `finish_reason: "length"`, empty or null content and matching identity headers
- **THEN** the probe result is healthy and carries the `empty_content` flag
- **AND** it does not carry the `missing_content` flag

#### Scenario: Captured missing-content reply
- **WHEN** a route answers 200 with one choice, `finish_reason: "length"`, an assistant message that holds only `role`, 1 completion token and matching identity headers
- **THEN** the probe result is healthy and carries the `missing_content` flag
- **AND** it does not carry the `empty_content` flag

#### Scenario: Missing content without a readable token count
- **WHEN** the message has no content key and usage is absent, or its completion token count is null, negative or not an integer
- **THEN** the probe result is `malformed_response`

#### Scenario: Missing content with an unreadable reasoning field
- **WHEN** the message has no content key and `reasoning` or `reasoning_content` holds a value that is neither null nor a string
- **THEN** the probe result is `malformed_response`

#### Scenario: Missing content that is not a truncated completion
- **WHEN** the message has no content key and the finish reason is `stop`, or there is a refusal, tool calls, a function call, a top-level error, zero or two choices, or a non-assistant role
- **THEN** the probe result is `malformed_response`

#### Scenario: Missing content without a verified identity
- **WHEN** the message has no content key and the served identity is absent or names another provider or model
- **THEN** the probe result is `malformed_response`

#### Scenario: Genuinely malformed reply
- **WHEN** the body is not JSON, has no choices or more than one, has a non-assistant role, a finish reason other than `length` (including `stop`), a refusal, tool calls, a top-level error, non-empty or non-string content, or no verified identity
- **THEN** the probe result is `malformed_response`
