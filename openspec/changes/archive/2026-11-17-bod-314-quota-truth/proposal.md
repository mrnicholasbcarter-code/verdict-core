# BOD-314 quota truth (phase 1)

## Problem
A configured, active connection is not remaining usage quota. A 100% display
must not override a current observed account 429 or provider cooldown.

## Scope
Read-only diagnosis is captured separately at `/tmp/cc-quota-diagnosis/REPORT.md`.
The reported 100% UI is not present in this Verdict checkout; no external
OmniRoute dashboard change is included.

Extend the existing verified-models projection with `availability_evidence`.
Each connection observation keeps provider and scope separate: account, pool,
provider or model. Records expose configured, active, nullable quota percent
and window, observed cooldown, source, timestamp and age. Missing quota is
UNKNOWN; quotaWindowThresholds are policy and cannot provide usage.

## Rules
- Current observed cooldown beats percentage and healthy cached proof.
- A timestamped 429 without an explicit cooldown uses the existing health-cache
  RATE_LIMIT_SECONDS window; a newer route success supersedes that error.
- Expired cooldowns remain historical evidence and do not block admission.
- Scoped blocks reuse existing account/pool path exhaustion. A usable sibling
  account can serve an unbound route; it cannot serve an explicitly bound route.
- Provider errors are reduced to a category. Account identifiers are hashed at
  display. Existing evidence stores remain authoritative; no new store exists.
- No cooldown clearing, service restarts, model calls or dashboard redesign.

## Validation
Recorded redacted gateway fixture and offline focused tests. Stable JSON key
and privacy audits are extended, not relaxed. Full suite and release gates
remain integration-branch work.
