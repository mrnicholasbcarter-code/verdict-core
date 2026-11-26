"""A serve denial of the assigned route ends the worker attempt at once (#1366, part 1).

``verdict serve`` answers a refused assigned route with HTTP 503 and the text ``assigned route
'<id>' denied: <code> (<detail>)``.  OpenCode 2.3.2 retries a 5xx ten times and prints one
``retry`` event for each, so an attempt on a refused route used to run until OpenCode gave up
(four to seven minutes in the fifth self-build run).  The executor now ends it on the first
denial with a permanent code, or on the second identical denial for any other code.

These tests run the real executor against fake workers (plain Python scripts, the shape of
``tests/test_orch_worker_reaping.py``): a child in a session of its own that the group the
executor started never held, then the ``retry`` events, then silence.  Every assertion is on a
PID the fake recorded or on the terminal; every wait has a deadline.  The detector itself is
tested in ``tests/test_opencode_denial.py``.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import sys
import textwrap
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from tests.test_opencode_executor import (
    NODE_ID,
    ROUTE,
    RUN_ID,
    _pid_gone,
    make_fake_opencode,
    requires_procfs,
)
from tests.test_opencode_executor import _serve_ready as _serve_ready
from tests.test_opencode_signed_assignment import empty_serve_database
from verdict.free_tier_admit import REASON_PROVIDER_UNHEALTHY
from verdict.orchestration.contracts import WorkerTerminal
from verdict.orchestration.executor_opencode import OpenCodeExecutor
from verdict.orchestration.opencode_denial import PERMANENT_DENIAL_CODES

pytestmark = requires_procfs

FIXTURES = Path(__file__).parent / "fixtures" / "opencode_stdout"

#: The longest any wait here may take; ending an attempt needs well under a second.
_BOUND = 15.0
#: The acceptance bar of #1366: the attempt is over this long after the first denial.
_DENIAL_TO_END_SECONDS = 5.0

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


def emit(text):
    sys.stdout.write(text)
    sys.stdout.flush()


record("leader", os.getpid())
record("config_dir", os.environ["OPENCODE_CONFIG_DIR"])
spawn("server")  # OpenCode's own server: a session of its own, which SIGTERM does not move
wait_for("server")
"""


def _denial(code: str, route: str = ROUTE, detail: str = "the refusal's own detail") -> str:
    """The text ``IntelligenceService._assigned_denial_reason`` writes."""
    return f"assigned route {route!r} denied: {code} ({detail})"


def _retry(message: str, *, status: int = 503, attempt: int = 2) -> str:
    """One OpenCode ``retry`` event: the shape of ``v2_3_2_error_503.jsonl``, with serve's body."""
    body = json.dumps({"error": {"message": message, "type": "api_error"}}, separators=(",", ":"))
    error = {
        "type": "provider.internal",
        "message": message,
        "status": status,
        "response": {"body": body},
    }
    return json.dumps({"type": "retry", "sessionID": "ses_x", "attempt": attempt, "error": error})


def _worker(rec: Path, steps: str) -> str:
    """A fake OpenCode: its server running in a session of its own, then ``steps``."""
    rec.mkdir(exist_ok=True)
    return make_fake_opencode(rec.parent, _PRELUDE.format(rec=str(rec)) + textwrap.dedent(steps))


def _executor(
    fake: str, *, receipts_db_provider: Callable[[], Path | str | None] | None = None
) -> OpenCodeExecutor:
    return OpenCodeExecutor(
        opencode_bin=fake,
        env={"HOME": str(Path(fake).parent), "PATH": os.environ["PATH"]},
        idle_output_seconds=60.0,
        receipts_db_provider=receipts_db_provider,
    )


async def _run(
    executor: OpenCodeExecutor, cwd: Path, *, route: str = ROUTE, timeout: float = 30.0
) -> WorkerTerminal:
    return await executor.run(
        "task", route_id=route, cwd=cwd, timeout_seconds=timeout, run_id=RUN_ID, node_id=NODE_ID
    )


def _pid(rec: Path, name: str) -> int:
    return int((rec / name).read_text())


def _wait_gone(pid: int, bound: float = _BOUND) -> bool:
    end = time.monotonic() + bound
    while time.monotonic() < end:
        if _pid_gone(pid):
            return True
        time.sleep(0.02)
    return _pid_gone(pid)


def _assert_ended(rec: Path, *names: str) -> None:
    for name in names:
        pid = _pid(rec, name)
        assert _wait_gone(pid), f"{name} (pid {pid}) is still running after the attempt"


def _assert_attempt_dir_removed(rec: Path) -> None:
    config_dir = Path((rec / "config_dir").read_text())
    assert not config_dir.parent.exists(), f"{config_dir.parent} was not removed"


def _kill(rec: Path) -> None:
    """Whatever a failed test left behind: only the PIDs the fake workers recorded."""
    for path in rec.glob("*") if rec.exists() else ():
        if path.name in {"config_dir", "denied_at", "second_sent"} or path.suffix == ".tmp":
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


# ----------------------------------------------------------------------------- the first denial


@pytest.mark.parametrize("code", sorted(PERMANENT_DENIAL_CODES))
async def test_a_permanent_denial_ends_the_attempt_and_everything_it_started(
    rec: Path, cwd: Path, code: str
) -> None:
    """The fake never exits and prints nothing after the denial: only the executor can end it."""
    fake = _worker(
        rec,
        f"""
        record("denied_at", time.time())
        emit({_retry(_denial(code))!r} + "\\n")
        sleep_forever()
        """,
    )
    terminal = await _run(_executor(fake), cwd, timeout=20.0)
    ended_after = time.time() - float((rec / "denied_at").read_text())
    assert ended_after < _DENIAL_TO_END_SECONDS, f"ended {ended_after:.1f}s after the denial"
    assert terminal.ok is False
    assert terminal.error.startswith(f"assigned route {ROUTE!r} denied: {code}"), terminal.error
    assert terminal.status_code == 503
    _assert_ended(rec, "leader", "server")
    _assert_attempt_dir_removed(rec)


async def test_a_denial_split_over_two_writes_is_still_seen(rec: Path, cwd: Path) -> None:
    """Pipe reads cut anywhere: the first half of the line alone is not a denial."""
    line = _retry(_denial("opaque_route_disallowed")) + "\n"
    half = len(line) // 2
    fake = _worker(
        rec,
        f"""
        record("denied_at", time.time())
        emit({line[:half]!r})
        time.sleep(0.5)
        emit({line[half:]!r})
        sleep_forever()
        """,
    )
    terminal = await _run(_executor(fake), cwd, timeout=20.0)
    ended_after = time.time() - float((rec / "denied_at").read_text())
    assert ended_after < _DENIAL_TO_END_SECONDS + 0.5
    assert terminal.error.startswith(f"assigned route {ROUTE!r} denied: opaque_route_disallowed")
    assert terminal.status_code == 503
    _assert_ended(rec, "leader", "server")


async def test_the_serve_denial_fixture_ends_the_attempt(rec: Path, cwd: Path) -> None:
    """The fixture is serve's text and body, in OpenCode's retry events (see its README row)."""
    fixture = (FIXTURES / "v2_3_2_retry_503_serve_denial.jsonl").read_text()
    fake = _worker(rec, f"emit({fixture!r})\nsleep_forever()\n")
    route = "cc/example-model"
    terminal = await _run(_executor(fake), cwd, route=route, timeout=20.0)
    assert terminal.ok is False
    assert terminal.error.startswith(f"assigned route {route!r} denied: opaque_route_disallowed")
    assert terminal.status_code == 503
    _assert_ended(rec, "leader", "server")


# ----------------------------------------------------------------------------- the second denial


async def test_a_denial_that_may_pass_ends_the_attempt_at_the_second_identical_one(
    rec: Path, cwd: Path
) -> None:
    """``provider_unhealthy`` can clear by the next try: one denial is not the end, two are.

    The fake records ``second_sent`` before it prints the second denial; the file exists only if
    the executor let the attempt run past the first one."""
    line = _retry(_denial(REASON_PROVIDER_UNHEALTHY)) + "\n"
    fake = _worker(
        rec,
        f"""
        emit({line!r})
        time.sleep(1.5)
        record("second_sent", time.time())
        emit({line!r})
        sleep_forever()
        """,
    )
    terminal = await _run(_executor(fake), cwd, timeout=20.0)
    assert (rec / "second_sent").exists(), "the attempt was ended on the first denial"
    assert terminal.ok is False
    assert terminal.error.startswith(
        f"assigned route {ROUTE!r} denied: {REASON_PROVIDER_UNHEALTHY}"
    ), terminal.error
    assert terminal.status_code == 503
    _assert_ended(rec, "leader", "server")


# ----------------------------------------------------------------------------- not a denial


@pytest.mark.parametrize(
    "message_and_status",
    [
        pytest.param(("fake upstream 503", 503), id="a-503-that-is-not-a-denial"),
        pytest.param((_denial("opaque_route_disallowed", "cc/other-model"), 503), id="other-route"),
        pytest.param((_denial("opaque_route_disallowed"), 502), id="other-status"),
    ],
)
async def test_what_is_not_a_denial_of_this_route_does_not_end_the_attempt(
    rec: Path, cwd: Path, message_and_status: tuple[str, int]
) -> None:
    """The attempt ends as it does today: at the attempt timeout, with no status and no code."""
    message, status = message_and_status
    line = _retry(message, status=status) + "\n"
    fake = _worker(rec, f"emit({line * 3!r})\nsleep_forever()\n")
    started = time.monotonic()
    terminal = await _run(_executor(fake), cwd, timeout=2.0)
    assert time.monotonic() - started >= 2.0, "the attempt ended before its timeout"
    assert terminal.ok is False
    assert terminal.error == "timeout", terminal.error
    assert terminal.status_code is None
    _assert_ended(rec, "leader", "server")


async def test_a_plain_503_capture_still_ends_the_way_it_did(rec: Path, cwd: Path) -> None:
    """The real capture of ten retries and a final error, then exit 1: OpenCode's own verdict."""
    capture = (FIXTURES / "v2_3_2_error_503.jsonl").read_text()
    fake = _worker(rec, f"emit({capture!r})\nsys.exit(1)\n")
    terminal = await _run(_executor(fake), cwd, timeout=20.0)
    assert terminal.ok is False
    assert "fake upstream 503" in terminal.error
    assert not terminal.error.startswith("assigned route")
    assert terminal.status_code == 503
    _assert_ended(rec, "leader", "server")


# ----------------------------------------------------------------------------- the terminal


async def test_the_denial_terminal_keeps_what_the_other_endings_carry(
    rec: Path, cwd: Path, tmp_path: Path
) -> None:
    """Serve's receipts are read back after the worker is gone, as for a stall or a timeout.

    The fake plays serve here: it writes a root under its own assignment's digest (the database
    the executor reads), then is denied."""
    db = empty_serve_database(tmp_path)
    root = str(Path(__file__).resolve().parent.parent)
    line = _retry(_denial("opaque_route_disallowed")) + "\n"
    fake = _worker(
        rec,
        f"""
        import hashlib, subprocess
        digest = hashlib.sha256(os.environ["VERDICT_WORKER_API_KEY"].split(".")[1].encode())
        seed = (
            "import sys; sys.path.insert(0, {root!r}); from pathlib import Path; "
            "from tests.test_opencode_signed_assignment import seed_serve_root; "
            "seed_serve_root(Path(sys.argv[1]), sys.argv[2], [], 1)"
        )
        subprocess.check_call([{sys.executable!r}, "-c", seed, {str(db)!r}, digest.hexdigest()])
        emit({line!r})
        sleep_forever()
        """,
    )
    executor = _executor(fake, receipts_db_provider=lambda: db)
    terminal = await _run(executor, cwd, timeout=60.0)
    assert terminal.error.startswith("assigned route"), terminal
    assert terminal.status_code == 503
    assert (terminal.ok, terminal.executor_kind, terminal.harness) == (False, "live", "opencode")
    assert terminal.model == ROUTE
    assert terminal.duration_seconds > 0
    assert terminal.worker_assignment is not None
    serve = terminal.worker_assignment["serve_receipts"]
    assert (serve["status"], serve["requests"]) == ("read", 1)
    assert len(terminal.worker_assignment["assignment_digest"]) == 64
    _assert_ended(rec, "leader", "server")
