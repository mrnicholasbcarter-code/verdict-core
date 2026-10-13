# Evidence: host-pressure-governor

Host: operator host `verdict` (user nick), 2026-10-13. Branch
`story/host-pressure-governor-20261013`. Commands ran from the worktree checkout
`/home/nick/dev/verdict-host-pressure` with its `.venv/bin/python -m verdict`.
This is branch evidence, not merged-main evidence.

## `verdict host pressure --json` (20000c86, 2026-10-13T19:34:31Z, exit 0)

```
"level": "GREEN", "pressure": 0.0, "evidence_missing": false
"ram": {"source": "MemAvailable", "mem_available": 11783356416, "mem_total": 16769368064}
"swap": [
  {"name": "/swapfile3", "kind": "disk", "prio": -2, "used": 1289928704, "size": 6442446848}
  {"name": "/dev/sdb", "kind": "disk", "prio": -3, "used": 49733632, "size": 520089600}
  {"name": "/dev/zram0", "kind": "zram", "prio": 100, "used": 5611085824, "size": 8384409600}
]
```

`zram0` is kind `zram` at priority 100; `/swapfile3` and `/dev/sdb` are kind
`disk`. RAM is `MemAvailable` only.

## `verdict doctor --json` (a458c8e7, 2026-10-13T19:53Z, exit 1 for unrelated issues)

```
{"label": "Host pressure", "state": "ok", "detail": "GREEN (all metrics below thresholds)"}
```

The exit code 1 comes from issues that existed before this story
(documentation preflight orphaned records, old config schema), not from host
pressure.

## Stress drill with `verdict orchestrate --max-parallel 4` (a458c8e7)

Run `20261013T195255Z`: 16 independent nodes, `--max-parallel 4`,
`--executor direct-gateway --inject '*=hang,...' --attempt-timeout 300` (each
attempt holds its slot for 300 s; no model work is done). Once 4 workers were
running, a 2500 MB incompressible writer started in
`systemd-run --user --scope -p MemoryMax=1500M -p MemorySwapMax=3G` (`hog.py`);
the kernel OOM-killed it inside its scope at 19:58:48 (`run-u966.scope: A process
of this unit has been killed by the OOM killer`). No other unit was affected.

`HOST_PRESSURE` controller events in the run's `events.jsonl`:

```
{"seq": 6, "at": "2026-10-13T19:53:12.779549Z", "type": "controller", "state": "HOST_PRESSURE", "level": "GREEN", "max_parallel": 4, "configured_max_parallel": 4, "reasons": []}
{"seq": 33, "at": "2026-10-13T19:57:01.873569Z", "type": "controller", "state": "HOST_PRESSURE", "level": "YELLOW", "max_parallel": 2, "configured_max_parallel": 4, "reasons": ["mem_full_avg10"]}
{"seq": 60, "at": "2026-10-13T19:58:09.366741Z", "type": "controller", "state": "HOST_PRESSURE", "level": "RED", "max_parallel": 1, "configured_max_parallel": 4, "reasons": ["mem_full_avg10"]}
{"seq": 61, "at": "2026-10-13T20:00:59.439011Z", "type": "controller", "state": "HOST_PRESSURE", "level": "YELLOW", "max_parallel": 2, "configured_max_parallel": 4, "reasons": []}
{"seq": 104, "at": "2026-10-13T20:03:03.041589Z", "type": "controller", "state": "HOST_PRESSURE", "level": "GREEN", "max_parallel": 4, "configured_max_parallel": 4, "reasons": []}
```

Dispatch (`+`) and terminal (`-`) timeline:

```
19:53:12 HOST GREEN 4
19:57:01 HOST YELLOW 2
19:57:57 +n00 1
19:57:57 +n01 2
19:57:57 +n02 3
19:57:57 +n03 4
19:58:09 HOST RED 1
20:00:59 HOST YELLOW 2
20:02:42 -n00 3
20:02:42 -n01 2
20:02:42 -n02 1
20:02:42 -n03 0
20:02:42 +n15 1
20:02:43 +n04 2
20:03:03 HOST GREEN 4
20:03:03 +n05 3
```

- GREEN: 4 workers dispatched together (19:57:57).
- Stress: RED at 19:58:09 (memory PSI full avg10 18 to 43 %), limit 1. The 4
  running attempts were not cancelled.
- When they finished (20:02:42), only 2 new attempts started (limit 2 at YELLOW).
- Recovery: GREEN at 20:03:03, limit 4, two more dispatched, 4 running again.
- No oscillation: 59 samples at 15 s (`monitor.py`), 4 level changes
  (GREEN to YELLOW to RED to YELLOW to GREEN); every step down came at least 120 s after the
  step before it.

Over the 1.9 h live log before this run (459 samples taken under the operator's
normal agent load), the raw level computed from the metrics changed 92 times
and the hysteresis level changed 23 times; no step down came less than 120 s
after a change.

## Findings recorded during the drill

- A halved ZRAM-fill calm band (0.45) kept a calm host at YELLOW for hours,
  because ZRAM stayed 65 % full after the stress. Fixed in a458c8e7 (capacity
  fractions are calm 0.05 inside their threshold).
- 20 s windows for the disk swap rate swung between 5 and 2350 pages/s on this
  host. The window is now 60 to 300 s (f5019727).
- Not part of this story: `DagRuntime._drive` calls `selector.select`
  synchronously. Each selection took 8 to 75 s here and blocked the event loop,
  so dispatch started 4 to 5 min after the run did. Story draft:
  `selection-blocks-event-loop` (handed to the controller).
