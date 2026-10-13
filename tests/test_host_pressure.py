"""Host pressure sampler and hysteresis (ADR-039), on fake /proc and /sys trees."""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from verdict import host_pressure as hp
from verdict.host_pressure import HostRoots, Level, govern, sample
from verdict.orchestration.concurrency_governor import ResourcePressure, effective_cap

GIB = 1024**3
# Collected before conftest's autouse fixture replaces ``current`` with a calm host.
_REAL_CURRENT: Callable[..., hp.HostDecision] = hp.current


def _psi(some10: float = 0.1, some60: float = 1.0, full10: float = 0.1, full60: float = 0.5) -> str:
    return (
        f"some avg10={some10:.2f} avg60={some60:.2f} avg300=0.00 total=1\n"
        f"full avg10={full10:.2f} avg60={full60:.2f} avg300=0.00 total=1\n"
    )


def fake_host(
    root: Path,
    *,
    mem_full10: float = 0.1,
    mem_some60: float = 1.0,
    avail_kb: int = 12_000_000,
    total_kb: int = 16_000_000,
    pswp: tuple[int, int] = (0, 0),
    zram_sectors: tuple[int, int] = (0, 0),
    zram_orig: int = 1 * GIB,
    oom_kill: int = 0,
    cg_oom_kill: int = 0,
) -> HostRoots:
    """This host's layout: zram0 (prio 100) above /swapfile3 and /dev/sdb."""
    proc, block, cgroup = root / "proc", root / "sys" / "block", root / "cgroup"
    (proc / "pressure").mkdir(parents=True, exist_ok=True)
    (proc / "pressure" / "memory").write_text(_psi(full10=mem_full10, some60=mem_some60))
    (proc / "pressure" / "cpu").write_text(_psi(some60=5.0))
    (proc / "pressure" / "io").write_text(_psi(some60=2.0, full10=0.5))
    (proc / "meminfo").write_text(
        f"MemTotal:       {total_kb} kB\nMemFree:          100000 kB\n"
        f"MemAvailable:   {avail_kb} kB\nHugePages_Total:       0\n"
    )
    (proc / "swaps").write_text(
        "Filename\t\t\t\tType\t\tSize\t\tUsed\t\tPriority\n"
        "/swapfile3                              file\t\t6291452\t\t2859912\t\t-2\n"
        "/dev/sdb                                partition\t507900\t\t101620\t\t-3\n"
        "/dev/zram0                              partition\t8187900\t\t2777536\t\t100\n"
    )
    (proc / "vmstat").write_text(
        f"nr_free_pages 1\npswpin {pswp[0]}\npswpout {pswp[1]}\noom_kill {oom_kill}\n"
    )
    (proc / "loadavg").write_text("3.20 2.42 2.98 1/900 12345\n")
    zram = block / "zram0"
    zram.mkdir(parents=True, exist_ok=True)
    fields = ["0"] * 17
    fields[2], fields[6] = str(zram_sectors[0]), str(zram_sectors[1])
    (zram / "stat").write_text(" ".join(fields) + "\n")
    (zram / "mm_stat").write_text(f"{zram_orig} {zram_orig // 5} {zram_orig // 4} 0 0 0 0 0 0\n")
    (zram / "disksize").write_text(f"{8 * GIB}\n")
    cgroup.mkdir(parents=True, exist_ok=True)
    (cgroup / "memory.events").write_text(f"low 0\nhigh 0\nmax 3\noom 0\noom_kill {cg_oom_kill}\n")
    (cgroup / "memory.max").write_text("max\n")
    return HostRoots(proc=proc, sys_block=root / "sys" / "block", cgroup=cgroup)


def test_sample_classifies_swap_devices_and_uses_memavailable_only(tmp_path: Path) -> None:
    s = sample(fake_host(tmp_path, avail_kb=4_000_000), now=0.0)
    kinds = {d.name: (d.kind, d.prio) for d in s.swaps}
    assert kinds == {
        "/dev/zram0": ("zram", 100),
        "/swapfile3": ("disk", -2),
        "/dev/sdb": ("disk", -3),
    }
    assert s.mem_available == 4_000_000 * 1024 and s.mem_total == 16_000_000 * 1024
    assert s.zram[0].dev == "zram0" and s.zram[0].disksize == 8 * GIB
    assert s.cg_memory_max is None and s.cg_max_events == 3 and s.oom_kill == 0
    assert s.errors == () and not s.evidence_missing


def test_free_zram_never_counts_as_ram(tmp_path: Path) -> None:
    # 8% of RAM available while ZRAM has 7 GiB free: RED, ZRAM capacity does not help.
    _, d = govern(sample(fake_host(tmp_path, avail_kb=1_280_000), now=0.0), None)
    assert d.level is Level.RED and "avail_frac" in d.reasons


def test_first_sample_is_cautious_then_recovers_after_calm_and_dwell(tmp_path: Path) -> None:
    roots = fake_host(tmp_path)
    state, d = govern(sample(roots, now=0.0), None)
    assert d.level is Level.YELLOW and d.target is Level.GREEN and d.calm == 1 and d.hold
    state, d = govern(sample(roots, now=60.0), state)
    assert d.level is Level.YELLOW and d.calm == 2  # calm, but 60 s < 120 s dwell
    state, d = govern(sample(roots, now=120.0), state)
    assert d.level is Level.GREEN and d.pressure == 0.0


def test_worse_is_immediate_better_is_one_step_with_hysteresis(tmp_path: Path) -> None:
    state: dict[str, Any] = {"level": "GREEN", "since": -999.0, "calm": 0}
    state, d = govern(sample(fake_host(tmp_path, mem_full10=12.0), now=0.0), state)
    assert d.level is Level.RED and d.reasons == ("mem_full_avg10",)
    # Below the RED threshold but not calm at half of it: stays RED.
    state, d = govern(sample(fake_host(tmp_path, mem_full10=6.0), now=60.0), state)
    assert d.level is Level.RED and d.target is Level.YELLOW and d.calm == 0
    calm = fake_host(tmp_path, mem_full10=0.2)
    levels = []
    for t in (120.0, 180.0, 240.0, 300.0, 360.0, 420.0, 480.0):
        state, d = govern(sample(calm, now=t), state)
        levels.append(d.level)
    # One level at a time, each after 3 calm samples and 120 s at the level.
    assert levels == [
        Level.RED,
        Level.RED,
        Level.YELLOW,
        Level.YELLOW,
        Level.YELLOW,
        Level.GREEN,
        Level.GREEN,
    ]


def test_frequent_samples_cannot_shortcut_calm_count(tmp_path: Path) -> None:
    roots = fake_host(tmp_path)
    state: dict[str, Any] = {"level": "YELLOW", "since": 0.0, "calm": 0}
    for t in range(120, 130):  # ten samples within ten seconds
        state, d = govern(sample(roots, now=float(t)), state)
    assert d.level is Level.YELLOW and d.calm == 1


def test_zram_only_swapping_is_not_disk_swap_io(tmp_path: Path) -> None:
    state: dict[str, Any] = {"level": "GREEN", "since": -999.0, "calm": 0}
    state, _ = govern(sample(fake_host(tmp_path), now=0.0), state)
    # 600k pages swapped in 60 s, all of them through zram0 (8 sectors per page).
    zram = fake_host(tmp_path, pswp=(300_000, 300_000), zram_sectors=(2_400_000, 2_400_000))
    state, d = govern(sample(zram, now=60.0), state)
    assert d.level is Level.GREEN and d.metrics["disk_swap_pps"] == 0.0
    disk = fake_host(
        tmp_path, pswp=(375_000, 375_000), zram_sectors=(2_400_000, 2_400_000)
    )  # +150k pages in 60 s that zram did not see: 2500 pages/s to disk
    state, d = govern(sample(disk, now=120.0), state)
    assert d.level is Level.RED and "disk_swap_pps" in d.reasons


def test_swap_rate_needs_a_window_and_keeps_its_baseline(tmp_path: Path) -> None:
    state: dict[str, Any] = {"level": "GREEN", "since": -999.0, "calm": 0}
    state, d = govern(sample(fake_host(tmp_path), now=0.0), state)
    assert "disk_swap_pps" not in d.metrics
    state, d = govern(sample(fake_host(tmp_path, pswp=(5_000, 5_000)), now=5.0), state)
    assert "disk_swap_pps" not in d.metrics  # 5 s is too short; baseline kept at t=0
    state, d = govern(sample(fake_host(tmp_path, pswp=(10_000, 10_000)), now=25.0), state)
    assert d.metrics["disk_swap_pps"] == pytest.approx(800.0) and d.level is Level.YELLOW


def test_new_oom_kill_is_red_host_wide_or_in_cgroup(tmp_path: Path) -> None:
    base = {"level": "GREEN", "since": -999.0, "calm": 0}
    state, _ = govern(sample(fake_host(tmp_path), now=0.0), base)
    _, d = govern(sample(fake_host(tmp_path, oom_kill=1), now=60.0), state)
    assert d.level is Level.RED and "oom_kill" in d.reasons
    _, d = govern(sample(fake_host(tmp_path, cg_oom_kill=2), now=60.0), state)
    assert d.level is Level.RED and d.metrics["oom_kills"] == 2.0


def test_zram_nearly_full_is_yellow(tmp_path: Path) -> None:
    _, d = govern(
        sample(fake_host(tmp_path, zram_orig=int(7.5 * GIB)), now=0.0),
        {"level": "GREEN", "since": 0.0, "calm": 0},
    )
    assert d.level is Level.YELLOW and d.reasons == ("zram_fill",)


@pytest.mark.parametrize("missing", ["pressure/memory", "meminfo"])
def test_missing_evidence_fails_closed_and_never_improves(tmp_path: Path, missing: str) -> None:
    roots = fake_host(tmp_path)
    (roots.proc / missing).unlink()
    _, d = govern(sample(roots, now=0.0), {"level": "GREEN", "since": -999.0, "calm": 0})
    assert d.level is Level.YELLOW and d.reasons[0].startswith("evidence_missing")
    assert d.evidence_missing and math.isnan(d.pressure)
    assert hp.resource_pressure(d).max_pressure() == 1.0
    _, d = govern(sample(roots, now=60.0), {"level": "RED", "since": -999.0, "calm": 0})
    assert d.level is Level.RED


def test_level_maps_to_governor_pressure() -> None:
    assert effective_cap(4, ResourcePressure(*(hp.LEVEL_PRESSURE[Level.GREEN],) * 3)) == 4
    assert effective_cap(4, ResourcePressure(*(hp.LEVEL_PRESSURE[Level.YELLOW],) * 3)) == 2
    assert effective_cap(4, ResourcePressure(*(hp.LEVEL_PRESSURE[Level.RED],) * 3)) == 1
    assert effective_cap(4, ResourcePressure.unknown()) == 1


def test_current_shares_state_across_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hp, "current", _REAL_CURRENT)
    roots = fake_host(tmp_path / "host")
    path = tmp_path / "home" / "host-pressure.json"
    first = hp.current(roots=roots, state_path=path)
    assert first.level is Level.YELLOW and first.state_path == str(path)
    saved = json.loads(path.read_text())
    assert saved["level"] == "YELLOW" and saved["baseline"]["swap"] == 0
    path.write_text("{not json")
    broken = hp.current(roots=roots, state_path=path)
    assert broken.level is Level.YELLOW and "state:JSONDecodeError" in broken.errors
