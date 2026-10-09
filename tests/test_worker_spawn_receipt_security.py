"""Receipt security and real post-admission cleanup regressions."""

import json
import os
import stat
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from tests.test_worker_runtime import OK, Adapter, controller, row
from verdict.admission import RuntimeEvidence, admit
from verdict.subagent_selection import HealthCache, HealthResult, WorkerTask
from verdict.worker_runtime import WorkerController


def runtime(tmp_path: Path, adapter: Adapter | None = None) -> WorkerController:
    return controller(tmp_path, adapter or Adapter([OK, OK]), run_dir=tmp_path)


@pytest.mark.parametrize("link", ["symlink", "hardlink"])
async def test_receipt_link_refused_without_changing_victim(tmp_path: Path, link: str) -> None:
    victim = tmp_path / "victim"
    victim.write_bytes(b"private victim bytes\n")
    victim.chmod(0o644)
    receipts = tmp_path / "spawn-receipts.jsonl"
    if link == "symlink":
        receipts.symlink_to(victim)
    else:
        os.link(victim, receipts)
    adapter = Adapter([OK, OK])
    outcome = await runtime(tmp_path, adapter).run("task")
    assert outcome.state == "FAIL_CLOSED"
    assert victim.read_bytes() == b"private victim bytes\n"
    assert stat.S_IMODE(victim.stat().st_mode) == 0o644
    assert [call[0] for call in adapter.calls] == ["spawn", "delete"]


def test_fifo_refused_without_blocking(tmp_path: Path) -> None:
    receipts = tmp_path / "spawn-receipts.jsonl"
    os.mkfifo(receipts, 0o644)
    ctrl = runtime(tmp_path)
    # A reader lets the old blocking open return. Assert the required flags
    # separately so red-first cannot hang the entire pytest process.
    reader = os.open(receipts, os.O_RDONLY | os.O_NONBLOCK)
    flags_seen: list[int] = []
    original = os.open
    with pytest.MonkeyPatch.context() as patch:

        def capture(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            if str(path).endswith("spawn-receipts.jsonl"):
                flags_seen.append(flags)
            return original(path, flags, *args, **kwargs)

        patch.setattr(os, "open", capture)
        start = time.monotonic()
        try:
            with pytest.raises((OSError, ValueError)):
                ctrl.spawn_receipt(1, ctrl.candidates[0], "medium", {}, None)
        finally:
            os.close(reader)
        assert time.monotonic() - start < 1
    assert flags_seen and all(flags & os.O_NONBLOCK for flags in flags_seen)
    assert stat.S_IMODE(receipts.stat().st_mode) == 0o644


@pytest.mark.parametrize("ancestor", [False, True])
async def test_receipt_refuses_symlinked_run_directory(tmp_path: Path, ancestor: bool) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    link = tmp_path / "link"
    link.symlink_to(actual, target_is_directory=True)
    directory = link
    if ancestor:
        (actual / "nested").mkdir()
        directory = link / "nested"
    ctrl = controller(tmp_path, Adapter([OK, OK]), run_dir=directory)
    assert (await ctrl.run("task")).state == "FAIL_CLOSED"
    assert not (directory / "spawn-receipts.jsonl").exists()


def test_concurrent_receipts_remain_complete_private_jsonl(tmp_path: Path) -> None:
    ctrl = runtime(tmp_path)
    reason = {"payload": "x" * 32000}

    def append(attempt: int) -> None:
        ctrl.spawn_receipt(attempt, ctrl.candidates[0], "medium", reason, None)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(append, range(40)))
    path = tmp_path / "spawn-receipts.jsonl"
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert sorted(record["attempt"] for record in records) == list(range(40))
    assert all(record["reason"] == reason for record in records)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("cleanup_failure", [False, True])
async def test_receipt_disk_failure_reaps_real_admitted_handle_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cleanup_failure: bool
) -> None:
    rows = [row("p000/free"), row("p001/free"), row("dead/free")]
    connections = [
        {"provider": provider, "isActive": True, "testStatus": "ok"}
        for provider in ("p000", "p001")
    ]
    admitted = admit(rows, connections, RuntimeEvidence(), now=datetime.now(timezone.utc))
    adapter = Adapter([OK, OK], cleanup_failure=cleanup_failure)
    ctrl = WorkerController(
        WorkerTask(),
        inventory_rows=rows,
        prime_selectors=[f"omniroute/{r['id']}" for r in rows],
        probe=lambda _: HealthResult(True, "healthy"),
        adapter=adapter,
        cache=HealthCache(tmp_path / "health.json"),
        run_dir=tmp_path,
        admitted=admitted,
        require_admission=True,
    )
    assert [c.route_id for c in ctrl.candidates] == ["p000/free", "p001/free"]
    deleted: list[dict[str, Any]] = []
    original_delete = adapter.delete

    async def capture_delete(handle: dict[str, Any]) -> None:
        deleted.append(dict(handle))
        await original_delete(handle)

    adapter.delete = capture_delete  # type: ignore[method-assign]

    def disk_full(fd: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "fsync", disk_full)
    outcome = await ctrl.run("same task")
    assert outcome.state == "FAIL_CLOSED"
    assert [call[0] for call in adapter.calls] == ["spawn", "delete"]
    assert len(adapter.prompts) == 1  # Successful spawn count, not a setup failure.
    assert len(deleted) == 1 and deleted[0]["rlm_child_id"].startswith("verdict-")
    assert deleted[0]["model"] == "omniroute/p000/free"
    assert ctrl.attempts == [("omniroute/p000/free", "local_persistence_failure")]
    assert ctrl.cache.usable("omniroute/p000/free", now=ctrl.now()).healthy
    assert "provider:p000" not in ctrl.cache._records
    failure = next(e for e in ctrl.events if e["event"] == "failure")
    assert failure["cooldown_seconds"] == 0 and failure["provider_wide"] is False
    assert failure["replacement"] is False and failure["excluded"] is False
    assert ("cleanup_unconfirmed" if cleanup_failure else "local_persistence_failure") in (
        outcome.diagnostic
    )
    assert ctrl.admitted and ctrl.admitted.launchable("p000/free")
    assert "dead/free" not in ctrl.admitted


def test_receipt_lock_covers_write_flush_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fcntl

    held: set[int] = set()
    original_flock, original_fsync = fcntl.flock, os.fsync

    def capture_lock(fd: int, operation: int) -> None:
        original_flock(fd, operation)
        if operation == fcntl.LOCK_EX:
            held.add(fd)
        elif operation == fcntl.LOCK_UN:
            held.remove(fd)

    def capture_sync(fd: int) -> None:
        if stat.S_ISREG(os.fstat(fd).st_mode):
            assert fd in held, "receipt fsync must still be under the append lock"
        original_fsync(fd)

    monkeypatch.setattr(fcntl, "flock", capture_lock)
    monkeypatch.setattr(os, "fsync", capture_sync)
    ctrl = runtime(tmp_path)
    ctrl.spawn_receipt(1, ctrl.candidates[0], "medium", {}, None)
    assert not held


async def test_receipt_refuses_shared_writable_run_directory(tmp_path: Path) -> None:
    directory = tmp_path / "shared"
    directory.mkdir(mode=0o777)
    directory.chmod(0o777)
    ctrl = controller(tmp_path, Adapter([OK, OK]), run_dir=directory)
    assert (await ctrl.run("task")).state == "FAIL_CLOSED"
    assert not (directory / "spawn-receipts.jsonl").exists()


def test_receipt_syncs_parent_only_for_first_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = os.fsync
    synced: list[str] = []

    def capture(fd: int) -> None:
        synced.append("dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        original(fd)

    monkeypatch.setattr(os, "fsync", capture)
    ctrl = runtime(tmp_path)
    ctrl.spawn_receipt(1, ctrl.candidates[0], "medium", {}, None)
    ctrl.spawn_receipt(2, ctrl.candidates[0], "medium", {}, None)
    assert synced == ["file", "dir", "file"]
