# Spec Delta

## Purpose

Provides distinct classification for 429 responses: quota exhaustion (no retry until
quota resets) vs rate limiting (bounded retry with Retry-After header).

## ADDED Requirements

### Requirement: 429 quota exhaustion SHALL prevent retry loops

When a 429 response indicates quota exhaustion (via response body or code), the route
SHALL transition to `QUOTA_EXHAUSTED` availability state and SHALL NOT retry until
quota resets.

#### Scenario: 429 quota exhausted response
- **WHEN** provider returns 429 with quota-exhausted indicator in body/code
- **THEN** route state becomes `QUOTA_EXHAUSTED`
- **AND** no retry occurs until quota state changes

### Requirement: 429 rate limiting SHALL honor Retry-After with bounds

When a 429 response indicates rate limiting (Retry-After or reset header present),
the route SHALL transition to `RATE_LIMITED` state with cooldown derived from the
header, clamped to safe bounds (min 1s, max 300s).

#### Scenario: 429 with Retry-After seconds
- **WHEN** provider returns 429 with `Retry-After: 60` (seconds)
- **THEN** route state becomes `RATE_LIMITED` with 60s cooldown

#### Scenario: 429 with Retry-After HTTP-date
- **WHEN** provider returns 429 with `Retry-After: Wed, 25 Sep 2026 06:00:00 GMT`
- **THEN** route state becomes `RATE_LIMITED` with cooldown = (date - now) clamped

#### Scenario: 429 rate limit with malicious Retry-After
- **WHEN** provider returns 429 with negative, huge, or garbage Retry-After value
- **THEN** cooldown is clamped to min 1s, max 300s

#### Scenario: 429 rate limit without Retry-After header
- **WHEN** provider returns 429 without Retry-After or reset header
- **THEN** route state becomes `RATE_LIMITED` with default bounded cooldown
