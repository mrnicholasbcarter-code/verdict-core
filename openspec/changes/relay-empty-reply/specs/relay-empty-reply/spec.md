## ADDED Requirements

### Requirement: Truncated-empty reply detection

Verdict SHALL treat an HTTP 200 buffered chat completion as a truncated-empty reply
when all of these hold: `finish_reason` is `"length"`, the assistant message carries
no tool calls, and the content field is absent or empty. One shared detector
(`is_truncated_empty_reply`) SHALL make this decision.

#### Scenario: Captured production shape

- **WHEN** a body has `finish_reason: "length"` and `content: ""`
- **THEN** the detector reports a truncated-empty reply

#### Scenario: Null content

- **WHEN** a body has `finish_reason: "length"` and `content: null`
- **THEN** the detector reports a truncated-empty reply

#### Scenario: Partial answer with non-empty content

- **WHEN** a body has `finish_reason: "length"` and a non-empty content field
- **THEN** the detector reports no truncated-empty reply

#### Scenario: Tool call with no text content

- **WHEN** a body has `finish_reason: "tool_calls"` and `content: null` with tool_calls present
- **THEN** the detector reports no truncated-empty reply

#### Scenario: finish_reason is not length

- **WHEN** a body has `finish_reason: "stop"` and empty content
- **THEN** the detector reports no truncated-empty reply

### Requirement: Relay treats a truncated-empty reply as a failed attempt

When a buffered upstream response is HTTP 200 and its body is a truncated-empty reply,
the relay SHALL count the attempt as failed with failure_class `"truncated_empty_reply"`.
It SHALL try the next admitted candidate within the existing attempt bound. It SHALL NOT
write a route health negative. When no candidate is left it SHALL return an error
response with code `"truncated_empty_reply"` and SHALL NOT forward the empty 200 as a
success.

#### Scenario: Another candidate is available

- **WHEN** the first attempt returns a truncated-empty reply and a second admitted candidate exists
- **THEN** the relay sends the request to the second candidate and returns its reply
- **AND** the first attempt is recorded with `failure_class: "truncated_empty_reply"` and `outcome: "error"`
- **AND** no route health negative is written for the first route

#### Scenario: All candidates return truncated-empty

- **WHEN** every admitted candidate returns a truncated-empty reply
- **THEN** the relay returns an error response with code `"truncated_empty_reply"`
- **AND** does not return HTTP 200 with empty content
- **AND** the attempt count is bounded by the admitted candidate pool

#### Scenario: Idempotency relaxed after truncated-empty

- **WHEN** a truncated-empty reply is received
- **THEN** `rejected_without_output` is set for the transition edge, relaxing the
  idempotency-key requirement for the next attempt (identical to the inband-credit-refusal
  pattern)
