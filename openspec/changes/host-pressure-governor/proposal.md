# Host pressure governs admission

## Why

The concurrency governor scaled its caps by resource pressure, but nothing in
production measured it. Supervisor admission replaced missing pressure with
0.0 and admitted at full caps (fail open), and `orchestrate`, `improve` and the
prover had no host gate. Operators used "swap below 2 GB" and "load below 6"
by hand, which misread this host: it swaps to ZRAM first, so swap use grows
while the host is healthy.

## What Changes

- Add `verdict/host_pressure.py`: a read-only sampler (PSI, MemAvailable,
  swap devices by kind, disk swap I/O rate, ZRAM fill, OOM kills) and a
  GREEN/YELLOW/RED hysteresis level shared through `$VERDICT_HOME`.
- Map the level to `ResourcePressure` at every production admission point:
  supervisor admission, `orchestrate --max-parallel`, `improve` cycles and the
  prover's agentic concurrency. Missing pressure now counts as full pressure.
- Add `verdict host pressure [--json]`; `verdict doctor` shows the level and
  fails on RED or missing evidence.
- ADR-039 records the thresholds and amends ADR-037 decision 3.

## Scope

`~/bin/vrun` (operator script) and OmniRoute's heavy-request limit are not
changed. Running work is never cancelled.
