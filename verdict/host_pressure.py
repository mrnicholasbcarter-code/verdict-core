"""Host pressure: a read-only host sample and a hysteresis level for admission (ADR-039).

``sample`` reads memory, CPU and I/O pressure stall information (PSI), RAM,
swap devices, ZRAM and one cgroup's memory events. ``govern`` turns samples into
a level, GREEN, YELLOW or RED, and ``current`` keeps that level in
``$VERDICT_HOME/host-pressure.json`` so every Verdict process shares it.

Two rules shape the inputs:

* RAM is ``MemAvailable`` only. ZRAM is swap, never RAM: its compressed pages
  already sit inside ``MemTotal - MemAvailable``.
* Swap size is never an input. Only the disk swap I/O rate is: all swap pages
  in and out (``/proc/vmstat``) minus ZRAM block I/O. Swapping to ZRAM is cheap;
  swapping to disk is the signal.

Thresholds (they replace the old "swap below 2 GB" and "load below 6" rules and
ADR-037 decision 3, which used RAM and swap fractions):

==================  ======  =====  ============================================
metric              YELLOW  RED    meaning
==================  ======  =====  ============================================
``mem_full_avg10``  2       10     % of time all tasks stalled on memory (10 s)
``mem_some_avg60``  10      20     % of time some task stalled on memory (60 s)
``avail_frac``      < 0.20  < 0.10 MemAvailable / MemTotal
``disk_swap_pps``   250     2000   pages per second swapped to or from disk
``cpu_some_avg60``  60      -      % of time some task waited for a CPU (60 s)
``io_some_avg60``   30      -      % of time some task waited for I/O (60 s)
``io_full_avg10``   -       20     % of time all tasks waited for I/O (10 s)
``zram_fill``       0.90    -      ZRAM stored data / ZRAM disk size
``oom_kill``        -       any    a new OOM kill on the host or in the cgroup
==================  ======  =====  ============================================

A worse level applies at once. A better level needs ``CALM_SAMPLES`` samples, at
least ``CALM_SAMPLE_GAP_S`` apart, in which every metric is calm (stall and swap
rates below ``RELAX`` times their threshold; ``avail_frac`` and ``zram_fill``
``FRACTION_MARGIN`` inside theirs), and ``MIN_DWELL_S`` at the current level. It then steps down one
level. The first sample (no saved state) starts at YELLOW. Missing PSI or meminfo evidence holds the level at YELLOW or
worse, and admission treats it as full pressure.

Admission maps the level to ``ResourcePressure`` (``LEVEL_PRESSURE``): GREEN 0,
YELLOW 0.5 (caps halved), RED 1.0 (the governor's floor of one: a running
orchestrate run keeps one worker slot so it can finish). Full pressure (RED or
missing evidence) starts no new story or improvement cycle. Nothing running is
ever cancelled.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any

from verdict.orchestration.concurrency_governor import ResourcePressure
from verdict.paths import verdict_home


class Level(str, Enum):
    GREEN = "GREEN"
    YELLOW = "YELLOW"
    RED = "RED"


_ORDER = {Level.GREEN: 0, Level.YELLOW: 1, Level.RED: 2}

YELLOW_AT: Mapping[str, float] = {
    "mem_full_avg10": 2.0,
    "mem_some_avg60": 10.0,
    "avail_frac": 0.20,
    "disk_swap_pps": 250.0,
    "cpu_some_avg60": 60.0,
    "io_some_avg60": 30.0,
    "zram_fill": 0.90,
}
RED_AT: Mapping[str, float] = {
    "mem_full_avg10": 10.0,
    "mem_some_avg60": 20.0,
    "avail_frac": 0.10,
    "disk_swap_pps": 2000.0,
    "io_full_avg10": 20.0,
}
#: Metrics where a lower value is worse.
BELOW = frozenset({"avail_frac"})
#: Calm band. Stall and swap rates are calm below ``RELAX`` x threshold. The two
#: capacity fractions move slowly (ZRAM keeps its pages long after a stall ends),
#: so halving them would hold a calm host at YELLOW for hours; they are calm
#: ``FRACTION_MARGIN`` inside their threshold instead.
RELAX = 0.5
FRACTIONS = frozenset({"avail_frac", "zram_fill"})
FRACTION_MARGIN = 0.05
CALM_SAMPLES = 3
CALM_SAMPLE_GAP_S = 20.0
MIN_DWELL_S = 120.0
#: The disk swap rate is measured over a window of at least this many seconds
#: (20 s windows on this host swung between 5 and 2350 pages/s, 2026-10-13)...
RATE_WINDOW_S = 60.0
#: ...and at most this many; an older baseline is replaced instead of averaged.
MAX_RATE_WINDOW_S = 300.0
#: Admission pressure per level (``ResourcePressure`` scale). YELLOW halves the
#: caps and RED leaves the governor's floor of one (ADR-037).
LEVEL_PRESSURE: Mapping[Level, float] = {Level.GREEN: 0.0, Level.YELLOW: 0.5, Level.RED: 1.0}

PAGE = os.sysconf("SC_PAGE_SIZE")  # pswpin/pswpout count pages; block stat counts 512-byte sectors
STATE_FILE = "host-pressure.json"


@dataclass(frozen=True)
class HostRoots:
    """Where the sampler reads. Only tests point these at a fake tree."""

    proc: Path = Path("/proc")
    sys_block: Path = Path("/sys/block")
    #: Verdict runs, OmniRoute and ~/bin/vrun jobs live under user.slice.
    cgroup: Path = Path("/sys/fs/cgroup/user.slice")


DEFAULT_ROOTS = HostRoots()


@dataclass(frozen=True)
class SwapDevice:
    name: str
    type: str
    kind: str  # "zram" or "disk"
    size: int  # bytes
    used: int  # bytes
    prio: int


@dataclass(frozen=True)
class ZramDevice:
    dev: str
    orig: int  # bytes of data stored, uncompressed
    compr: int  # bytes after compression
    mem_used: int  # RAM the device uses
    disksize: int


@dataclass(frozen=True)
class HostSample:
    t: float
    psi: Mapping[str, Mapping[str, float]]  # "memory"/"cpu"/"io" -> "some_avg10" -> %
    mem_total: int | None
    mem_available: int | None
    swaps: tuple[SwapDevice, ...] = ()
    zram: tuple[ZramDevice, ...] = ()
    swap_pages_io: int | None = None
    zram_pages_io: int = 0
    oom_kill: int | None = None  # host-wide, /proc/vmstat
    cgroup: str = ""
    cg_oom_kill: int | None = None
    cg_max_events: int | None = None
    cg_memory_max: int | None = None  # None: unlimited or unknown
    load1: float | None = None
    cpus: int = 1
    errors: tuple[str, ...] = ()

    @property
    def evidence_missing(self) -> bool:
        return self.mem_total is None or any(
            resource not in self.psi for resource in ("memory", "cpu", "io")
        )


def _psi(path: Path) -> dict[str, float]:
    out: dict[str, float] = {}
    for line in path.read_text().splitlines():
        kind, *fields = line.split()
        for item in fields:
            key, value = item.split("=")
            if key.startswith("avg"):
                out[f"{kind}_{key}"] = float(value)
    return out


def sample(roots: HostRoots | None = None, *, now: float | None = None) -> HostSample:
    """One read-only sample. An unreadable source is named in ``errors``."""
    roots = roots or DEFAULT_ROOTS
    errors: list[str] = []
    psi: dict[str, dict[str, float]] = {}
    for resource in ("memory", "cpu", "io"):
        try:
            psi[resource] = _psi(roots.proc / "pressure" / resource)
        except (OSError, ValueError) as exc:
            errors.append(f"psi/{resource}:{type(exc).__name__}")
    mem_total = mem_available = None
    try:
        info = {}
        for line in (roots.proc / "meminfo").read_text().splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                info[parts[0].rstrip(":")] = int(parts[1]) * 1024
        mem_total, mem_available = info["MemTotal"], info["MemAvailable"]
    except (OSError, KeyError) as exc:
        errors.append(f"meminfo:{type(exc).__name__}")
    swaps: list[SwapDevice] = []
    try:
        for line in (roots.proc / "swaps").read_text().splitlines()[1:]:
            name, typ, size, used, prio = line.split()[:5]
            kind = "zram" if Path(name).name.startswith("zram") else "disk"
            swaps.append(SwapDevice(name, typ, kind, int(size) * 1024, int(used) * 1024, int(prio)))
    except (OSError, ValueError) as exc:
        errors.append(f"swaps:{type(exc).__name__}")
    zram: list[ZramDevice] = []
    zram_pages_io = 0
    for device in swaps:
        if device.kind != "zram":
            continue
        block = roots.sys_block / Path(device.name).name
        try:
            stat = (block / "stat").read_text().split()
            zram_pages_io += (int(stat[2]) + int(stat[6])) * 512 // PAGE
            mm = (block / "mm_stat").read_text().split()
            disksize = int((block / "disksize").read_text())
            zram.append(ZramDevice(block.name, int(mm[0]), int(mm[1]), int(mm[2]), disksize))
        except (OSError, ValueError, IndexError) as exc:
            errors.append(f"zram/{block.name}:{type(exc).__name__}")
    swap_pages_io = host_oom_kill = None
    try:
        vmstat = dict(line.split() for line in (roots.proc / "vmstat").read_text().splitlines())
        swap_pages_io = int(vmstat["pswpin"]) + int(vmstat["pswpout"])
        host_oom_kill = int(vmstat["oom_kill"]) if "oom_kill" in vmstat else None
    except (OSError, KeyError, ValueError) as exc:
        errors.append(f"vmstat:{type(exc).__name__}")
    oom_kill = max_events = memory_max = None
    try:
        events = dict(
            line.split() for line in (roots.cgroup / "memory.events").read_text().splitlines()
        )
        oom_kill, max_events = int(events.get("oom_kill", 0)), int(events.get("max", 0))
        raw_max = (roots.cgroup / "memory.max").read_text().strip()
        memory_max = None if raw_max == "max" else int(raw_max)
    except (OSError, ValueError) as exc:
        errors.append(f"cgroup:{type(exc).__name__}")
    load1: float | None
    try:
        load1 = float((roots.proc / "loadavg").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        load1 = None
    return HostSample(
        t=time.time() if now is None else now,
        psi=psi,
        mem_total=mem_total,
        mem_available=mem_available,
        swaps=tuple(swaps),
        zram=tuple(zram),
        swap_pages_io=swap_pages_io,
        zram_pages_io=zram_pages_io,
        oom_kill=host_oom_kill,
        cgroup=str(roots.cgroup),
        cg_oom_kill=oom_kill,
        cg_max_events=max_events,
        cg_memory_max=memory_max,
        load1=load1,
        cpus=os.cpu_count() or 1,
        errors=tuple(errors),
    )


def _rate(sample: HostSample, state: dict[str, Any]) -> float | None:
    """Disk swap pages per second; updates the window baseline in *state*."""
    if sample.swap_pages_io is None:
        return None
    base = state.get("baseline")
    point = {"t": sample.t, "swap": sample.swap_pages_io, "zram": sample.zram_pages_io}
    if base is None:
        state["baseline"], state["disk_swap_pps"] = point, None
        return None
    dt = sample.t - float(base["t"])
    swapped = sample.swap_pages_io - int(base["swap"])
    to_zram = sample.zram_pages_io - int(base["zram"])
    if dt > MAX_RATE_WINDOW_S or dt < 0 or swapped < 0 or to_zram < 0:
        state["baseline"], state["disk_swap_pps"] = point, None  # stale or counters reset
        return None
    if dt < RATE_WINDOW_S:
        rate = state.get("disk_swap_pps")
        return None if rate is None else float(rate)
    rate = max(0.0, (swapped - to_zram) / dt)
    state["baseline"], state["disk_swap_pps"] = point, rate
    return rate


def _metrics(sample: HostSample, state: dict[str, Any]) -> dict[str, float]:
    m: dict[str, float] = {}
    memory, cpu, io = (sample.psi.get(r, {}) for r in ("memory", "cpu", "io"))
    for name, source, key in (
        ("mem_full_avg10", memory, "full_avg10"),
        ("mem_some_avg60", memory, "some_avg60"),
        ("cpu_some_avg60", cpu, "some_avg60"),
        ("io_some_avg60", io, "some_avg60"),
        ("io_full_avg10", io, "full_avg10"),
    ):
        if key in source:
            m[name] = source[key]
    if sample.mem_total and sample.mem_available is not None:
        m["avail_frac"] = sample.mem_available / sample.mem_total
    for device in sample.zram:
        fill = device.orig / device.disksize if device.disksize else 1.0
        m["zram_fill"] = max(m.get("zram_fill", 0.0), fill)
    rate = _rate(sample, state)
    if rate is not None:
        m["disk_swap_pps"] = rate
    # Prime agents run outside user.slice, so the host-wide counter is watched too.
    for key, count in (("oom_kill", sample.oom_kill), ("cg_oom_kill", sample.cg_oom_kill)):
        seen = state.get(key)
        if count is None:
            continue
        if seen is not None and count > int(seen):
            m["oom_kills"] = max(m.get("oom_kills", 0.0), float(count - int(seen)))
        state[key] = count
    return m


def _hits(m: Mapping[str, float], table: Mapping[str, float], calm: bool) -> list[str]:
    """Metrics at or past *table*; with *calm*, past the calm band instead (see RELAX)."""
    hits = []
    for key, threshold in table.items():
        if key not in m:
            continue
        if key in FRACTIONS:
            band = FRACTION_MARGIN if calm else 0.0
            past = m[key] < threshold + band if key in BELOW else m[key] >= threshold - band
        else:
            past = m[key] >= threshold * (RELAX if calm else 1.0)
        if past:
            hits.append(key)
    return hits


def level_for(m: Mapping[str, float], *, calm: bool = False) -> tuple[Level, list[str]]:
    """The level the metrics call for (with *calm*, inside the calm band) and why."""
    red = _hits(m, RED_AT, calm) + (["oom_kill"] if m.get("oom_kills", 0) > 0 else [])
    if red:
        return Level.RED, red
    yellow = _hits(m, YELLOW_AT, calm)
    return (Level.YELLOW, yellow) if yellow else (Level.GREEN, [])


@dataclass(frozen=True)
class HostDecision:
    level: Level
    target: Level
    reasons: tuple[str, ...]
    metrics: Mapping[str, float]
    calm: int
    since: float
    sample: HostSample
    errors: tuple[str, ...] = ()
    state_path: str = ""

    @property
    def evidence_missing(self) -> bool:
        return self.sample.evidence_missing

    @property
    def pressure(self) -> float:
        """Admission pressure in [0, 1]; NaN (full pressure) when evidence is missing."""
        return math.nan if self.evidence_missing else LEVEL_PRESSURE[self.level]

    @property
    def hold(self) -> str:
        """Why the level is above what the metrics call for now, if it is."""
        if _ORDER[self.level] <= _ORDER[self.target]:
            return ""
        dwell = max(0.0, self.sample.t - self.since)
        return (
            f"stepping down after {CALM_SAMPLES} calm samples {CALM_SAMPLE_GAP_S:g} s apart "
            f"and {MIN_DWELL_S:g} s at {self.level.value}: calm {self.calm}/{CALM_SAMPLES}, "
            f"{dwell:.0f}/{MIN_DWELL_S:g} s"
        )

    def summary(self) -> str:
        why = ", ".join(self.reasons) if self.reasons else "all metrics below thresholds"
        text = f"{self.level.value} ({why})"
        return f"{text}; {self.hold}" if self.hold else text

    def to_dict(self) -> dict[str, Any]:
        sample = self.sample
        return {
            "level": self.level.value,
            "target": self.target.value,
            "reasons": list(self.reasons),
            "hold": self.hold,
            "evidence_missing": self.evidence_missing,
            "pressure": None if self.evidence_missing else self.pressure,
            "metrics": {k: round(v, 4) for k, v in self.metrics.items()},
            "thresholds": {"yellow": dict(YELLOW_AT), "red": dict(RED_AT), "relax": RELAX},
            "calm": self.calm,
            "since": self.since,
            "ram": {
                "source": "MemAvailable",
                "mem_total": sample.mem_total,
                "mem_available": sample.mem_available,
            },
            "psi": {k: dict(v) for k, v in sample.psi.items()},
            "swap": [asdict(device) for device in sample.swaps],
            "zram": [asdict(device) for device in sample.zram],
            "oom_kill": sample.oom_kill,
            "cgroup": {
                "path": sample.cgroup,
                "oom_kill": sample.cg_oom_kill,
                "max_events": sample.cg_max_events,
                "memory_max": sample.cg_memory_max,
            },
            "load1": sample.load1,
            "cpus": sample.cpus,
            "sampled_at": sample.t,
            "errors": list(self.errors),
            "state_path": self.state_path,
        }


def govern(
    sample: HostSample, state: Mapping[str, Any] | None
) -> tuple[dict[str, Any], HostDecision]:
    """One pure hysteresis step: the next state and the decision for *sample*."""
    new: dict[str, Any] = dict(state or {"level": Level.YELLOW.value, "since": sample.t, "calm": 0})
    current = Level(new["level"])
    m = _metrics(sample, new)
    if sample.evidence_missing:
        reasons = ["evidence_missing:" + ",".join(sample.errors or ("psi/meminfo",))]
        target = max(Level.YELLOW, current, key=_ORDER.__getitem__)  # unknown never improves
    else:
        target, hits = level_for(m)
        reasons = list(hits)
    if _ORDER[target] > _ORDER[current]:
        new.update(level=target.value, since=sample.t, calm=0, calm_at=None)
    elif _ORDER[target] < _ORDER[current]:
        relaxed, _ = level_for(m, calm=True)
        if _ORDER[relaxed] >= _ORDER[current]:
            new.update(calm=0, calm_at=None)
        elif new.get("calm_at") is None or sample.t - float(new["calm_at"]) >= CALM_SAMPLE_GAP_S:
            new.update(calm=int(new.get("calm", 0)) + 1, calm_at=sample.t)
        if int(new["calm"]) >= CALM_SAMPLES and sample.t - float(new["since"]) >= MIN_DWELL_S:
            lower = next(lvl for lvl, rank in _ORDER.items() if rank == _ORDER[current] - 1)
            new.update(level=lower.value, since=sample.t, calm=0, calm_at=None)
    else:
        new.update(calm=0, calm_at=None)
    decision = HostDecision(
        level=Level(new["level"]),
        target=target,
        reasons=tuple(reasons),
        metrics=m,
        calm=int(new["calm"]),
        since=float(new["since"]),
        sample=sample,
        errors=sample.errors,
    )
    return new, decision


def default_state_path() -> Path:
    return verdict_home() / STATE_FILE


def current(*, roots: HostRoots | None = None, state_path: Path | None = None) -> HostDecision:
    """Sample the host and step the shared hysteresis state under a file lock.

    When the state cannot be read or written the step starts from no state
    (YELLOW) and the error is listed; the decision is never better for it.
    """
    path = state_path or default_state_path()
    errors: list[str] = []
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path.with_suffix(".lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                prior = json.loads(path.read_text()) if path.exists() else None
            except (OSError, ValueError) as exc:
                prior, errors = None, [f"state:{type(exc).__name__}"]
            state, decision = govern(sample(roots), prior)
            tmp = path.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(state))
            os.replace(tmp, path)
    except OSError as exc:
        _, decision = govern(sample(roots), None)
        errors.append(f"state:{type(exc).__name__}")
    return replace(decision, errors=(*decision.errors, *errors), state_path=str(path))


def resource_pressure(decision: HostDecision) -> ResourcePressure:
    """The host level as governor pressure; missing evidence is full pressure."""
    p = decision.pressure
    return ResourcePressure(cpu=p, ram=p, disk=p)
