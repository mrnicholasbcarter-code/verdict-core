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

### Requirement: OmniRoute response anomalies SHALL be named and SHALL fail closed
Verdict SHALL match OmniRoute marker header names case-insensitively. A marker SHALL be safe only when it is absent or carries the exact value OmniRoute 3.8.53 sends when nothing happened: `X-OmniRoute-Cache: MISS`, `X-OmniRoute-Cache-Hit: false`, an `X-OmniRoute-Compression` value whose mode (the text before the first `;`) is exactly `off`, and `X-OmniRoute-Emergency-Fallback: false`. Any other value, including empty, `no`, `0` and unknown text, SHALL be an anomaly: `cache_hit` for either cache marker, `compressed` for compression, `emergency_fallback` for the fallback marker. A repeated marker SHALL be safe only when every value is safe. A cache hit SHALL map to the reason `omniroute_cache_hit` and an emergency fallback to `omniroute_emergency_fallback`. A compressed reply alone SHALL have no such reason.

#### Scenario: Cached reply
- **WHEN** a reply carries `X-OmniRoute-Cache: HIT`
- **THEN** the anomalies include `cache_hit`
- **AND** the reason is `omniroute_cache_hit`

#### Scenario: A clean OmniRoute reply
- **WHEN** a reply carries `X-OmniRoute-Cache: MISS`, `X-OmniRoute-Cache-Hit: false` and `X-OmniRoute-Compression: off; source=request-header`
- **THEN** no anomaly is named

#### Scenario: Unknown marker value
- **WHEN** a reply carries `X-OmniRoute-Cache`, `X-OmniRoute-Cache-Hit` or `X-OmniRoute-Emergency-Fallback` with a value such as `no`, `0`, an empty value or arbitrary text
- **THEN** the matching anomaly is named

#### Scenario: Unknown compression mode
- **WHEN** a reply carries `X-OmniRoute-Compression` whose mode is `no`, `0`, empty, `none`, `false`, `OFF` or arbitrary text
- **THEN** the anomalies include `compressed`

#### Scenario: Compressed reply only
- **WHEN** a reply carries `X-OmniRoute-Compression: lite; source=config` and no other marker
- **THEN** the anomalies are `compressed`
- **AND** there is no proof-voiding reason

### Requirement: A cached or emergency-fallback probe reply SHALL NOT count as proof
A probe reply that OmniRoute marks as a cache hit or an emergency fallback SHALL NOT count as healthy, ready, liveness, tool or agentic proof. The probe SHALL fail closed with the named reason. Prove-at-rest probes SHALL decide this before any health or capability persistence: they SHALL write no route health or capability entry for it and SHALL NOT refresh existing proof, which ages out by its own TTL. The probe runner SHALL NOT count it toward quarantine.

#### Scenario: Cached reply on a route with earlier proof
- **WHEN** a prove-at-rest cycle re-probes a route whose earlier proof is still usable but due for refresh, and the chat reply is marked as a cache hit or emergency fallback
- **THEN** the stored entry for the route is unchanged
- **AND** no tool request is sent

#### Scenario: Agentic screen with a fallback turn
- **WHEN** an identify screen's third agentic turn is marked as an emergency fallback
- **THEN** the screen result is `omniroute_emergency_fallback`
- **AND** the screen never enters the health-cache write path for that route

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
