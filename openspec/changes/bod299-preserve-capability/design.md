# Design

## Context

See proposal.md for motivation. The existing cache stores probe capability, session evidence and current health on the exact route; no independent store or new schema is needed.

## Goals / Non-Goals

**Goals:** Retain earlier valid no-answer capability evidence without renewing age or making a negative route live.

**Non-Goals:** Change session revocation, selection, dispatch authority, TTLs, caps, gateway configuration or historical cause attribution.

## Decisions

Reuse existing HealthEntry.agentic_ok and agentic_checked_at rather than add a second capability store; this keeps the existing serialization and merge authority. A recognized no-answer availability failure carries forward those fields and probe_class from a successful prior exact-route proof strictly older than the failure. Same-time conflicting evidence still fails closed under the existing negative-wins safety rule. It still creates a negative HealthEntry and records the current last_failure_at. Never refresh proof age during that carry-forward. Missing proof stays missing and expired proof stays expired.

An answered failure is not a no-answer availability result. Unknown/malformed/model-mismatch negatives retain the existing fail-closed reset. Existing session evidence and newer answered agentic-probe revocation logic stay unchanged.

## Risks / Trade-offs

Retained proof could be confused with readiness → keep negative health, failure timestamps and existing permit/liveness gates authoritative. Old proof could be renewed accidentally → preserve its original timestamp and verify the original expiry boundary.

## Security / trust implications

Retained proof is not dispatch authority. Ties, future evidence and answered/unknown/malformed/model-mismatch failures still fail closed; existing session denial and permit checks are unchanged.

## Routing / context / memory implications

Reuse exact-route HealthEntry fields and existing serialization/merge behavior. No routing policy, context pipeline or memory store changes; a sibling route remains untouched.

## Migration / rollback

No data migration is required. Existing JSON readers and writers remain compatible. Roll back the source correction if necessary without changing schema or renewing stored proof. Deployment, installed-main setup/doctor and BOD-439 operational acceptance remain OPEN.

## ADR impact

ADR-007 remains governing: identity, capability and current liveness are distinct; retained capability alone never promotes readiness. ADR-014 remains governing: an HTTP answer alone does not prove the required tool lifecycle; malformed or failed tool behavior does not grant capability. Reuse the existing JSON cache and serialization/merge path. This is a correction within those decisions, not a new architecture or data store. No ADR file, TTL, cap or independent 600-second permit bound changes.

## Verification

New regressions capture pass -> no-answer failure -> save/reload -> backoff expiry -> healthy single-call recovery, preserving original proof expiry. A stale peer's merge reloads authoritative prior proof without touching a sibling. Answered/unknown failures revoke durably; missing/expired proof cannot gain qualification. Existing session-denial and permit tests remain unchanged. Host acceptance, installed-main proof and independent parent verification remain outside this local slice.
