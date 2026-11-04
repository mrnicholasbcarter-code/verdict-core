"""Dispatch gate: every Prime worker launch validates a current packet first."""

from __future__ import annotations

import ast
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
    state = repo / ".git" / "verdict-prime"  # where the bridge looks
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
        ("released_lease", "lease is not active"),
        ("other_attempt", "does not match the hydrated packet attempt"),
    ],
)
def test_invalid_or_stale_packet_is_refused(current, tmp_path, breakage, message) -> None:
    m, p, repo, state = current
    _break(p, repo, state, tmp_path, breakage)
    updated_at = "2026-09-14T00:00:00Z" if breakage == "issue_revision" else UPDATED_AT
    if breakage == "no_lease":
        state = tmp_path / "empty-state"
    with pytest.raises(ValueError, match=message):
        m.validate_dispatch(p, repo, issue_updated_at=updated_at, state=state)


def _break(p: dict[str, Any], repo: Path, state: Path, tmp_path: Path, breakage: str) -> None:
    lease_file = state / "leases" / "BOD-12" / "active.json"
    lease = json.loads(lease_file.read_text())
    if breakage == "invalid":
        p["model"] = "auto/best-coding"
    elif breakage == "worktree":
        p["worktree"] = str(tmp_path)
    elif breakage == "branch":
        _git(repo, "checkout", "-qb", "other")
    elif breakage == "base":
        p["base_sha"] = "b" * 40
    elif breakage == "generation":
        p["lease"]["generation"] += 1
    elif breakage == "stale_lease":
        lease["last_progress_at"] = 0
    elif breakage == "released_lease":
        lease["status"] = "RELEASED"
    elif breakage == "other_attempt":
        lease["dispatch_id"] = "attempt-2"
    lease_file.write_text(json.dumps(lease))


class _Handle:
    running = False


def _bridge_start(repo: Path, p: dict[str, Any], issue_updated_at: str, launches: list[str]):
    packet_path = repo.parent / "packet.json"
    packet_path.write_text(json.dumps(p))
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


@pytest.mark.parametrize(
    ("breakage", "message"),
    [
        ("issue_revision", "Linear issue changed"),
        ("released_lease", "lease is not active"),
        ("base", "base_sha is not an ancestor"),
    ],
)
def test_bridge_refuses_before_any_worker_launch(current, tmp_path, breakage, message) -> None:
    _m, p, repo, state = current
    _break(p, repo, state, tmp_path, breakage)
    updated_at = "2026-09-14T00:00:00Z" if breakage == "issue_revision" else UPDATED_AT
    launches: list[str] = []
    with pytest.raises(ValueError, match=message):
        _bridge_start(repo, p, updated_at, launches)
    assert launches == []
    assert not (state / "worker-runs").exists()


def test_bridge_launches_current_packet_and_binds_its_identity(current) -> None:
    _m, p, repo, _state = current
    launches: list[str] = []
    operation = _bridge_start(repo, p, UPDATED_AT, launches)
    assert len(launches) == 1 and "verdict.worker_runtime" in launches[0]
    config = json.loads((operation.directory / "config.json").read_text())
    assert config["attempt_id"] == "attempt-1"
    assert config["lease_generation"] == p["lease"]["generation"]
    assert config["packet"] == str((repo.parent / "packet.json").resolve())


#: Launch sites allowed without the gate, each with the reason. Keep this list empty-biased.
UNGATED_ALLOWED = {
    # Library compat entry: runs a caller-supplied callback, not a Prime worker process;
    # no production caller (docs/quality/REACHABILITY-TRIAGE-2026-10.md).
    ("verdict/subagent_selection.py", "execute_with_worker_failover")
}


def _is_gate_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "validate_dispatch"
    if isinstance(func, ast.Attribute):
        return func.attr == "validate_dispatch"
    return (
        isinstance(func, ast.Subscript)
        and isinstance(func.slice, ast.Constant)
        and func.slice.value == "validate_dispatch"
    )


def _is_launch(node: ast.AST) -> bool:
    """A worker launch: any string naming the runtime module, or an in-process controller."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return "verdict.worker_runtime" in node.value
    if isinstance(node, ast.Call):
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        return name == "WorkerController"
    return False


def _launch_sites(source: str) -> list[tuple[str, int, bool]]:
    """(enclosing function, launch line, gated) for every launch in ``source``.

    Gated means a ``validate_dispatch`` call executes earlier in the same function body.
    Docstrings and comments are not calls, so mentioning the gate never satisfies it.
    """
    tree = ast.parse(source)
    scopes = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    docstrings = {
        id(n.body[0].value)
        for n in [tree, *ast.walk(tree)]
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and n.body
        and isinstance(n.body[0], ast.Expr)
        and isinstance(n.body[0].value, ast.Constant)
    }
    sites = []
    for node in ast.walk(tree):
        if not _is_launch(node) or id(node) in docstrings:
            continue
        line = node.lineno
        enclosing = [s for s in scopes if s.lineno <= line <= (s.end_lineno or s.lineno)]
        if not enclosing:
            sites.append(("<module>", line, False))
            continue
        scope = max(enclosing, key=lambda s: s.lineno)
        gated = any(
            _is_gate_call(n) and n.lineno < line for stmt in scope.body for n in ast.walk(stmt)
        )
        sites.append((scope.name, line, gated))
    return sites


def test_launch_detector_flags_ungated_argv_fstring_and_in_process_launches() -> None:
    bad = (
        "'''Module docstring naming verdict.worker_runtime is not a launch.'''\n"
        "run(['python', '-m', 'verdict.worker_runtime'])\n"
        "def a(run):\n    run(['python', '-m', 'verdict.worker_runtime', d])\n"
        "def b(bash):\n    # validate_dispatch is mentioned only in a comment\n"
        "    bash(f'.venv/bin/python -m verdict.worker_runtime {d}')\n"
        "def c():\n    return WorkerController(task)\n"
        "def d(bash, wf):\n    bash('python -m verdict.worker_runtime x')\n"
        "    wf['validate_dispatch'](p)\n"
    )
    assert sorted((name, gated) for name, _line, gated in _launch_sites(bad)) == [
        ("<module>", False),
        ("a", False),
        ("b", False),
        ("c", False),
        ("d", False),
    ]
    good = (
        "def e(bash, wf):\n    wf['validate_dispatch'](p)\n    bash('-m verdict.worker_runtime')\n"
    )
    assert [gated for _n, _l, gated in _launch_sites(good)] == [True]


def test_every_worker_runtime_launch_site_calls_validate_dispatch_first() -> None:
    """Caller coverage: a new dispatcher that skips the gate fails this test."""
    found = {}
    for top in ("verdict", "scripts", ".prime"):
        for path in sorted((ROOT / top).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if rel == "verdict/worker_runtime.py":
                continue  # the launched runtime itself
            for name, line, gated in _launch_sites(path.read_text(encoding="utf-8")):
                found[(rel, name, line)] = gated
    bridge = BRIDGE.relative_to(ROOT).as_posix()
    assert any(rel == bridge and gated for (rel, _n, _l), gated in found.items())
    ungated = {(rel, name) for (rel, name, _l), gated in found.items() if not gated}
    assert ungated <= UNGATED_ALLOWED, sorted(ungated - UNGATED_ALLOWED)
