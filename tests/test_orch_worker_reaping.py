"""Worker processes end with their attempt (#1363): real subprocesses, bounded waits.

The fourth self-build run left OpenCode worker processes running after both of its nodes had
finished.  The installed OpenCode (2.3.2) starts its server (``opencode serve --stdio``) in a
session and process group of its own, and that server starts the processes of the model's tool
calls (``opencode-pty``, then the shell command) in another: none of them is in the group the
executor started, so signalling that group never reached them, and a tool command the model
left running (a dev server, a background job) outlived the attempt, the worker and the run.

Every process the attempt started carries the attempt's marker in its environment (the children
of a process inherit it).  The fake workers below model that shape with plain Python: a child in
a session of its own, an orphan whose parent has gone, a leader that outlives its final event.
Every assertion is on a PID the test or the fake worker recorded, never on a process name, and
every wait has a deadline.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.test_opencode_executor import (
    NODE_ID,
    ROUTE,
    RUN_ID,
    _pid_gone,
    make_fake_opencode,
    ndjson_success,
    requires_procfs,
)
from tests.test_opencode_executor import _serve_ready as _serve_ready
from tests.test_orch_runtime import _subscription_overlay as _subscription_overlay
from verdict.orchestration.contracts import WorkerTerminal
from verdict.orchestration.executor_opencode import OpenCodeExecutor

pytestmark = requires_procfs

#: The longest any wait here may take; the reaping itself needs well under a second.
_BOUND = 15.0


# ----------------------------------------------------------------------------- helpers

_PRELUDE = """
import signal

REC = {rec!r}


def record(name, value):
    tmp = os.path.join(REC, name + ".tmp")
    with open(tmp, "w") as fh:
        fh.write(str(value))
    os.replace(tmp, os.path.join(REC, name))


def wait_for(name, seconds=30):
    path = os.path.join(REC, name)
    end = time.monotonic() + seconds
    while not os.path.exists(path):
        if time.monotonic() > end:
            sys.exit(97)
        time.sleep(0.02)


def go_quiet():
    fd = os.open(os.devnull, os.O_RDWR)
    for n in (0, 1, 2):
        os.dup2(fd, n)


def sleep_forever():
    while True:
        time.sleep(0.05)


def spawn(name, new_session=True, ignore_term=False, then=None):
    '''A child that records its pid.  ``then`` runs in it before it sleeps (a grandchild).'''
    pid = os.fork()
    if pid:
        return pid
    if new_session:
        os.setsid()
    if ignore_term:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    go_quiet()
    record(name, os.getpid())
    if then is not None:
        then()
    sleep_forever()


record("leader", os.getpid())
record("config_dir", os.environ["OPENCODE_CONFIG_DIR"])
"""

_FINAL_EVENT = "sys.stdout.write({payload!r} + '\\n'); sys.stdout.flush()"


def _fake(tmp_path: Path, rec: Path, body: str) -> str:
    rec.mkdir(exist_ok=True)
    return make_fake_opencode(
        tmp_path, _PRELUDE.format(rec=str(rec)) + textwrap.dedent(body).replace("{FINAL}", _final())
    )


def _final() -> str:
    return _FINAL_EVENT.format(payload=ndjson_success("done"))


def _pid(rec: Path, name: str) -> int:
    return int((rec / name).read_text())


def _executor(fake: str, **kwargs: float) -> OpenCodeExecutor:
    return OpenCodeExecutor(
        opencode_bin=fake,
        env={"HOME": str(Path(fake).parent), "PATH": os.environ["PATH"]},
        **kwargs,
    )


async def _run(executor: OpenCodeExecutor, cwd: Path, timeout: float = 30.0) -> WorkerTerminal:
    return await executor.run(
        "task", route_id=ROUTE, cwd=cwd, timeout_seconds=timeout, run_id=RUN_ID, node_id=NODE_ID
    )


def _wait_gone(pid: int, bound: float = _BOUND) -> bool:
    end = time.monotonic() + bound
    while time.monotonic() < end:
        if _pid_gone(pid):
            return True
        time.sleep(0.02)
    return _pid_gone(pid)


def _kill(rec: Path) -> None:
    """Whatever a failed test left behind: only the PIDs the fake workers recorded."""
    for path in rec.glob("*") if rec.exists() else ():
        if path.name in {"config_dir"} or path.suffix == ".tmp":
            continue
        with contextlib.suppress(ValueError, OSError):
            os.kill(int(path.read_text()), signal.SIGKILL)


@pytest.fixture()
def rec(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "rec"
    path.mkdir()
    try:
        yield path
    finally:
        _kill(path)


@pytest.fixture()
def cwd(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    return work


def _assert_ended(rec: Path, *names: str) -> None:
    for name in names:
        pid = _pid(rec, name)
        assert _wait_gone(pid), f"{name} (pid {pid}) is still running after the attempt"


def _assert_attempt_dir_removed(rec: Path) -> None:
    config_dir = Path((rec / "config_dir").read_text())
    assert config_dir.name == "config" and config_dir.parent.name.startswith("verdict-oc-")
    assert not config_dir.parent.exists(), f"{config_dir.parent} was not removed"


# ----------------------------------------------------------------------------- exit paths


async def test_success_leaves_no_process_of_a_session_of_its_own(rec: Path, cwd: Path) -> None:
    """The server the worker starts has a session of its own, so the group the executor signals
    never held it: a worker that exits 0 with it still running must not leave it behind."""
    fake = _fake(
        tmp_path=rec.parent,
        rec=rec,
        body="""
        spawn("server")
        wait_for("server")
        {FINAL}
        sys.exit(0)
        """,
    )
    terminal = await _run(_executor(fake), cwd)
    assert terminal.ok is True, terminal
    _assert_ended(rec, "leader", "server")
    _assert_attempt_dir_removed(rec)


async def test_success_leaves_no_orphan_of_a_tool_command(rec: Path, cwd: Path) -> None:
    """The shape the real run showed: server -> tool daemon (own session) -> the command the model
    left running, whose parents are gone by the time the worker exits."""
    fake = _fake(
        tmp_path=rec.parent,
        rec=rec,
        body="""
        def daemon():
            spawn("tool_command")
            os._exit(0)  # the daemon goes, its child stays: an orphan in a session of its own

        spawn("server", then=lambda: (spawn("daemon", then=daemon), None))
        wait_for("tool_command")
        {FINAL}
        sys.exit(0)
        """,
    )
    terminal = await _run(_executor(fake), cwd)
    assert terminal.ok is True, terminal
    _assert_ended(rec, "leader", "server", "tool_command")
    _assert_attempt_dir_removed(rec)


async def test_failure_leaves_no_process_behind(rec: Path, cwd: Path) -> None:
    fake = _fake(
        tmp_path=rec.parent,
        rec=rec,
        body="""
        spawn("server")
        spawn("same_group", new_session=False, ignore_term=True)
        wait_for("server"); wait_for("same_group")
        sys.stderr.write("provider failure\\n")
        sys.exit(3)
        """,
    )
    terminal = await _run(_executor(fake), cwd)
    assert terminal.ok is False
    _assert_ended(rec, "leader", "server", "same_group")
    _assert_attempt_dir_removed(rec)


async def test_timeout_leaves_no_process_behind(rec: Path, cwd: Path) -> None:
    fake = _fake(
        tmp_path=rec.parent,
        rec=rec,
        body="""
        spawn("server", ignore_term=True)
        wait_for("server")
        sleep_forever()
        """,
    )
    terminal = await _run(_executor(fake, idle_output_seconds=60.0), cwd, timeout=2.0)
    assert terminal.ok is False and terminal.error == "timeout", terminal
    _assert_ended(rec, "leader", "server")
    _assert_attempt_dir_removed(rec)


async def test_a_leader_that_keeps_running_after_its_final_event_is_stopped(
    rec: Path, cwd: Path
) -> None:
    """The exit status stays authoritative: a worker that prints its final event and does not
    exit is a stall, not a success.  It and its server still end with the attempt."""
    fake = _fake(
        tmp_path=rec.parent,
        rec=rec,
        body="""
        spawn("server", ignore_term=True)
        wait_for("server")
        {FINAL}
        sleep_forever()
        """,
    )
    terminal = await _run(_executor(fake, idle_output_seconds=1.0), cwd, timeout=60.0)
    assert terminal.ok is False and terminal.error.startswith("stalled"), terminal
    _assert_ended(rec, "leader", "server")
    _assert_attempt_dir_removed(rec)


async def test_cancellation_leaves_no_process_behind(rec: Path, cwd: Path) -> None:
    fake = _fake(
        tmp_path=rec.parent,
        rec=rec,
        body="""
        spawn("server", ignore_term=True)
        wait_for("server")
        sleep_forever()
        """,
    )
    task = asyncio.ensure_future(_run(_executor(fake, idle_output_seconds=60.0), cwd))
    end = time.monotonic() + _BOUND
    while not (rec / "server").exists() and time.monotonic() < end:
        await asyncio.sleep(0.02)
    assert (rec / "server").exists(), "the worker never started"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=_BOUND)
    _assert_ended(rec, "leader", "server")
    _assert_attempt_dir_removed(rec)


@pytest.mark.parametrize("leader_ignores_term", [True, False], ids=["in-group", "in-marked"])
async def test_cancelling_again_while_the_attempt_is_ended_still_ends_it(
    rec: Path, cwd: Path, leader_ignores_term: bool
) -> None:
    """A second cancellation that lands inside the cleanup must not leave a process running:
    once while the group is being ended (the leader ignores SIGTERM, so that takes its grace),
    once while the processes outside it are (the leader goes at once; the others ignore it)."""
    fake = _fake(
        tmp_path=rec.parent,
        rec=rec,
        body=f"""
        spawn("server", ignore_term=True)
        spawn("tool", ignore_term=True)
        wait_for("server"); wait_for("tool")
        if {leader_ignores_term!r}:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        sleep_forever()
        """,
    )
    task = asyncio.ensure_future(_run(_executor(fake, idle_output_seconds=60.0), cwd))
    end = time.monotonic() + _BOUND
    while not (rec / "tool").exists() and time.monotonic() < end:
        await asyncio.sleep(0.02)
    task.cancel()
    await asyncio.sleep(0.3)  # inside the cleanup: the SIGTERM grace of one or the other
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=_BOUND)
    _assert_ended(rec, "leader", "server", "tool")


async def test_only_the_attempts_own_processes_are_ended(rec: Path, cwd: Path) -> None:
    """A process the test started, in a session of its own and unrelated to the attempt, is not
    touched: processes are found by the attempt's marker, never by name."""
    bystander = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True
    )
    try:
        fake = _fake(
            tmp_path=rec.parent,
            rec=rec,
            body="""
            spawn("server")
            wait_for("server")
            {FINAL}
            sys.exit(0)
            """,
        )
        terminal = await _run(_executor(fake), cwd)
        assert terminal.ok is True
        _assert_ended(rec, "server")
        assert bystander.poll() is None, "an unrelated process was ended"
    finally:
        bystander.kill()
        bystander.wait()


async def test_two_attempts_end_only_their_own_processes(
    rec: Path, cwd: Path, tmp_path: Path
) -> None:
    """Attempts that run side by side each carry their own marker."""
    rec_a, rec_b = tmp_path / "rec_a", tmp_path / "rec_b"
    long_lived = (
        _fake(
            tmp_path=tmp_path / "bin_a",
            rec=rec_a,
            body="""
        spawn("server", ignore_term=True)
        wait_for("server")
        sleep_forever()
        """,
        )
        if (tmp_path / "bin_a").mkdir() is None
        else ""
    )
    short = (
        _fake(
            tmp_path=tmp_path / "bin_b",
            rec=rec_b,
            body="""
        spawn("server")
        wait_for("server")
        {FINAL}
        sys.exit(0)
        """,
        )
        if (tmp_path / "bin_b").mkdir() is None
        else ""
    )
    try:
        slow = asyncio.ensure_future(_run(_executor(long_lived, idle_output_seconds=60.0), cwd))
        end = time.monotonic() + _BOUND
        while not (rec_a / "server").exists() and time.monotonic() < end:
            await asyncio.sleep(0.02)
        terminal = await _run(_executor(short), cwd)
        assert terminal.ok is True
        _assert_ended(rec_b, "server")
        assert not _pid_gone(_pid(rec_a, "server")), "the other attempt's process was ended"
        slow.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(slow, timeout=_BOUND)
        _assert_ended(rec_a, "leader", "server")
    finally:
        _kill(rec_a)
        _kill(rec_b)


# ----------------------------------------------------------------------------- the orchestrator

_ORCHESTRATOR = """
import asyncio, sys
sys.path.insert(0, {root!r})
from pathlib import Path
import verdict.orchestration.executor_opencode as eo
from tests.test_orch_runtime import Reviewer, Selector
from verdict.orchestration.contracts import WorkGraph, WorkNode
from verdict.orchestration.demo_scenario import _init_repo
from verdict.orchestration.executor_opencode import OpenCodeExecutor
from verdict.orchestration.recovery import FailureIntelligence
from verdict.orchestration.run import run_golden_path
from verdict.orchestration.runtime import RuntimePolicy

eo._serve_token = lambda *a, **k: "fixture-serve-token-0000"
eo.load_assignment_key = lambda *a, **k: bytes(range(32))
eo._check_binary = lambda *a, **k: None
eo._check_serve = lambda *a, **k: None
eo._check_assignment = lambda *a, **k: None

work = Path({work!r})
repo = _init_repo(work)
graph = WorkGraph(
    "g",
    (WorkNode("a", "write a", owned_files=("a.txt",), verification_command=("true",)),),
)
executor = OpenCodeExecutor(
    opencode_bin={fake!r}, env={{"HOME": {home!r}, "PATH": {path!r}}}, idle_output_seconds=600.0
)
asyncio.run(
    run_golden_path(
        "write a",
        repo=repo,
        runs_root=work / "runs",
        selector=Selector(["cc/s"]),
        executor=executor,
        classifier=FailureIntelligence(),
        reviewer=Reviewer(),
        graph=graph,
        run_id="sig1",
        policy=RuntimePolicy(attempt_timeout_seconds=600.0),
    )
)
"""


@pytest.mark.parametrize(
    ("sig", "status"), [(signal.SIGTERM, 143), (signal.SIGINT, 130)], ids=["SIGTERM", "SIGINT"]
)
def test_an_orchestrator_that_is_signalled_leaves_no_worker_behind(
    rec: Path, tmp_path: Path, sig: signal.Signals, status: int
) -> None:
    """``run_golden_path`` in its own process, with a worker (and the worker's server, in its own
    session) running: the signal ends the run and every process of the attempt, and the
    process exits with the status of a process that was signalled."""
    fake = _fake(
        tmp_path=tmp_path,
        rec=rec,
        body="""
        spawn("server", ignore_term=True)
        wait_for("server")
        sleep_forever()
        """,
    )
    script = tmp_path / "orchestrator.py"
    script.write_text(
        _ORCHESTRATOR.format(
            root=str(Path(__file__).resolve().parents[1]),
            work=str(tmp_path / "ws"),
            fake=fake,
            home=str(tmp_path),
            path=os.environ["PATH"],
        )
    )
    (tmp_path / "ws").mkdir()
    proc = subprocess.Popen(
        [sys.executable, str(script)],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        end = time.monotonic() + 60.0
        while not (rec / "server").exists() and proc.poll() is None and time.monotonic() < end:
            time.sleep(0.05)
        assert (rec / "server").exists(), f"the worker never started: {proc.poll()}"
        leader, server = _pid(rec, "leader"), _pid(rec, "server")
        assert not _pid_gone(leader) and not _pid_gone(server)
        proc.send_signal(sig)
        try:
            output, _ = proc.communicate(timeout=_BOUND * 2)
        except subprocess.TimeoutExpired:
            proc.kill()
            output, _ = proc.communicate()
            pytest.fail(f"the orchestrator did not exit after {sig.name}:\n{output[-1500:]}")
        assert _wait_gone(leader), "the worker is still running after the orchestrator exited"
        assert _wait_gone(server), "the worker's server is still running"
        assert proc.returncode == status, (proc.returncode, output[-1500:])
        assert "Traceback" not in output, output[-1500:]
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


@pytest.mark.skipif(
    threading.current_thread() is not threading.main_thread(),
    reason="signal handlers belong to the main thread",
)
async def test_the_signal_handlers_are_the_runs_while_it_lasts_and_the_callers_after() -> None:
    from verdict.orchestration.run import _exit_when_signalled

    before = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    during: dict[signal.Signals, object] = {}

    @_exit_when_signalled
    async def run() -> str:
        during.update({sig: signal.getsignal(sig) for sig in before})
        return "done"

    assert await run() == "done"
    assert all(during[sig] != before[sig] for sig in before), "the run installed no handler"
    assert {sig: signal.getsignal(sig) for sig in before} == before


def test_a_run_in_a_worker_thread_installs_no_handler_and_still_runs() -> None:
    from verdict.orchestration.run import _exit_when_signalled

    seen: dict[str, object] = {}

    @_exit_when_signalled
    async def run() -> str:
        seen["term"] = signal.getsignal(signal.SIGTERM)
        return "done"

    holder: list[str] = []
    before = signal.getsignal(signal.SIGTERM)
    worker = threading.Thread(target=lambda: holder.append(asyncio.run(run())))
    worker.start()
    worker.join(timeout=_BOUND)
    assert holder == ["done"]
    assert seen["term"] == before == signal.getsignal(signal.SIGTERM)
