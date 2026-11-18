# Capability passports

Capability passports are Verdict's fail-closed evidence boundary for model
qualification. A passport belongs to one exact executable route, identified by
gateway, provider, connection/account class, endpoint, protocol surface, model
ID, and optional model revision. Model aliases that use different connections
or protocol surfaces therefore receive different route keys. <!-- claim:C-051 -->

Version 1 intentionally implements a narrow slice of issue #106:

- three-valued capability states: `supported`, `unsupported`, and `unknown`; <!-- claim:C-052 -->
- separate claimed and observed evidence; <!-- claim:C-053 -->
- source, observation time, expiry, confidence, evidence digest, and limitations
  on every signal; <!-- claim:C-054 -->
- canonical serialization and an integrity digest; and <!-- claim:C-055 -->
- hard-requirement evaluation that accepts only fresh observed support. <!-- claim:C-056 -->

A catalog claim never satisfies a hard requirement. <!-- claim:C-057 --> Missing, expired, malformed,
or claim-only evidence resolves to `unknown`. <!-- claim:C-058 --> A fresh failed observation remains
authoritative over a positive catalog claim. <!-- claim:C-059 --> Passport expiry also makes every <!-- claim:C-060 -->
capability unknown.

```python
from datetime import datetime, timezone

decision = passport.resolve("tools", at=datetime.now(timezone.utc))
if not decision.admitted:
    print(decision.reason)
```

The public JSON Schema is
`verdict/schemas/capability-passport.v1.json`. The schema and Python parser are
strict: unknown fields and optimistic boolean capability shortcuts are rejected.

The design decision is recorded in
`docs/adr/ADR-010-fail-closed-capability-passports.md`.

This slice does not implement probe scheduling, task-strength measurement,
durable receipt storage, protocol translation, retry/fallback legality, or
promotion. Those layers consume this contract in later #106 work and the
corresponding #115–#119 epics.
