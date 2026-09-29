# Flagship Demo Recording Plan

This plan prepares the final BOD-279/BOD-242 capture. Do not record it until the
listed TUI dependencies have landed on the verified build. The scenario is a
**RECORDED/OFFLINE-SCENARIO**, not proof of live provider latency, quality, cost,
or savings. Static route inventory, worker responses, one rate-limit fault, and
the OCR PASS are scripted at their normal I/O boundaries. The production
`run_golden_path` loop, `EligibilityLadder`, recovery policy, reviewer parsing,
event log, and receipt code produce the evidence shown in the recording.

## Build gate

Record from a clean checkout after these surfaces are integrated and verified:

- **#719 / BOD-276**: cockpit follow/replay and navigation.
- **#720 / BOD-279**: evidence-derived claims and trace projection.
- **BOD-275**: shared actions used by keyboard/command controls.
- **BOD-277**: routing/model/provider view.
- **BOD-278**: context provenance/budget view.

Do not substitute mock panels when a dependency is absent. Defer that shot.

The run id is fixed. Event timestamps, cooldown expirations, measured durations,
and git commit hashes remain runtime-derived because the production run loop does
not expose clock or git-object injection seams. Routes, fault count, event types,
recovery outcome, review outcome, and receipt verdict are deterministic.

## Exact command sequence

Use a fresh output directory so the fixed run id cannot append to old evidence.
These commands use the same installed checkout for scenario generation and TUI.

```bash
# [RECORDED/OFFLINE-SCENARIO] Generate durable events and receipt; no credentials/network.
rm -rf /tmp/verdict-flagship-recording
mkdir -p /tmp/verdict-flagship-recording
python scripts/demo_scenario.py /tmp/verdict-flagship-recording

# [RECORDED/OFFLINE-SCENARIO] Verify completion and event-log digest before capture.
verdict run-receipt /tmp/verdict-flagship-recording/offline-flagship-failover

# [RECORDED/OFFLINE-SCENARIO] Start the final terminal recording and replay the real TUI.
asciinema rec docs/demo/flagship.cast --overwrite --command \
  "verdict watch /tmp/verdict-flagship-recording/offline-flagship-failover --replay"
```

Inside the cockpit, pause long enough for each title and evidence value to read:

1. **Cockpit replay** — show goal, DAG, `alpha/model-a` selection, the injected
   `rate_limited` failure, provider cooldown, `beta/model-b` reassignment, nodes
   continuing, validation, independent review PASS, and COMPLETE receipt.
   Dependency: **#719/BOD-276** plus shared **BOD-275 actions**.
2. **Routing view** — invoke the shared routing action and show the recorded
   eligibility ladder, selected/actual route identities, rejection stage, and
   cooldown evidence. Dependency: **BOD-277** and **BOD-275**.
3. **Context view** — invoke the shared context action and show only provenance
   and budget fields actually present in this run. Dependency: **BOD-278** and
   **BOD-275**. Never fill absent context evidence with a sample value.
4. **Failure chain** — open node-1's trace and show selection → failure →
   cooldown/exclusion → replacement → terminal → verification. Dependencies:
   **#719/BOD-276**, **BOD-277**, and **BOD-275**.
5. **Receipt** — open the receipt action and show COMPLETE, review PASS, attempt
   chain, and verified event digest. Dependencies: **#719/BOD-276** and
   **BOD-275**.
6. **Claims** — open `CLAIMS VERIFIED`. Every displayed claim must be derived
   from this run's events/receipt. Dependency: **#720/BOD-279**. Omit cost,
   savings, live quality, and any other claim without a predicate satisfied by
   this run.
7. Quit through the shared action, stop `asciinema`, then verify the same run
   once more:

```bash
# [RECORDED/OFFLINE-SCENARIO] Post-recording integrity check.
verdict run-receipt /tmp/verdict-flagship-recording/offline-flagship-failover
```

## Separate live capture (optional)

Any provider-backed run is **LIVE** and must be recorded separately. Label it
`LIVE: credentials and provider spend used`. It must use the normal live command
and production routing path. Do not reuse this offline script, do not inject a
failure, and do not carry offline claims or values into the live recording.

## Publication checks

- The pre- and post-recording receipt commands both report `COMPLETE` and
  `integrity: OK (events digest verified)`.
- The cast title/caption says `RECORDED/OFFLINE-SCENARIO`.
- A separate `LIVE` label appears on any live execution footage.
- No model score, cost, savings, provider result, or claim was added by hand.
- The final cast comes only from the verified build containing all dependencies.
