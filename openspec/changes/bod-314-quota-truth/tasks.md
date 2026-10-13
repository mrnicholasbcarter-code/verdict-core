# Tasks

Recorded from PR #805 (release train 8, merged 2026-10-10).

- [x] Read-only OmniRoute diagnosis of the 100% display (no model calls, no writes).
- [x] Keep safe cooldown evidence in sanitized connections; hash account ids.
- [x] Add `availability_evidence` to the verified-model projection; quota UNKNOWN without a usage field.
- [x] Block on a current cooldown or a 429 newer than the last success through the existing scoped blockers.
- [x] Offline focused tests, redaction audits and mutations; full suite on the PR head.
