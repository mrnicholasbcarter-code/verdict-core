# Tasks

## 1. Wave 1 — shared gateway binding and metadata schedule (Foundation)

- [x] 1.1 **S01 / BOD-416** (acceptance A06): Unify credential and gateway binding across discovery and transport; verify canonical/stored credentials bind both callers identically, explicit URL precedence, default `http://127.0.0.1:20128/v1`, and secret-free errors with focused tests. Pre-merge code implementation and automated test verification complete; branch real call exit 0 recorded, parent merged-main repeat verification pending. Does NOT claim full story DoD, Done status, or change archive.
- [x] 1.2 **S08 / BOD-436** (acceptance A24): Install one optional six-hour metadata refresh timer with 5-minute boot delay, explicit setup opt-in, non-destructive dry-run, source provenance, partial-source retention, concurrent lock, and doctor completion/freshness checks with isolated tests. Pre-merge code implementation and automated test verification complete; real host installation not authorized for this PR. Does NOT claim full story DoD, Done status, host install, or change archive.

## Scope and Delivery Notice

- **Active Scope**: This active OpenSpec change covers Wave 1 (Foundation: S01 and S08) pre-merge code implementation and test verification only.
- **Umbrella Planning Authority**: The full 13-story V1 hardening plan (Waves 1–7, stories S01–S13, acceptance criteria A01–A30) is preserved byte-for-byte in `docs/contracts/v1-hardening-umbrella-plan.md` and tracked in Linear under BOD-431.
- **Subsequent Waves**: Stories S02–S13 will enter `openspec/changes/` in dedicated follow-on PRs as their implementation waves are dispatched.
- **DoD Policy**: Ticked tasks (`[x]`) represent pre-merge code implementation and focused test verification within PR 916. Stories S01 and S08 remain open and unarchived until the parent verifies live commands on the operator's gateway from merged main per operator rules (`test != Done`).
