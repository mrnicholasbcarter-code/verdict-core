# Tasks

## 1. Implementation and focused verification

Ownership: story branch owner; verify with the focused tests named below.

- [x] 1.1 Inspect HealthCache, SessionLedger, terminal classifier and pool identities.
- [x] 1.2 Add a negative-only helper using HealthCache's serialized writer.
- [x] 1.3 Store bounded classifier expiry, canonical scope and opaque provenance.
- [x] 1.4 Keep unavailable turns explicitly unknown; do not fabricate them.
- [x] 1.5 Add focused offline persistence, expiry, scope and exclusion tests.
- [x] 1.6 Integrate real runtime and planner failure handlers and cross-run selection readers.
- [x] 1.7 Reload concurrent failure evidence at assessment, refresh and dispatch.
- [x] 1.8 Validate focused runtime, selector, expiry and injected/task exclusion regressions.
- [ ] 1.9 Validate integrated admission, review and release-train gates.
- [ ] 1.10 Capture authenticated live evidence when separately authorized and available.
