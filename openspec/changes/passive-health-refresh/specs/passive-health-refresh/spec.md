# Spec Delta: Passive health refresh

## ADDED Requirements

### Requirement: Passive traffic SHALL write chat-only liveness
The passive identify run with `--refresh-health-cache` SHALL record OmniRoute
health-matrix traffic, and verified webhook events when registered, as healthy
entries with `chat_ok` true and `tool_ok` false, tagged with `evidence_source`
`omniroute_traffic` or `omniroute_webhook`. It SHALL make no model calls and no
writes to OmniRoute. Passive traffic SHALL NOT grant tool or agentic proof.

#### Scenario: Recent traffic for a route
- **WHEN** the health matrix reports a request for a route inside the usable window
- **THEN** the cache holds a liveness entry whose `checked_at` is the OmniRoute `lastRequestAt`
- **AND** `tool_ok` is false

### Requirement: Newer and stronger evidence SHALL win
Passive traffic SHALL NOT overwrite an entry checked at or after the traffic time,
SHALL NOT shorten an unexpired negative, and SHALL NOT replace a healthy tool
proof that is still fresh or stale. Traffic older than the evidence lifetime plus
grace, or more than sixty seconds in the future, SHALL be skipped.

#### Scenario: Active negative
- **WHEN** a route has an unexpired negative entry and older or newer chat traffic arrives
- **THEN** the negative entry is kept unchanged

#### Scenario: Usable tool proof
- **WHEN** a route has a fresh healthy entry with `tool_ok` true
- **THEN** chat traffic leaves that entry unchanged

### Requirement: Doctor SHALL report the passive source honestly
Doctor SHALL fail the passive report row when the saved report has no
`health_refresh`, or when the refresh has source errors, model calls or gateway
writes. When the health cache has no fresh or stale route, doctor SHALL fail and
name `verdict-identify.service`, when it last refreshed, and its repair. Doctor
SHALL report the OmniRoute webhook as ok only when registered with an event in
the usable window, and otherwise as a warning that never fails doctor.

#### Scenario: Empty health cache
- **WHEN** the health cache has no fresh or stale route
- **THEN** the Health cache row fails and names the passive source and `systemctl --user start verdict-identify.service`

#### Scenario: Webhook not registered
- **WHEN** no OmniRoute webhook is registered
- **THEN** the OmniRoute webhook row is a warning naming `verdict identify register-webhook`
- **AND** doctor exit status is unaffected by that row
