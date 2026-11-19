# cert-evidence-gateway-origin: keep the gateway address out of rehearsal permit events

The rehearsal does not write the gateway address into its dispatch-permit events
or receipt, and the script refuses to upload any run evidence that contains the
gateway address or the gateway key.

## Why

A clean certification rehearsal completed once from empty state (03:21Z on
2026-11-19), but `scripts/run_certification_rehearsals.py` then refused its
evidence with "Run
evidence contains protected configuration; refusing upload". Every
`dispatch_permit` controller event records the run's gateway origin, and the
receipt projects those events into `controller_events`. On GitHub that address
is a protected secret and the run directory is uploaded as a workflow artifact,
so the scan is right to refuse. The run must not write the address into those events in
the first place.

## What Changes

- `DispatchAuthority` gains `record_gateway_origin` (default true). When false,
  each `dispatch_permit` event records the literal string `redacted` as its
  `gateway_origin`. A plain marker, not a hash: a likely hostname could be
  guessed and hashed.
- `verdict orchestrate` sets it false when `VERDICT_REDACT_GATEWAY_ORIGIN=1`.
  The certification rehearsal script sets that variable for every orchestrate
  run.
- The permit check is unchanged: the executor's reported origin is still
  compared with the real bound origin, and a mismatch or missing origin is still
  denied.
- The default is unchanged: without the variable, events and receipts are
  byte-compatible with today. Existing receipts verify exactly as before.
- The evidence scan in the rehearsal script is unchanged.

## Impact

- Code: `verdict/orchestration/runtime.py`, `verdict/orchestration/cli.py`,
  `scripts/run_certification_rehearsals.py`.
- Specs: `dispatch-permits`.
- Docs: `docs/certification/README.md`.
