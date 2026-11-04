"""Dispatch gate: every Prime worker launch validates a current packet first."""

from __future__ import annotations

import asyncio
import json
import runpy
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from tests.test_prime_workflow import module, packet

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / ".prime/agent/skills/verdict-dispatch/scripts/runtime_bridge.py"
UPDATED_AT = "2026-09-13T00:00:00Z"


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        text=True,
    ).strip()


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name)
    _git(repo, "add", name)
    _git(repo, "commit", "-qm", name)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def current(tmp_path: Path) -> tuple[Any, dict[str, Any], Path, Path]:
    """A worktree on its branch, a packet bound to it and the active lease it names."""
    m = module()
    import prime_state

    repo = tmp_path / "wt"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "issue")
    base = _commit(repo, "README")
    state = tmp_path / "state"
    lease = prime_state.acquire_lease(
        state,
        issue="BOD-12",
        dispatch_id="attempt-1",
        worker_id="worker-1",
        worktree=str(repo),
        branch="issue",
        stale_after_seconds=600,
        supervisor_id="test",
        now=time.time(),
    )
    p = packet(repo)
    p["base_sha"] = base
    p["lease"]["generation"] = lease["generation"]
    return m, p, repo, state


def test_current_packet_is_admitted_with_its_lease(current) -> None:
    m, p, repo, state = current
    lease = m.validate_dispatch(p, repo, issue_updated_at=UPDATED_AT, state=state)
    assert lease["dispatch_id"] == "attempt-1"
    assert lease["generation"] == p["lease"]["generation"]


def test_worker_commits_on_top_of_base_stay_current(current) -> None:
    m, p, repo, state = current
    _commit(repo, "work.txt")
    m.validate_dispatch(p, repo, issue_updated_at=UPDATED_AT, state=state)


@pytest.mark.parametrize(
    ("breakage", "message"),
    [
        ("invalid", "exact model required"),
        ("worktree", "worktree mismatch"),
        ("issue_revision", "Linear issue changed"),
        ("branch", "branch is not checked out"),
        ("base", "base_sha is not an ancestor"),
        ("no_lease", "no active issue lease"),
        ("generation", "generation superseded"),
        ("stale_lease", "lease is stale"),
    ],
)
def test_invalid_or_stale_packet_is_refused(current, tmp_path, breakage, message) -> None:
    m, p, repo, state = current
    updated_at = UPDATED_AT
    if breakage == "invalid":
        p["model"] = "auto/best-coding"
    elif breakage == "worktree":
        p["worktree"] = str(tmp_path)
    elif breakage == "issue_revision":
        updated_at = "2026-09-14T00:00:00Z"
    elif breakage == "branch":
        _git(repo, "checkout", "-qb", "other")
    elif breakage == "base":
        p["base_sha"] = "b" * 40
    elif breakage == "no_lease":
        state = tmp_path / "empty-state"
    elif breakage == "generation":
        p["lease"]["generation"] += 1
    else:
        lease_file = state / "leases" / "BOD-12" / "active.json"
        lease = json.loads(lease_file.read_text())
        lease["last_progress_at"] = 0
        lease_file.write_text(json.dumps(lease))
    with pytest.raises(ValueError, match=message):
        m.validate_dispatch(p, repo, issue_updated_at=updated_at, state=state)


class _Handle:
    running = False


def _bridge_start(repo: Path, packet_path: Path, issue_updated_at: str, launches: list[str]):
    module = runpy.run_path(str(BRIDGE))

    def bash(command: str) -> _Handle:
        launches.append(command)
        return _Handle()

    async def run() -> Any:
        return module["start"](
            object(),
            bash,
            str(repo),
            "prompt",
            controller_model="cc/claude-fable-5-1",
            packet=str(packet_path),
            issue_updated_at=issue_updated_at,
        )

    return asyncio.run(run())


def _bridge_fixture(current) -> tuple[dict[str, Any], Path, Path]:
    import prime_state

    _m, p, repo, _state = current
    git_state = repo / ".git" / "verdict-prime"
    lease = prime_state.acquire_lease(
        git_state,
        issue="BOD-12",
        dispatch_id="attempt-1",
        worker_id="worker-1",
        worktree=str(repo),
        branch="issue",
        stale_after_seconds=600,
        supervisor_id="test",
        now=time.time(),
    )
    p["lease"]["generation"] = lease["generation"]
    packet_path = repo.parent / "packet.json"
    packet_path.write_text(json.dumps(p))
    return p, repo, packet_path


def test_bridge_refuses_stale_packet_before_any_worker_launch(current) -> None:
    _p, repo, packet_path = _bridge_fixture(current)
    launches: list[str] = []
    with pytest.raises(ValueError, match="Linear issue changed"):
        _bridge_start(repo, packet_path, "2026-09-14T00:00:00Z", launches)
    assert launches == []
    assert not (repo / ".git" / "verdict-prime" / "worker-runs").exists()


def test_bridge_launches_current_packet_and_binds_its_identity(current) -> None:
    p, repo, packet_path = _bridge_fixture(current)
    launches: list[str] = []
    operation = _bridge_start(repo, packet_path, UPDATED_AT, launches)
    assert len(launches) == 1 and "verdict.worker_runtime" in launches[0]
    config = json.loads((operation.directory / "config.json").read_text())
    assert config["attempt_id"] == "attempt-1"
    assert config["lease_generation"] == p["lease"]["generation"]
    assert config["packet"] == str(packet_path.resolve())


def test_every_worker_runtime_launch_site_calls_validate_dispatch_first() -> None:
    """Caller coverage: a new dispatcher that skips the gate fails this test."""
    launch = "-m verdict.worker_runtime"
    sites = [
        path
        for top in ("verdict", "scripts", ".prime")
        for path in sorted((ROOT / top).rglob("*.py"))
        if launch in path.read_text(encoding="utf-8")
    ]
    assert sites == [BRIDGE], sites
    for path in sites:
        text = path.read_text(encoding="utf-8")
        gate = text.find('["validate_dispatch"](')
        assert 0 <= gate < text.find(launch), f"{path} launches a worker before validate_dispatch"
