# Spec Delta

## Purpose

Verdict asks OmniRoute not to cache, compress or inject into the inference
requests whose replies it treats as evidence, and never counts a cached or
emergency-fallback probe reply as proof.

## ADDED Requirements

### Requirement: Verdict-owned inference requests SHALL carry the OmniRoute evidence opt-outs
Every inference request that Verdict sends to an OmniRoute gateway through the probe, prove-at-rest, eligibility health, acceptance tool check, census needle, direct gateway executor or relay upstream transport SHALL send `X-OmniRoute-No-Cache: true`, `X-OmniRoute-Compression: off` and `X-OmniRoute-No-Memory: true`. Each request SHALL send its own unique `X-Request-Id`. Caller-supplied extra headers SHALL NOT weaken these opt-outs. Non-inference requests, such as the relay's `GET /models`, SHALL NOT be changed.

#### Scenario: Two probe requests
- **WHEN** a probe transport sends two chat completion requests
- **THEN** both carry the three opt-out headers
- **AND** their `X-Request-Id` values differ

#### Scenario: A caller tries to switch the cache back on
- **WHEN** a caller passes `X-OmniRoute-No-Cache: false` as an extra header
- **THEN** the request still sends `X-OmniRoute-No-Cache: true`

#### Scenario: Relay model catalog
- **WHEN** the relay fetches the upstream model catalog
- **THEN** that request does not carry the opt-out headers

### Requirement: OmniRoute response anomalies SHALL be named
Verdict SHALL read OmniRoute response headers case-insensitively and name `cache_hit` for `X-OmniRoute-Cache: HIT` (or `X-OmniRoute-Cache-Hit: true`), `compressed` for an `X-OmniRoute-Compression` mode other than off, and `emergency_fallback` for an `X-OmniRoute-Emergency-Fallback` value other than false. A cache hit SHALL map to the reason `omniroute_cache_hit` and an emergency fallback to `omniroute_emergency_fallback`. A compressed reply alone SHALL have no such reason.

#### Scenario: Cached reply
- **WHEN** a reply carries `X-OmniRoute-Cache: HIT`
- **THEN** the anomalies include `cache_hit`
- **AND** the reason is `omniroute_cache_hit`

#### Scenario: Compression turned off
- **WHEN** a reply carries `X-OmniRoute-Compression: off; source=request`
- **THEN** no anomaly is named

#### Scenario: Compressed reply only
- **WHEN** a reply carries `X-OmniRoute-Compression: lite; source=config` and no other marker
- **THEN** the anomalies are `compressed`
- **AND** there is no proof-voiding reason

### Requirement: A cached or emergency-fallback probe reply SHALL NOT count as proof
A probe reply that OmniRoute marks as a cache hit or an emergency fallback SHALL NOT count as healthy, ready, liveness, tool or agentic proof. The probe SHALL fail closed with the named reason. Prove-at-rest probes SHALL write no route health for it, so prior proof ages out by its own TTL. The probe runner SHALL NOT count it toward quarantine.

#### Scenario: Cached liveness probe
- **WHEN** the probe runner receives a 200 reply marked as a cache hit
- **THEN** the observation is not ready
- **AND** its error class is `omniroute_cache_hit`
- **AND** the consecutive failure count does not grow

#### Scenario: Cached tool probe
- **WHEN** the prove-at-rest chat phase passes and the tool phase reply is marked as a cache hit
- **THEN** the full probe writes no health
- **AND** its no-write reason is `omniroute_cache_hit`

#### Scenario: Emergency fallback on an agentic turn
- **WHEN** an agentic probe turn is marked as an emergency fallback
- **THEN** no agentic capability entry is written for the route

#### Scenario: Acceptance tool check on a cached turn
- **WHEN** the acceptance tool check receives a reply marked as a cache hit on turn 1
- **THEN** the route outcome is `fail` with detail `turn1: omniroute_cache_hit`

#### Scenario: Eligibility health probe on a fallback reply
- **WHEN** the eligibility health probe receives a reply marked as an emergency fallback
- **THEN** the result is unhealthy with category `omniroute_emergency_fallback`
