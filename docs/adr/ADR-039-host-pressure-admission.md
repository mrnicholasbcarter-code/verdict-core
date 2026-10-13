# ADR-039: Host pressure governs admission

- **Status:** Accepted
- **Date:** 2026-10-13
- **Amends:** [ADR-037](ADR-037-supervisor-concurrency-governor.md) (operator decision 3, pressure inputs)
- **Related:** [ADR-036](ADR-036-goal-to-receipt-orchestration.md),
  [ADR-038](ADR-038-improvement-cycle.md)

## Context

`GlobalConcurrencyGovernor` scales its caps by a `ResourcePressure(cpu, ram,
disk)` in [0, 1], but no production code measured it. `evaluate_admission`
replaced a missing pressure with `ResourcePressure()`, which is 0.0: unknown
pressure admitted at full caps (fail open). `verdict orchestrate`,
`verdict improve` and the prover had no host gate at all; operators used two
absolute rules by hand instead: "swap below 2 GB" and "load below 6".

Both rules are wrong on this host. It swaps first to ZRAM (7.8 GiB, zstd,
priority 100), then to disk (`/swapfile3`, priority -2; `/dev/sdb`, priority
-3). Swap used to ZRAM is compressed RAM, not a stall, so "swap used" rises
while the host is healthy. Load counts runnable and I/O-blocked tasks, not how
long work actually waits. ADR-037 decision 3 (RAM and swap fractions) has the
same problem.

## Decision

`verdict/host_pressure.py` measures the host read-only and keeps one shared
level, GREEN, YELLOW or RED, in `$VERDICT_HOME/host-pressure.json` (under a
file lock, so every Verdict process steps the same hysteresis).

**Inputs.** Pressure stall information (`/proc/pressure/{memory,cpu,io}`),
`MemAvailable / MemTotal` (`/proc/meminfo`), swap devices (`/proc/swaps`,
classified `zram` or `disk` by device name), the disk swap I/O rate (pages
swapped in and out in `/proc/vmstat` minus ZRAM block I/O from
`/sys/block/zram*/stat`, at 8 sectors of 512 bytes per 4 KiB page), ZRAM fill
(`mm_stat` stored data / `disksize`), and new OOM kills (`oom_kill` in
`/proc/vmstat` and in `user.slice/memory.events`). ZRAM is never counted as RAM,
and swap size is never an input.

**Thresholds** (constants in `verdict/host_pressure.py`):

| Metric | YELLOW at | RED at |
|---|---|---|
| memory PSI full avg10 | 2 % | 10 % |
| memory PSI some avg60 | 10 % | 20 % |
| MemAvailable / MemTotal | below 0.20 | below 0.10 |
| disk swap I/O | 250 pages/s | 2000 pages/s |
| CPU PSI some avg60 | 60 % | - |
| I/O PSI some avg60 | 30 % | - |
| I/O PSI full avg10 | - | 20 % |
| ZRAM fill | 0.90 | - |
| new OOM kill | - | any |

**Hysteresis.** A worse level applies at once. A better level needs 3 samples,
at least 20 s apart, that are calm at half of every threshold (twice the free
RAM fraction), plus 120 s at the current level; it then steps down one level.
Without saved state the level starts at YELLOW. Missing PSI or meminfo evidence
never improves the level and admission treats it as full pressure.

**Admission.** The level maps to `ResourcePressure`: GREEN 0, YELLOW 0.5, RED
1.0, missing evidence NaN (which the governor already treats as 1.0).

| Admission point | GREEN | YELLOW | RED or no evidence |
|---|---|---|---|
| supervisor (`evaluate_admission`) | configured caps | caps halved | no new story (DEFER) |
| `verdict orchestrate --max-parallel N` | N workers | N/2 | 1 (the run keeps one slot so it can finish) |
| `verdict improve` | cycle runs | cycle runs (its run is limited as above) | cycle REFUSED with a retry time 120 s later |
| prover agentic phase (`agentic_concurrency`) | configured | halved | 1 |

`evaluate_admission` without a `pressure` argument now means "no evidence",
not "no pressure". `verdict orchestrate` re-reads the level at most every 10 s
while a node waits for a slot, records each change as a `controller` event with
state `HOST_PRESSURE`, and never cancels a running attempt: a lower limit only
stops new attempts.

**Commands.** `verdict host pressure [--json]` shows the level, the reasons,
the hold (what must happen before it steps down), the signals against their
thresholds, RAM from `MemAvailable` and the swap devices by kind. It exits 1
on RED or missing evidence. `verdict doctor` shows the level and reports RED or
missing evidence as an issue; YELLOW is shown, not failed.

## Relation to `~/bin/vrun`

`~/bin/vrun` is an operator script, not Verdict code, and is unchanged. It runs a
heavy local command (pytest, mypy, builds) in its own systemd scope with a
memory cap and a timeout, and lets only a fixed number of such jobs run at once
through `flock` slot files in `/tmp`. Its header says 2 slots; the loop
currently tries 3. The two limits do not share state and answer different
questions: vrun bounds local test and build jobs by a fixed count and confines
an OOM kill to the job's scope; the host pressure level bounds how much model
work Verdict starts, from measured stalls. A vrun job that pushes the host to
RED lowers Verdict's admission; Verdict never waits on a vrun slot.

## Consequences

- Admission fails closed: a host whose `/proc/pressure` is unreadable runs one
  worker per run and starts no new story or improvement cycle.
- The first command after a quiet period starts at YELLOW and needs about two
  minutes of calm samples to reach GREEN. A run started then runs at half its
  `--max-parallel` until the level steps down.
- Thresholds are constants. Changing one is a code change with a test, and this
  ADR is updated.
- Out of scope: OmniRoute's own heavy-request limit (4 in flight) is a gateway
  setting; it still caps concurrent agent turns regardless of the host level.
