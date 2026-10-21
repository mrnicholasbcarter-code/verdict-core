# Passive-first identify maintenance

## Why

BOD-350 and BOD-342 D3/D4 replace the retired five-minute prove-at-rest/census loops with passive identification and bounded missing-proof screening. Doctor must report the replacement honestly, not require a retired timer.

## What Changes

- Explicit `verdict identify install-service` / `uninstall-service` manage separate passive and screen service/timer pairs. Setup uses the same installer after consent.
- Approved defaults (2026-10-21): passive every 3 hours; screen daily, free/subscription capacity only, at most 1 route per provider and 8 routes per run.
- Passive persistence is explicit (`--save-report`): atomically replace a private JSON report. Ordinary passive mode remains read-only.
- Doctor checks both timers and a source-error-free passive report younger than twice the interval. Health-cache usability uses evidence lifetimes, not the retired 300-second probe cadence.
- Installed-runtime provenance includes both new services. Existing hourly acceptance and eligible-free-route requirement are unchanged.

## Capabilities

### New Capabilities
- `identify-maintenance`: installable passive reports and bounded daily screening, with truthful setup/doctor state.

## Impact

CLI parser/dispatch, identify, service rendering, setup, doctor, tests. No dependency changes or legacy unit activation. Real-host installation and full DoD remain parent responsibilities after merge.

## Rollout / verification after merge

Merge and fresh-main real-host proof remain release work, not completed implementation tasks. This change is not Done until the parent installs from merged main, runs `verdict identify install-service`, shows raw `systemctl --user status` and report output, and verifies freshness survival across the passive/screen interval.
