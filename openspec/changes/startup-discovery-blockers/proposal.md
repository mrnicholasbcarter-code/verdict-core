# Settle expected startup discovery blockers

## Why
A rejected gateway key caused discovery to raise before the run directory and
terminal event existed. The supervisor treated that known blocker as a crash
and restarted the unchanged controller until its restart budget was exhausted.

## What Changes
Create the run directory before live selector discovery. A typed non-retryable
`GatewayDiscoveryError` during discovery writes one `run_finished` BLOCKED event
with `phase=startup`, prints the safe endpoint cause, and returns nonzero.
The existing supervisor consumes the event without restarting the controller.
Unexpected implementation errors still raise and retain crash recovery.

## Safety
No credential fallback, minting, auth bypass, provider call, or gate weakening.
No receipt or COMPLETE claim is produced for a run that never planned work.
