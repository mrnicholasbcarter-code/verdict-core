## ADDED Requirements

### Requirement: In-band credit refusal detection

Verdict SHALL treat an HTTP 200 chat completion as a credit refusal when all of these hold:
the usage block reports zero completion tokens, the assistant message carries no tool call,
and the assistant text matches a not-free phrase. The not-free phrases SHALL include
"enough credits" and "top up". One shared detector SHALL make this decision for probes, the
relay and the acceptance serve check.

#### Scenario: Captured refusal shape

- **WHEN** a body has `completion_tokens` 0, no tool call, and the text "doesn't have enough credits. Please top up"
- **THEN** the detector reports a credit refusal

#### Scenario: A real answer quotes the phrase

- **WHEN** a body has the same text and `completion_tokens` greater than 0
- **THEN** the detector reports no refusal

#### Scenario: No usage evidence

- **WHEN** a body has the refusal text and no usage block, or carries a tool call
- **THEN** the detector reports no refusal

### Requirement: Probes record a refusal as a route-scoped payment failure

A probe exchange whose HTTP 200 body is a credit refusal SHALL be a failed exchange with the
category `payment_required`. It SHALL NOT count as chat, tool or agentic proof. The recorded
negative SHALL be route-scoped: it SHALL NOT stop the provider or cool its pool.

#### Scenario: Chat probe meets a refusal

- **WHEN** the chat turn of a probe receives a credit refusal
- **THEN** the exchange is not ok, its category is `payment_required`, and no tool turn is sent
- **AND** the probe result has `chat_ok` false and failure scope `route`

#### Scenario: Agentic transport meets a refusal

- **WHEN** the agentic transport receives a credit refusal
- **THEN** the exchange is not ok and its category is `payment_required`

### Requirement: Relay treats a buffered refusal as a failed attempt

When a buffered upstream response is HTTP 200 and its body is a credit refusal, the relay
SHALL count the attempt as failed. It SHALL record route-scoped payment feedback with
`capacity_evidence` `billing_402_metered`, and SHALL NOT write a pool or provider cooldown.
It SHALL try the next candidate within the existing attempt bound. When no candidate is left
it SHALL return HTTP 402 and SHALL NOT forward the refusal as a success.

#### Scenario: Another candidate is available

- **WHEN** the first attempt returns a credit refusal and a second admitted candidate exists
- **THEN** the relay sends the request to the second candidate and returns its reply
- **AND** the first route has a negative entry with category `payment_required`, failure scope `route` and `capacity_evidence` `billing_402_metered`

#### Scenario: No candidate is left

- **WHEN** the only candidate returns a credit refusal
- **THEN** the relay sends one request and returns HTTP 402

### Requirement: Passive traffic does not turn a refusal into liveness

A webhook completion that reports zero output tokens SHALL NOT be recorded as liveness.
Observed traffic SHALL NOT replace a payment negative with a healthy entry, even after the
negative's backoff has ended; only a probe may clear it.

#### Scenario: Zero-output completion

- **WHEN** a `request.completed` webhook event reports zero output tokens
- **THEN** the refresh records no liveness for it and counts it as `skipped_zero_output`

#### Scenario: Traffic after a billing negative

- **WHEN** a route's newest entry is an expired payment negative and newer HTTP 200 traffic is observed
- **THEN** the entry stays unhealthy and the outcome is `skipped_billing_negative`

### Requirement: Acceptance serve check fails on a refusal

The acceptance serve check SHALL fail when the reply to its request is a credit refusal.
The independent tool check and its precision floor are unchanged.

#### Scenario: Serve returns a refusal

- **WHEN** `verdict serve` answers the check request with HTTP 200 and a credit refusal body
- **THEN** the serve check does not pass and its reason names the credit refusal
