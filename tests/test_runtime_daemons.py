from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from verdict.runtime_daemons import (
    OwnershipRecord,
    ProcessSnapshot,
    RuntimeManager,
    RuntimeManagerError,
    RuntimeServiceSpec,
)


class FakeInspector:
    def __init__(self, processes: list[ProcessSnapshot]) -> None:
        self.processes = processes

    def snapshots(self) -> tuple[ProcessSnapshot, ...]:
        return tuple(self.processes)

    def snapshot(self, pid: int) -> ProcessSnapshot | None:
        return next((item for item in self.processes if item.pid == pid), None)


def spec() -> RuntimeServiceSpec:
    return RuntimeServiceSpec(
        service_id="fixture",
        display_name="Fixture service",
        kind="mcp",
        endpoint="http://127.0.0.1:29999/mcp",
        health_endpoint="http://127.0.0.1:29999/healthz",
        launcher_env="FIXTURE_COMMAND",
        command_markers=("fixture-service", "29999"),
        state_filename="fixture.ownership.json",
        pid_filename="fixture.pid",
        lock_filename="fixture.lock",
        global_state_root="/home/test/.fixture",
    )


def process(pid: int, *, start: str = "start", command: str = "fixture-service") -> ProcessSnapshot:
    return ProcessSnapshot(
        pid=pid,
        uid=1000,
        start_time=start,
        argv=("/home/test/bin/" + command, "--port", "29999"),
        cwd="/home/test/project",
    )


def manager(
    tmp_path: Path, inspector: FakeInspector, *, port_probe: object | None = None
) -> RuntimeManager:
    return RuntimeManager(
        state_dir=tmp_path / "runtime",
        specs=(spec(),),
        inspector=inspector,
        uid=1000,
        home=Path("/home/test"),
        port_probe=port_probe if port_probe is not None else (lambda _endpoint: False),
        health_probe=lambda _endpoint: "unavailable",
    )


def write_owner(runtime: RuntimeManager, current: ProcessSnapshot) -> None:
    runtime.state_dir.mkdir(parents=True)
    (runtime.state_dir / "fixture.ownership.json").write_text(
        json.dumps(
            OwnershipRecord(
                service_id="fixture",
                pid=current.pid,
                uid=1000,
                start_time=current.start_time or "",
                argv_hash=current.argv_hash,
                endpoint=spec().endpoint,
                created_at=1.0,
            ).to_dict()
        ),
        encoding="utf-8",
    )


def test_status_is_read_only_and_reports_unavailable_without_state(tmp_path: Path) -> None:
    runtime = manager(tmp_path, FakeInspector([]))

    report = runtime.status()

    assert report.status == "ready"
    assert report.services[0]["status"] == "unavailable"
    assert report.services[0]["health"] == "unavailable"
    assert not (tmp_path / "runtime").exists()


def test_plan_is_deterministic_and_proposes_only_proven_duplicate(tmp_path: Path) -> None:
    canonical = process(101)
    duplicate = process(102, start="duplicate")
    inspector = FakeInspector([canonical, duplicate])
    runtime = manager(tmp_path, inspector)
    write_owner(runtime, canonical)

    first = runtime.reconcile_plan().to_dict()
    second = runtime.reconcile_plan().to_dict()

    assert first == second
    assert first["status"] == "blocked"
    assert first["actions"] == [
        {
            "action": "stop-duplicate",
            "argv_hash": duplicate.argv_hash,
            "pid": 102,
            "reason": "contract markers and owner identity match; canonical owner is recorded separately",
            "service_id": "fixture",
            "start_time": "duplicate",
        }
    ]
    assert {item["classification"] for item in first["processes"]} == {"canonical", "duplicate"}


@pytest.mark.parametrize(
    "changed",
    [
        lambda current: replace(current, start_time="pid-reused"),
        lambda current: replace(current, argv=("/home/test/bin/unrelated", "--port", "29999")),
    ],
)
def test_plan_refuses_pid_reuse_or_command_mismatch(tmp_path: Path, changed: object) -> None:
    canonical = process(101)
    inspector = FakeInspector([changed(canonical)])  # type: ignore[operator]
    runtime = manager(tmp_path, inspector)
    write_owner(runtime, canonical)

    report = runtime.reconcile_plan()

    assert report.actions == ()
    assert report.status == "blocked"
    assert "fixture:ambiguous-process-identity" in report.errors


def test_apply_requires_consent_and_revalidates_identity(tmp_path: Path) -> None:
    canonical = process(101)
    duplicate = process(102, start="duplicate")
    inspector = FakeInspector([canonical, duplicate])
    runtime = manager(tmp_path, inspector)
    write_owner(runtime, canonical)

    with pytest.raises(RuntimeManagerError, match="explicit consent"):
        runtime.reconcile_apply(service_ids=["fixture"], consent=False)

    killed: list[int] = []

    def fake_kill(pid: int, _signal: int) -> None:
        killed.append(pid)
        inspector.processes = [item for item in inspector.processes if item.pid != pid]

    import verdict.runtime_daemons as runtime_daemons

    original_kill = runtime_daemons.os.kill
    runtime_daemons.os.kill = fake_kill
    try:
        report = runtime.reconcile_apply(service_ids=["fixture"], consent=True)
    finally:
        runtime_daemons.os.kill = original_kill

    assert killed == [102]
    assert report.status == "ready"


def test_apply_refuses_ambiguous_plan_without_signaling(tmp_path: Path) -> None:
    canonical = process(101)
    ambiguous = replace(process(102, start="ambiguous"), uid=200)
    inspector = FakeInspector([canonical, ambiguous])
    runtime = manager(tmp_path, inspector)
    write_owner(runtime, canonical)

    import verdict.runtime_daemons as runtime_daemons

    original_kill = runtime_daemons.os.kill
    runtime_daemons.os.kill = lambda *_args: pytest.fail("ambiguous process was signaled")
    try:
        report = runtime.reconcile_apply(service_ids=["fixture"], consent=True)
    finally:
        runtime_daemons.os.kill = original_kill

    assert report.status == "blocked"
    assert report.actions == ()


def test_apply_rejects_unknown_scope_and_lock_contention(tmp_path: Path) -> None:
    runtime = manager(tmp_path, FakeInspector([]))

    with pytest.raises(RuntimeManagerError, match="known --service"):
        runtime.reconcile_apply(service_ids=["unknown"], consent=True)

    first_lock = runtime._lock(spec())
    first_lock.__enter__()
    try:
        with pytest.raises(RuntimeManagerError, match="lock-contention"):
            second_lock = runtime._lock(spec())
            second_lock.__enter__()
    finally:
        first_lock.__exit__(None, None, None)


def test_start_rejects_uncontracted_launcher_without_spawning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = manager(tmp_path, FakeInspector([]))
    monkeypatch.setenv("FIXTURE_COMMAND", "node unrelated-service --port 29999")

    with pytest.raises(RuntimeManagerError, match="does not match"):
        runtime.start("fixture")


def test_port_collision_is_explicit_and_unrelated_process_is_ignored(tmp_path: Path) -> None:
    unrelated = ProcessSnapshot(
        pid=999,
        uid=1000,
        start_time="other",
        argv=("/home/test/bin/node", "./mcp/server.mjs", "--port", "29999"),
        cwd="/home/test/project",
    )
    runtime = manager(tmp_path, FakeInspector([unrelated]), port_probe=lambda _endpoint: True)

    report = runtime.status()

    assert report.status == "blocked"
    assert report.actions == ()
    assert report.processes == ()
    assert "fixture:port-collision" in report.errors


def test_ownership_record_is_private_and_versioned(tmp_path: Path) -> None:
    current = process(101)
    runtime = manager(tmp_path, FakeInspector([current]))
    write_owner(runtime, current)

    payload = json.loads((runtime.state_dir / "fixture.ownership.json").read_text())

    assert payload["contract_version"] == "1"
    assert "fixture-service" not in json.dumps(payload)
    assert not payload.get("environment")
