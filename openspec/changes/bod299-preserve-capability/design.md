# Design

Reuse existing HealthEntry.agentic_ok and agentic_checked_at. A recognized no-answer availability failure carries forward those fields and probe_class from a successful prior exact-route proof strictly older than the failure. Same-time conflicting evidence still fails closed under the existing negative-wins safety rule. It still creates a negative HealthEntry and records the current last_failure_at. Never refresh proof age during that carry-forward. Missing proof stays missing and expired proof stays expired.

An answered failure is not a no-answer availability result. Unknown/malformed/model-mismatch negatives retain the existing fail-closed reset. Existing session evidence and newer answered agentic-probe revocation logic stay unchanged.

## ADR disposition

ADR-007 remains governing: identity, capability and current liveness are distinct; retained capability alone never promotes readiness. ADR-014 remains governing: an HTTP answer alone does not prove the required tool lifecycle; malformed or failed tool behavior does not grant capability. Reuse the existing JSON cache and serialization/merge path. This is a correction within those decisions, not a new architecture or data store. No ADR file, TTL, cap or independent 600-second permit bound changes.

## Verification

New regressions capture pass -> no-answer failure -> save/reload -> backoff expiry -> healthy single-call recovery, preserving original proof expiry. A stale peer's merge reloads authoritative prior proof without touching a sibling. Answered/unknown failures revoke durably; missing/expired proof cannot gain qualification. Existing session-denial and permit tests remain unchanged. Host acceptance, installed-main proof and independent parent verification remain outside this local slice.
