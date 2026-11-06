# Spec Delta

## ADDED Requirements

### Requirement: Relayed replies SHALL record OmniRoute response anomalies
For every upstream attempt it relays, `verdict serve` SHALL record `omniroute_anomalies` on the outcome receipt: the anomaly names that the OmniRoute response anomalies requirement produces from that attempt's response headers. Repeated headers SHALL be kept, so a repeated marker is safe only when every value is safe. The relay SHALL also record `omniroute_anomalies` on the request's evidence outcome event for the attempt whose reply the client received, for buffered and streamed replies. Only the names `cache_hit`, `compressed` and `emergency_fallback` SHALL be stored. A header value SHALL NOT be stored in this field. When every marker is absent or safe, the field SHALL be an empty list. Recording SHALL NOT change the relayed reply, its status code, or the relay's retry decisions.

#### Scenario: Cached and fallback relayed reply
- **WHEN** a relayed chat completion reply carries `X-OmniRoute-Cache: HIT` and an `X-OmniRoute-Emergency-Fallback` value other than `false`
- **THEN** the client receives the upstream reply with its status code
- **AND** the outcome receipt and the evidence outcome event record `omniroute_anomalies` as `cache_hit` and `emergency_fallback`
- **AND** neither record contains the emergency-fallback header value

#### Scenario: Clean relayed reply
- **WHEN** a relayed reply carries `X-OmniRoute-Cache: MISS`, `X-OmniRoute-Cache-Hit: false`, `X-OmniRoute-Compression: off; source=request-header` and `X-OmniRoute-Emergency-Fallback: false`
- **THEN** the outcome receipt and the evidence outcome event record `omniroute_anomalies` as an empty list

#### Scenario: Streamed cached reply
- **WHEN** a streamed relayed reply carries `X-OmniRoute-Cache: HIT`
- **THEN** the finalized evidence outcome event and the outcome receipt record `omniroute_anomalies` as `cache_hit`

#### Scenario: Repeated cache marker
- **WHEN** a reply carries `X-OmniRoute-Cache` twice, once `MISS` and once `HIT`
- **THEN** `omniroute_anomalies` includes `cache_hit`
