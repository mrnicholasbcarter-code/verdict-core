"""Gateway lifecycle coverage: ensure-ready without duplicates.

Every test is offline and process-free. Probes, launchers, clocks and sleeps are
fakes; the state directory is a tmp_path. Nothing opens a socket, nothing starts
a real gateway, and nothing touches the operator's ~/.verdict or the shared
gateway on 127.0.0.1:20128.
"""

from __future__ import annotations

import fcntl
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from verdict import gateway_lifecycle as gl
from verdict.provider_bootstrap import (
    BootstrapError,
    BootstrapResult,
    ProviderBinding,
    resolve_provider_bootstrap,
)

GATEWAY_URL = "http://127.0.0.1:29999/v1"
#: Mirrors gl._MAX_BACKOFF_S; asserted against so a raised cap cannot hide a
#: wait that runs past its deadline.
_MAX_BACKOFF = 1.0


@pytest.fixture(autouse=True)
def _no_live_gateway(no_gateway_network: None) -> None:
    """Enforce the module docstring: nothing here opens a socket.

    The real-process tests below start throwaway children that bind no port, so
    the guard stays on for the whole module.
    """


def _result(
    *,
    gateway_url: str | None = GATEWAY_URL,
    required: bool = True,
    start_command: tuple[str, ...] | None = ("fake-gateway", "serve"),
    timeout: float = 5.0,
    config_path: Path | None = None,
) -> BootstrapResult:
    """Build a BootstrapResult directly: lifecycle never reads configuration."""
    providers = (
        {
            "omniroute": ProviderBinding(
                name="omniroute",
                base_url=gateway_url or "http://127.0.0.1:29999/v1",
                source="config_file",
                api_key_env="OMNIROUTE_API_KEY",
            )
        }
        if required
        else {
            "public_ollama": ProviderBinding(
                name="public_ollama", base_url="http://localhost:11434/v1", source="config_file"
            )
        }
    )
    return BootstrapResult(
        primary_model="anthropic/claude-opus-5",
        providers=providers,
        gateway_required=required,
        gateway_url=gateway_url,
        profile="development",
        log_path="decisions.jsonl",
        config_path=config_path or Path("/nonexistent/verdict.yaml"),
        config_present=False,
        field_sources={"gateway_url": "config_file", "gateway_start_command": "config_file"},
        gateway_start_command=start_command,
        gateway_ready_timeout_s=timeout,
    )


class FakeClock:
    """Monotonic clock advanced only by the injected sleep, with a watchdog.

    The watchdog is the point: an unbounded wait loop (``while True`` instead of
    ``while clock() < deadline``) would otherwise spin until the CI job's own
    timeout, which reports as a hang rather than as a failure, and this repo has no
    pytest timeout plugin. Reading the clock more times than any bounded wait
    plausibly needs raises instead, so the defect fails in milliseconds with a
    message that names it.

    Every test in this module that injects a clock gets this, so the protection
    cannot be bypassed by a test that forgets to ask for it.
    """

    def __init__(self, *, max_reads: int = 2000) -> None:
        self.now = 0.0
        self.reads = 0
        self.slept: list[float] = []
        self.max_reads = max_reads

    def __call__(self) -> float:
        self.reads += 1
        if self.reads > self.max_reads:
            raise AssertionError(
                f"the wait read the clock {self.reads} times without terminating: it is not "
                f"bounded by the deadline"
            )
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds
        if len(self.slept) > self.max_reads:
            raise AssertionError(
                f"the wait slept {len(self.slept)} times without terminating: it is not "
                f"bounded by the deadline"
            )


class FakeProbe:
    """Scripted health answers; the last answer repeats forever."""

    def __init__(self, *answers: gl.GatewayHealth) -> None:
        self.answers = list(answers)
        self.calls: list[str] = []

    def __call__(self, url: str) -> gl.GatewayHealth:
        self.calls.append(url)
        index = min(len(self.calls) - 1, len(self.answers) - 1)
        return self.answers[index]


class FakeProcess:
    """Process handle that never runs anything."""

    def __init__(self, *, exit_code: int | None = None) -> None:
        self._exit_code = exit_code
        self.pid = 4242
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self._exit_code

    def terminate(self) -> None:
        self.terminated = True
        self._exit_code = -15

    def kill(self) -> None:
        self.killed = True
        self._exit_code = -9


class CountingLauncher:
    """Counts launches. Duplicate prevention is asserted against this count."""

    def __init__(self, process: Any = None, *, error: Exception | None = None) -> None:
        self.launches: list[tuple[tuple[str, ...], Path]] = []
        self.process = process
        self.error = error

    def launch(self, argv: tuple[str, ...], *, cwd: Path) -> Any:
        self.launches.append((argv, cwd))
        if self.error is not None:
            raise self.error
        return self.process if self.process is not None else FakeProcess()


HEALTHY = gl.GatewayHealth(reachable=True, responded=True, detail="inventory reachable")
CLOSED = gl.GatewayHealth(reachable=False, responded=False, detail="connection refused")
RESPONDING_SICK = gl.GatewayHealth(reachable=False, responded=True, detail="HTTP 503")


def _ensure(result: BootstrapResult, probe: Any, tmp_path: Path, **kwargs: Any) -> Any:
    clock = kwargs.pop("clock", None) or FakeClock()
    return gl.ensure_gateway_ready(
        result, probe=probe, clock=clock, sleep=clock.sleep, state_dir=tmp_path / "state", **kwargs
    )


# --- not required -----------------------------------------------------------


def test_not_required_never_probes_and_never_launches(tmp_path: Path) -> None:
    probe = FakeProbe(CLOSED)
    launcher = CountingLauncher()
    outcome = _ensure(_result(required=False), probe, tmp_path, launcher=launcher)
    assert outcome.state == "not_required"
    assert outcome.ready is True
    assert probe.calls == []
    assert launcher.launches == []


# --- already running --------------------------------------------------------


def test_healthy_gateway_is_reused_and_never_relaunched(tmp_path: Path) -> None:
    probe = FakeProbe(HEALTHY)
    launcher = CountingLauncher()
    outcome = _ensure(_result(), probe, tmp_path, launcher=launcher)
    assert outcome.state == "already_ready"
    assert outcome.launched is False
    assert launcher.launches == []
    assert probe.calls == [GATEWAY_URL]
    assert not (tmp_path / "state").exists()


def test_healthy_gateway_found_under_the_lock_is_not_relaunched(tmp_path: Path) -> None:
    # The first probe misses, then a concurrent owner finishes; the re-probe
    # under the lock must win over launching a duplicate.
    probe = FakeProbe(CLOSED, HEALTHY)
    launcher = CountingLauncher()
    outcome = _ensure(_result(), probe, tmp_path, launcher=launcher)
    assert outcome.state == "already_ready"
    assert launcher.launches == []


# --- startup success --------------------------------------------------------


def test_absent_gateway_is_started_once_and_reported_started(tmp_path: Path) -> None:
    probe = FakeProbe(CLOSED, CLOSED, HEALTHY)
    launcher = CountingLauncher()
    outcome = _ensure(_result(), probe, tmp_path, launcher=launcher)
    assert outcome.state == "started"
    assert outcome.launched is True
    assert outcome.ready is True
    assert len(launcher.launches) == 1
    assert launcher.launches[0][0] == ("fake-gateway", "serve")


def test_starting_is_reported_to_the_observer_before_the_terminal_state(tmp_path: Path) -> None:
    probe = FakeProbe(CLOSED, CLOSED, HEALTHY)
    seen: list[str] = []
    outcome = _ensure(
        _result(),
        probe,
        tmp_path,
        launcher=CountingLauncher(),
        observer=lambda item: seen.append(item.state),
    )
    assert "starting" in seen
    assert seen[-1] == "started" == outcome.state


# --- startup failure --------------------------------------------------------


def test_missing_start_command_names_the_field_and_never_guesses(tmp_path: Path) -> None:
    probe = FakeProbe(CLOSED)
    launcher = CountingLauncher()
    outcome = _ensure(
        _result(start_command=None, config_path=tmp_path / "verdict.yaml"),
        probe,
        tmp_path,
        launcher=launcher,
    )
    assert outcome.state == "failed_to_start"
    assert outcome.ready is False
    assert launcher.launches == []
    assert outcome.diagnostic is not None
    assert outcome.diagnostic.code == "gateway_start_command_missing"
    assert outcome.diagnostic.diagnostic_class == "gateway_health"
    assert outcome.diagnostic.field == "gateway_start_command"
    assert "gateway_start_command" in outcome.diagnostic.remediation
    assert "VERDICT_GATEWAY_START_COMMAND" in outcome.diagnostic.remediation


def test_launch_error_is_a_precise_gateway_health_diagnostic(tmp_path: Path) -> None:
    launcher = CountingLauncher(error=FileNotFoundError("fake-gateway"))
    outcome = _ensure(_result(), FakeProbe(CLOSED), tmp_path, launcher=launcher)
    assert outcome.state == "failed_to_start"
    assert outcome.diagnostic is not None
    assert outcome.diagnostic.code == "gateway_start_failed"
    assert outcome.diagnostic.diagnostic_class == "gateway_health"
    assert len(launcher.launches) == 1


def test_process_that_exits_before_readiness_is_failed_to_start(tmp_path: Path) -> None:
    launcher = CountingLauncher(FakeProcess(exit_code=3))
    outcome = _ensure(_result(), FakeProbe(CLOSED), tmp_path, launcher=launcher)
    assert outcome.state == "failed_to_start"
    assert outcome.launched is True
    assert outcome.diagnostic is not None
    assert outcome.diagnostic.code == "gateway_process_exited"
    assert "3" in outcome.diagnostic.detail


def test_remote_gateway_is_never_started(tmp_path: Path) -> None:
    launcher = CountingLauncher()
    outcome = _ensure(
        _result(gateway_url="http://gateway.internal:20128/v1"),
        FakeProbe(CLOSED),
        tmp_path,
        launcher=launcher,
    )
    assert outcome.state == "failed_to_start"
    assert launcher.launches == []
    assert outcome.diagnostic is not None
    assert outcome.diagnostic.code == "gateway_remote_not_managed"


def test_required_gateway_without_a_url_fails_closed(tmp_path: Path) -> None:
    outcome = _ensure(_result(gateway_url=None), FakeProbe(CLOSED), tmp_path)
    assert outcome.state == "failed_to_start"
    assert outcome.diagnostic is not None
    assert outcome.diagnostic.code == "gateway_required_but_absent"


# --- unhealthy --------------------------------------------------------------


def test_responding_but_unhealthy_gateway_is_reported_not_duplicated(tmp_path: Path) -> None:
    launcher = CountingLauncher()
    outcome = _ensure(_result(), FakeProbe(RESPONDING_SICK), tmp_path, launcher=launcher)
    assert outcome.state == "unhealthy"
    assert outcome.ready is False
    assert launcher.launches == []
    assert outcome.diagnostic is not None
    assert outcome.diagnostic.code == "gateway_unhealthy"


def test_launched_process_that_answers_but_never_recovers_is_unhealthy(tmp_path: Path) -> None:
    process = FakeProcess()
    launcher = CountingLauncher(process)
    outcome = _ensure(
        _result(timeout=1.0), FakeProbe(CLOSED, RESPONDING_SICK), tmp_path, launcher=launcher
    )
    assert outcome.state == "unhealthy"
    assert outcome.launched is True
    assert process.terminated is True
    assert outcome.diagnostic is not None
    assert outcome.diagnostic.code == "gateway_unhealthy"


# --- timeout and boundedness ------------------------------------------------


def test_readiness_wait_is_bounded_and_leaves_no_orphan(tmp_path: Path) -> None:
    process = FakeProcess()
    launcher = CountingLauncher(process)
    clock = FakeClock()
    outcome = _ensure(
        _result(timeout=2.0), FakeProbe(CLOSED), tmp_path, launcher=launcher, clock=clock
    )
    assert outcome.state == "failed_to_start"
    assert outcome.diagnostic is not None
    assert outcome.diagnostic.code == "gateway_ready_timeout"
    assert process.terminated is True, "a process we started must not be left running"
    assert clock.now <= 4.0, "the wait must respect the configured deadline"
    assert outcome.attempts < 40, "probing must back off rather than spin"


def test_backoff_grows_and_is_capped(tmp_path: Path) -> None:
    clock = FakeClock()
    _ensure(
        _result(timeout=5.0), FakeProbe(CLOSED), tmp_path, launcher=CountingLauncher(), clock=clock
    )
    assert clock.slept[0] == pytest.approx(0.1)
    assert clock.slept[1] == pytest.approx(0.2)
    assert max(clock.slept) == pytest.approx(1.0)


def test_probe_exception_is_treated_as_unreachable_not_as_a_crash(tmp_path: Path) -> None:
    def exploding(url: str) -> gl.GatewayHealth:
        raise OSError("connection refused")

    outcome = _ensure(
        _result(start_command=None, config_path=tmp_path / "verdict.yaml"), exploding, tmp_path
    )
    assert outcome.state == "failed_to_start"
    assert outcome.diagnostic is not None
    assert outcome.diagnostic.code == "gateway_start_command_missing"


# --- the start command is never echoed --------------------------------------


def test_no_diagnostic_or_serialization_carries_the_start_argv(tmp_path: Path) -> None:
    """A token on the start command line must not reach a diagnostic or a receipt.

    The launch failure path is the dangerous one: an exception raised by the
    launcher can quote the argv it was handed.
    """
    argv = ("fake-gateway", "serve", "--api-key", "sk-SECRETTOKEN123")
    launcher = CountingLauncher(
        error=FileNotFoundError(f"[Errno 2] No such file or directory: {argv!r}")
    )
    outcome = _ensure(_result(start_command=argv), FakeProbe(CLOSED), tmp_path, launcher=launcher)

    assert outcome.state == "failed_to_start"
    assert outcome.diagnostic is not None
    assert outcome.diagnostic.code == "gateway_start_failed"
    for rendered in (
        repr(outcome.to_dict()),
        outcome.describe(),
        repr(outcome.diagnostic.to_dict()),
    ):
        assert "sk-SECRETTOKEN123" not in rendered
        assert "--api-key" not in rendered
    # The binary is still named, so the operator can act on the report.
    assert "fake-gateway" in outcome.diagnostic.detail
    assert launcher.launches[0][0] == argv, "the runtime argv is unchanged"


def test_the_start_command_is_absent_from_every_outcome_field(tmp_path: Path) -> None:
    """Even on the success path, no outcome field carries the argv."""
    argv = ("fake-gateway", "--token", "sk-ANOTHERSECRET")
    outcome = _ensure(
        _result(start_command=argv),
        FakeProbe(CLOSED, CLOSED, HEALTHY),
        tmp_path,
        launcher=CountingLauncher(),
    )

    assert outcome.state == "started"
    assert "sk-ANOTHERSECRET" not in repr(outcome.to_dict())
    assert "sk-ANOTHERSECRET" not in outcome.describe()
    assert "--token" not in repr(outcome.to_dict())


def test_scrubbing_removes_a_short_argument_that_is_part_of_a_longer_one() -> None:
    """Longest-first replacement, so no fragment of a token survives."""
    argv = ("gw", "--key", "sk-AB", "--other", "sk-ABCDEF")
    scrubbed = gl._scrub_argv("failed: sk-ABCDEF and sk-AB via --key", argv)
    assert "sk-ABCDEF" not in scrubbed
    assert "sk-AB" not in scrubbed
    assert "--key" not in scrubbed


# --- real-process reaping ---------------------------------------------------


def _proc_state(pid: int) -> str | None:
    """Kernel state letter for ``pid``, or ``None`` when the pid is gone.

    Reads ``/proc`` rather than signalling, so an already-reaped pid that the
    kernel has recycled cannot be mistaken for our child.
    """
    try:
        stat_line = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    return stat_line.rpartition(")")[2].split()[0]


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="needs procfs")
def test_timeout_reaps_the_real_child_and_kills_its_descendants(tmp_path: Path) -> None:
    """A real launched process, and everything it spawned, is gone afterwards.

    This is the one test that uses a real process. It is a throwaway
    ``sh -c`` wrapper around ``sys.executable -c 'time.sleep(...)'``, which spawns
    a grandchild, so it reproduces the shape of a gateway started through a
    wrapper (``npx``, ``node``, ``sh``). No port is bound, no socket is opened and
    no gateway is contacted: the probe is a fake that always reports closed, so the
    readiness budget always expires.

    Two failures are asserted separately, because the old implementation had both:
    a leader left as an un-reaped zombie (no ``wait()``), and a live grandchild (only
    the leader was signalled, never the group).
    """
    marker = tmp_path / "grandchild.pid"
    script = (
        f"{shlex.quote(sys.executable)} -c 'import time; time.sleep(30)' & "
        f"echo $! > {shlex.quote(str(marker))}; wait"
    )
    probe = FakeProbe(CLOSED)
    launcher = gl.SubprocessGatewayLauncher()
    launches: list[Any] = []

    class RecordingRealLauncher:
        def launch(self, argv: tuple[str, ...], *, cwd: Path) -> Any:
            handle = launcher.launch(argv, cwd=cwd)
            launches.append(handle)
            return handle

    outcome = gl.ensure_gateway_ready(
        _result(start_command=("sh", "-c", script), timeout=2.0),
        probe=probe,
        launcher=RecordingRealLauncher(),
        state_dir=tmp_path / "state",
    )

    assert outcome.state == "failed_to_start"
    assert outcome.launched is True
    assert outcome.diagnostic is not None
    assert outcome.diagnostic.code == "gateway_ready_timeout"
    assert len(launches) == 1
    child = launches[0]
    assert isinstance(child, gl.LaunchedGateway)
    assert child.pgid == child.process.pid, "the launcher must own a new process group"

    leader = child.process.pid
    assert _proc_state(leader) != "Z", (
        f"pid {leader} is an un-reaped zombie: the timeout path must wait() for the child "
        f"it started"
    )
    assert child.process.returncode is not None, "the child must be reaped, not merely signalled"

    deadline = time.monotonic() + 10.0
    grandchild: int | None = None
    while time.monotonic() < deadline:
        text = marker.read_text(encoding="utf-8").strip() if marker.exists() else ""
        if text:
            grandchild = int(text)
            break
        time.sleep(0.05)
    assert grandchild is not None, "the wrapper never recorded its grandchild"
    while time.monotonic() < deadline and _proc_state(grandchild) not in (None, "Z"):
        time.sleep(0.05)
    assert _proc_state(grandchild) in (None, "Z"), (
        f"grandchild {grandchild} is still running: the whole process group we created must be "
        f"signalled, not only the leader"
    )
    assert "no orphan remains" in (outcome.diagnostic.detail or "")


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="needs procfs")
def test_a_child_that_ignores_sigterm_is_killed_and_reaped(tmp_path: Path) -> None:
    """SIGTERM is not enough on its own, so the stop path must escalate to SIGKILL.

    Uses a real throwaway ``sh -c 'trap "" TERM; ...'`` child, which ignores
    SIGTERM by contract. No port, no socket, no gateway. The grace period is
    shortened so the escalation is observed in well under a second.
    """
    marker = tmp_path / "grandchild.pid"
    script = (
        f"trap \"\" TERM; {shlex.quote(sys.executable)} -c 'import time; time.sleep(30)' & "
        f"echo $! > {shlex.quote(str(marker))}; wait"
    )
    child = gl.SubprocessGatewayLauncher().launch(("sh", "-c", script), cwd=tmp_path)
    assert isinstance(child, gl.LaunchedGateway)
    assert child.pgid == child.process.pid
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and not (marker.exists() and marker.read_text().strip()):
        time.sleep(0.05)
    grandchild = int(marker.read_text(encoding="utf-8").strip())

    stopped = gl._stop_launched(child, grace_s=0.3)

    assert "killed and reaped" in stopped, stopped
    assert "no orphan remains" in stopped
    assert child.process.returncode is not None
    assert _proc_state(child.process.pid) != "Z"
    while time.monotonic() < deadline and _proc_state(grandchild) not in (None, "Z"):
        time.sleep(0.05)
    assert _proc_state(grandchild) in (None, "Z"), (
        f"grandchild {grandchild} survived: SIGTERM alone does not stop a child that ignores it, "
        f"so the stop path must escalate to SIGKILL on the group"
    )


def test_the_stop_diagnostic_states_what_actually_happened() -> None:
    """The sentence in the diagnostic reports the real result, never a fixed claim."""

    class Wedged:
        """Ignores every signal: nothing we do makes it exit."""

        pid = 99991
        returncode = None

        def poll(self) -> int | None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            raise subprocess.TimeoutExpired("wedged", timeout or 0.0)

        def terminate(self) -> None:
            return None

        def kill(self) -> None:
            return None

    wedged = gl._stop_launched(gl.LaunchedGateway(process=Wedged()), grace_s=0.01)
    assert "still running" in wedged
    assert "99991" in wedged
    assert "no orphan remains" not in wedged

    class StopsOnTerm:
        pid = 99992

        def __init__(self) -> None:
            self._code: int | None = None

        def poll(self) -> int | None:
            return self._code

        def wait(self, timeout: float | None = None) -> int:
            return self._code if self._code is not None else 0

        def terminate(self) -> None:
            self._code = -15

        def kill(self) -> None:  # pragma: no cover - SIGTERM already worked
            self._code = -9

    polite = gl._stop_launched(gl.LaunchedGateway(process=StopsOnTerm()), grace_s=0.01)
    assert "stopped on SIGTERM and was reaped" in polite
    assert "no orphan remains" in polite


def test_only_a_process_group_we_created_is_ever_signalled(monkeypatch: pytest.MonkeyPatch) -> None:
    """A child that is not its own group leader is signalled individually.

    Without this, the lifecycle could ``killpg`` a group it did not create — the
    operator's own shell job, in the worst case.
    """
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(gl.os, "killpg", lambda pgid, number: signalled.append((pgid, number)))

    class NotALeader:
        pid = 4242

        def poll(self) -> int | None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            return -15

        def terminate(self) -> None:
            signalled.append((-1, signal.SIGTERM))

        def kill(self) -> None:  # pragma: no cover - SIGTERM works here
            signalled.append((-1, signal.SIGKILL))

    monkeypatch.setattr(gl.os, "getpgid", lambda pid: pid + 1)
    assert gl._own_process_group(NotALeader()) is None

    gl._stop_launched(gl.LaunchedGateway(process=NotALeader(), pgid=None), grace_s=0.01)
    assert signalled == [(-1, signal.SIGTERM)], "no killpg may be issued for an unowned group"


def test_a_process_that_exits_early_still_has_its_group_reaped(tmp_path: Path) -> None:
    """A wrapper that exits before readiness must not leave its descendants behind."""
    stopped: list[str] = []

    class ExitedLeader:
        pid = 7777
        returncode = 0

        def poll(self) -> int | None:
            return 0

        def wait(self, timeout: float | None = None) -> int:
            stopped.append("waited")
            return 0

        def terminate(self) -> None:  # pragma: no cover - already exited
            raise AssertionError("an exited leader must not be signalled again")

        def kill(self) -> None:  # pragma: no cover - already exited
            raise AssertionError("an exited leader must not be signalled again")

    launcher = CountingLauncher(gl.LaunchedGateway(process=ExitedLeader(), pgid=None))
    outcome = _ensure(_result(), FakeProbe(CLOSED), tmp_path, launcher=launcher)

    assert outcome.state == "failed_to_start"
    assert outcome.diagnostic is not None
    assert outcome.diagnostic.code == "gateway_process_exited"
    assert stopped == ["waited"], "an exited child must still be reaped"
    assert "reaped" in outcome.diagnostic.detail


# --- duplicate prevention under concurrency ---------------------------------


def test_two_concurrent_callers_launch_exactly_one_gateway(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    launches: list[tuple[str, ...]] = []
    launch_lock = threading.Lock()
    ready = threading.Event()
    both_in_flight = threading.Barrier(2, timeout=10)

    class SharedLauncher:
        def launch(self, argv: tuple[str, ...], *, cwd: Path) -> Any:
            with launch_lock:
                launches.append(argv)
            ready.set()
            return FakeProcess()

    def probe(url: str) -> gl.GatewayHealth:
        return HEALTHY if ready.is_set() else CLOSED

    outcomes: dict[str, Any] = {}

    def run(name: str) -> None:
        both_in_flight.wait()
        outcomes[name] = gl.ensure_gateway_ready(
            _result(timeout=10.0),
            probe=probe,
            launcher=SharedLauncher(),
            state_dir=state_dir,
            sleep=lambda seconds: None,
        )

    threads = [threading.Thread(target=run, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
        assert not thread.is_alive()

    assert len(launches) == 1, f"exactly one launch expected, saw {launches}"
    assert set(outcomes) == {"a", "b"}
    assert all(item.ready for item in outcomes.values())
    assert all(item.state in {"started", "already_ready"} for item in outcomes.values())
    assert sum(1 for item in outcomes.values() if item.launched) <= 1


def test_the_lock_alone_prevents_a_duplicate_when_no_probe_ever_succeeds(tmp_path: Path) -> None:
    """The exclusive lock, not the re-probe, is what prevents the second launch.

    The probe never reports healthy, so the re-probe under the lock cannot mask a
    broken lock: if both callers become owners, both launch. Ordering is enforced
    by an event rather than by timing -- the launcher holds the lock until the
    other caller has entered the contender path -- so the assertion is
    deterministic rather than a race the scheduler usually wins.

    This is the test that kills a removed flock, LOCK_EX downgraded to LOCK_SH,
    and no-lock-plus-no-reprobe. The pre-existing race test above exercises the
    re-probe; this one isolates the lock.
    """
    state_dir = tmp_path / "state"
    launches: list[tuple[str, ...]] = []
    record = threading.Lock()
    contender_arrived = threading.Event()
    both_started = threading.Barrier(2, timeout=30)

    class SequencedLauncher:
        """Holds the launch lock until the other caller has decided what it is."""

        def launch(self, argv: tuple[str, ...], *, cwd: Path) -> Any:
            with record:
                launches.append(argv)
            # A correct implementation makes the other caller a contender, which
            # sets this event. A broken lock makes it a second owner, which never
            # does; the bounded wait then expires and the launch count exposes it.
            contender_arrived.wait(timeout=10.0)
            return FakeProcess()

    def probe(url: str) -> gl.GatewayHealth:
        return CLOSED

    def observe(outcome: Any) -> None:
        if outcome.waited_for_owner:
            contender_arrived.set()

    outcomes: dict[str, Any] = {}
    failures: list[BaseException] = []

    def run(name: str) -> None:
        try:
            both_started.wait()
            outcomes[name] = gl.ensure_gateway_ready(
                _result(timeout=1.0),
                probe=probe,
                launcher=SequencedLauncher(),
                state_dir=state_dir,
                observer=observe,
            )
        except BaseException as exc:  # surfaced below; a thread must not die silently
            failures.append(exc)

    threads = [threading.Thread(target=run, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive()

    assert failures == []
    assert len(launches) == 1, (
        f"exactly one launch expected, saw {len(launches)}: without an exclusive lock both "
        f"callers become owners, and no probe ever reports healthy to hide it"
    )
    assert sum(1 for item in outcomes.values() if item.launched) == 1
    assert sum(1 for item in outcomes.values() if item.waited_for_owner) == 1
    assert set(outcomes) == {"a", "b"}


def test_a_contender_waits_and_never_launches_when_the_owner_hangs(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    lock_path = gl.gateway_lock_path(GATEWAY_URL, state_dir=state_dir)
    launcher = CountingLauncher()
    clock = FakeClock()
    import fcntl

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        outcome = gl.ensure_gateway_ready(
            _result(timeout=1.0),
            probe=FakeProbe(CLOSED),
            launcher=launcher,
            clock=clock,
            sleep=clock.sleep,
            state_dir=state_dir,
        )
    assert outcome.state == "failed_to_start"
    assert outcome.waited_for_owner is True
    assert outcome.launched is False
    assert launcher.launches == [], "a contender must never launch a second gateway"
    assert outcome.diagnostic is not None
    assert outcome.diagnostic.code == "gateway_ready_timeout"


def test_a_contender_reports_started_when_the_owner_succeeds(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    lock_path = gl.gateway_lock_path(GATEWAY_URL, state_dir=state_dir)
    launcher = CountingLauncher()
    clock = FakeClock()
    import fcntl

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        outcome = gl.ensure_gateway_ready(
            _result(timeout=5.0),
            probe=FakeProbe(CLOSED, HEALTHY),
            launcher=launcher,
            clock=clock,
            sleep=clock.sleep,
            state_dir=state_dir,
        )
    assert outcome.state == "started"
    assert outcome.waited_for_owner is True
    assert outcome.launched is False
    assert launcher.launches == []


def test_the_contender_wait_is_bounded_by_the_same_deadline(tmp_path: Path) -> None:
    """A contender stops at the owner's deadline, and a watchdog proves it terminates.

    The lock is pre-held, so this caller takes the contender path. The watchdog
    clock turns an unbounded loop into a fast failure instead of a hang, which is
    what the review asked for: with no pytest timeout plugin in this repo, a hang
    is indistinguishable from an infrastructure stall.
    """
    state_dir = tmp_path / "state"
    lock_path = gl.gateway_lock_path(GATEWAY_URL, state_dir=state_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    clock = FakeClock()
    launcher = CountingLauncher()

    with lock_path.open("a+", encoding="utf-8") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        outcome = gl.ensure_gateway_ready(
            _result(timeout=3.0),
            probe=FakeProbe(CLOSED),
            launcher=launcher,
            clock=clock,
            sleep=clock.sleep,
            state_dir=state_dir,
        )

    assert outcome.state == "failed_to_start"
    assert outcome.waited_for_owner is True
    assert outcome.diagnostic is not None
    assert outcome.diagnostic.code == "gateway_ready_timeout"
    assert launcher.launches == []
    # The deadline is the owner's budget, so the contender cannot outlive it. With
    # capped 1.0s backoff, a 3s budget cannot take more than a handful of probes.
    assert clock.now <= 3.0 + _MAX_BACKOFF, f"the wait ran past the deadline: {clock.now}"
    assert outcome.attempts <= 8, f"probing must back off, saw {outcome.attempts} attempts"
    assert max(clock.slept) <= _MAX_BACKOFF


def test_the_launch_wait_is_bounded_by_the_deadline_too(tmp_path: Path) -> None:
    """The same watchdog applies to the owner's own readiness wait."""
    clock = FakeClock()
    process = FakeProcess()

    outcome = _ensure(
        _result(timeout=3.0),
        FakeProbe(CLOSED),
        tmp_path,
        launcher=CountingLauncher(process),
        clock=clock,
    )

    assert outcome.state == "failed_to_start"
    assert clock.now <= 3.0 + _MAX_BACKOFF
    assert outcome.attempts <= 8


def test_a_zero_budget_terminates_immediately_without_probing_forever(tmp_path: Path) -> None:
    """A budget that has already expired must not become an unbounded wait."""
    clock = FakeClock()
    outcome = _ensure(
        _result(timeout=0.001),
        FakeProbe(CLOSED),
        tmp_path,
        launcher=CountingLauncher(),
        clock=clock,
    )

    assert outcome.state == "failed_to_start"
    assert outcome.attempts <= 4


def test_lock_path_is_per_url_and_under_the_state_dir(tmp_path: Path) -> None:
    first = gl.gateway_lock_path("http://127.0.0.1:20128/v1", state_dir=tmp_path)
    second = gl.gateway_lock_path("http://127.0.0.1:29999/v1", state_dir=tmp_path)
    assert first != second
    assert first.parent == tmp_path
    # A trailing slash is the same gateway, so it must not get a second lock.
    assert gl.gateway_lock_path("http://127.0.0.1:20128/v1/", state_dir=tmp_path) == first


def test_aliased_loopback_urls_share_one_lock(tmp_path: Path) -> None:
    """localhost, 127.0.0.1 and [::1] on the same port are one gateway, so one lock.

    Two processes configured with different spellings of the same listening socket
    must not both believe they are the launcher.
    """
    same = [
        "http://localhost:20128/v1",
        "http://127.0.0.1:20128/v1",
        "http://[::1]:20128/v1",
        "http://127.0.0.1:20128",
        "http://127.0.0.1:20128/",
        "http://LOCALHOST:20128/v1",
    ]
    paths = {gl.gateway_lock_path(url, state_dir=tmp_path) for url in same}
    assert len(paths) == 1, f"aliases of one gateway must share a lock, got {paths}"

    # A different port, and a non-loopback host, are different gateways.
    assert gl.gateway_lock_path("http://127.0.0.1:29999/v1", state_dir=tmp_path) not in paths
    assert gl.gateway_lock_path("http://gateway.internal:20128/v1", state_dir=tmp_path) not in paths


def test_lock_key_defaults_the_port_by_scheme_and_keeps_a_real_path() -> None:
    """The canonical key names the socket, not the spelling of the URL."""
    assert gl.gateway_lock_key("http://localhost/v1") == gl.gateway_lock_key("http://127.0.0.1:80")
    assert gl.gateway_lock_key("https://example.com") == gl.gateway_lock_key(
        "https://example.com:443/v1"
    )
    assert gl.gateway_lock_key("http://127.0.0.1:20128/gw/v1") != gl.gateway_lock_key(
        "http://127.0.0.1:20128/v1"
    )
    # An unparsable value still gets a stable lock of its own rather than sharing one.
    assert gl.gateway_lock_key("not a url") == "not a url"


def test_a_symlinked_lock_path_is_refused_and_never_followed(tmp_path: Path) -> None:
    """A symlinked lock must not be opened, created through, or chmodded through."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    target = tmp_path / "victim.txt"
    target.write_text("do not touch", encoding="utf-8")
    target.chmod(0o644)
    lock = gl.gateway_lock_path(GATEWAY_URL, state_dir=state_dir)
    lock.symlink_to(target)

    launcher = CountingLauncher()
    outcome = gl.ensure_gateway_ready(
        _result(),
        probe=FakeProbe(CLOSED),
        launcher=launcher,
        state_dir=state_dir,
        sleep=lambda seconds: None,
    )

    assert outcome.state == "failed_to_start"
    assert outcome.diagnostic is not None
    assert outcome.diagnostic.code == "gateway_start_failed"
    assert outcome.diagnostic.diagnostic_class == "gateway_health"
    assert "symbolic link" in outcome.diagnostic.detail
    assert launcher.launches == [], "a gateway must not be launched through an unsafe lock"
    assert target.stat().st_mode & 0o777 == 0o644, "the symlink target must not be re-permissioned"
    assert target.read_text(encoding="utf-8") == "do not touch"


def test_a_symlinked_state_dir_is_refused(tmp_path: Path) -> None:
    """The lock must not be created inside a directory someone else can redirect."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    state_dir = tmp_path / "state"
    state_dir.symlink_to(elsewhere, target_is_directory=True)

    outcome = gl.ensure_gateway_ready(
        _result(),
        probe=FakeProbe(CLOSED),
        launcher=CountingLauncher(),
        state_dir=state_dir,
        sleep=lambda seconds: None,
    )

    assert outcome.state == "failed_to_start"
    assert outcome.diagnostic is not None
    assert outcome.diagnostic.code == "gateway_start_failed"
    assert "symbolic link" in outcome.diagnostic.detail
    assert list(elsewhere.iterdir()) == [], "nothing may be written through the symlink"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_an_unwritable_state_dir_is_a_diagnostic_not_a_traceback(tmp_path: Path) -> None:
    """A PermissionError must reach the operator as gateway_health, never as a crash.

    The CLI catches BootstrapError only, so a raw OSError here would print a
    traceback at the operator.
    """
    parent = tmp_path / "readonly"
    parent.mkdir()
    parent.chmod(0o500)
    try:
        outcome = gl.ensure_gateway_ready(
            _result(),
            probe=FakeProbe(CLOSED),
            launcher=CountingLauncher(),
            state_dir=parent / "state",
            sleep=lambda seconds: None,
        )
    finally:
        parent.chmod(0o700)

    assert outcome.state == "failed_to_start"
    assert outcome.ready is False
    assert outcome.diagnostic is not None
    assert outcome.diagnostic.code == "gateway_start_failed"
    assert outcome.diagnostic.diagnostic_class == "gateway_health"
    assert "VERDICT_HOME" in outcome.diagnostic.remediation


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_require_gateway_ready_raises_a_bootstrap_error_for_an_unusable_lock(
    tmp_path: Path,
) -> None:
    """The fail-closed entry point converts it too, so no caller sees an OSError."""
    parent = tmp_path / "readonly"
    parent.mkdir()
    parent.chmod(0o500)
    try:
        with pytest.raises(BootstrapError) as excinfo:
            gl.require_gateway_ready(
                _result(),
                probe=FakeProbe(CLOSED),
                launcher=CountingLauncher(),
                state_dir=parent / "state",
                sleep=lambda seconds: None,
            )
    finally:
        parent.chmod(0o700)

    assert excinfo.value.reason_code == "gateway_lifecycle_failed"
    assert excinfo.value.diagnostics[0].code == "gateway_start_failed"


def test_state_dir_follows_verdict_home_then_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("VERDICT_HOME", raising=False)
    assert gl.gateway_state_dir() == tmp_path / "home" / ".verdict" / "gateway"
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / "explicit"))
    assert gl.gateway_state_dir() == tmp_path / "explicit" / "gateway"


def test_lock_file_is_owner_only(tmp_path: Path) -> None:
    _ensure(_result(), FakeProbe(CLOSED, CLOSED, HEALTHY), tmp_path, launcher=CountingLauncher())
    lock = gl.gateway_lock_path(GATEWAY_URL, state_dir=tmp_path / "state")
    assert lock.is_file()
    assert lock.stat().st_mode & 0o777 == 0o600


# --- probe authentication ---------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def _recording_httpx_get(status_code: int, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace httpx.get with a recorder. No socket is opened."""
    import httpx

    calls: list[dict[str, Any]] = []

    def fake_get(url: str, **kwargs: Any) -> Any:
        calls.append({"url": url, **kwargs})
        return _FakeResponse(status_code)

    monkeypatch.setattr(httpx, "get", fake_get)
    return calls


def test_the_probe_sends_the_configured_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A gateway that requires a credential must receive one when we have it."""
    calls = _recording_httpx_get(200, monkeypatch)

    health = gl.http_gateway_probe(GATEWAY_URL, api_key="sk-GATEWAYKEY")

    assert health.reachable is True
    assert calls[0]["url"] == f"{GATEWAY_URL}/models"
    assert calls[0]["headers"] == {"Authorization": "Bearer sk-GATEWAYKEY"}


def test_the_probe_sends_no_authorization_header_without_a_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Behaviour without a configured key is unchanged: no header is invented."""
    calls = _recording_httpx_get(200, monkeypatch)

    gl.http_gateway_probe(GATEWAY_URL)

    assert calls[0]["headers"] == {}


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_credential_is_reported_as_auth_failed_not_unhealthy(
    status: int, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """401/403 means the gateway is running and refused us, which is a distinct fault.

    Reporting it as unhealthy would call a perfectly healthy auth-protected gateway
    broken, and would send the operator to the wrong remediation.
    """
    _recording_httpx_get(status, monkeypatch)

    health = gl.http_gateway_probe(GATEWAY_URL, api_key="sk-WRONGKEY")

    assert health.auth_failed is True
    assert health.responded is True
    assert health.reachable is False
    assert "sk-WRONGKEY" not in health.detail, "the probe must never echo the key"

    launcher = CountingLauncher()
    outcome = _ensure(_result(), lambda url: health, tmp_path, launcher=launcher)

    assert outcome.state == "unhealthy"
    assert outcome.diagnostic is not None
    assert outcome.diagnostic.code == "gateway_auth_failed"
    assert outcome.diagnostic.diagnostic_class == "gateway_health"
    assert "OMNIROUTE_API_KEY" in outcome.diagnostic.remediation
    assert launcher.launches == [], "an auth failure must not start a second gateway"


def test_a_plain_error_status_is_still_unhealthy_not_an_auth_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """503 is an unhealthy gateway; the two faults stay distinct in both directions."""
    _recording_httpx_get(503, monkeypatch)

    health = gl.http_gateway_probe(GATEWAY_URL)

    assert health.auth_failed is False
    assert health.responded is True

    outcome = _ensure(_result(), lambda url: health, tmp_path, launcher=CountingLauncher())

    assert outcome.diagnostic is not None
    assert outcome.diagnostic.code == "gateway_unhealthy"


def test_the_authenticated_probe_binds_the_key_for_the_lifecycle_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lifecycle calls probe(url), so the credential is bound by the caller."""
    calls = _recording_httpx_get(200, monkeypatch)
    probe = gl.authenticated_gateway_probe("sk-BOUNDKEY")

    probe(GATEWAY_URL)

    assert calls[0]["headers"] == {"Authorization": "Bearer sk-BOUNDKEY"}
    assert gl.authenticated_gateway_probe(None)(GATEWAY_URL).reachable is True
    assert calls[1]["headers"] == {}


# --- report-only inspection -------------------------------------------------


def test_inspect_gateway_never_launches(tmp_path: Path) -> None:
    calls: list[str] = []

    def probe(url: str) -> gl.GatewayHealth:
        calls.append(url)
        return CLOSED

    outcome = gl.inspect_gateway(_result(), probe=probe)
    assert outcome.state == "unhealthy"
    assert outcome.launched is False
    assert calls == [GATEWAY_URL]
    assert not (tmp_path / "state").exists()


def test_inspect_gateway_reports_a_healthy_gateway_as_already_ready() -> None:
    outcome = gl.inspect_gateway(_result(), probe=FakeProbe(HEALTHY))
    assert outcome.state == "already_ready"
    assert outcome.ready is True


def test_inspect_gateway_reports_not_required_without_probing() -> None:
    probe = FakeProbe(CLOSED)
    outcome = gl.inspect_gateway(_result(required=False), probe=probe)
    assert outcome.state == "not_required"
    assert probe.calls == []


# --- fail-closed requirement ------------------------------------------------


def test_require_gateway_ready_returns_the_outcome_when_ready(tmp_path: Path) -> None:
    outcome = gl.require_gateway_ready(
        _result(),
        probe=FakeProbe(HEALTHY),
        launcher=CountingLauncher(),
        state_dir=tmp_path,
        sleep=lambda seconds: None,
    )
    assert outcome.state == "already_ready"


def test_require_gateway_ready_raises_instead_of_switching_paths(tmp_path: Path) -> None:
    with pytest.raises(BootstrapError) as excinfo:
        gl.require_gateway_ready(
            _result(start_command=None),
            probe=FakeProbe(CLOSED),
            launcher=CountingLauncher(),
            state_dir=tmp_path,
            sleep=lambda seconds: None,
        )
    assert excinfo.value.reason_code == "gateway_lifecycle_failed"
    assert excinfo.value.diagnostic_class == "gateway_health"
    assert excinfo.value.diagnostics[0].code == "gateway_start_command_missing"


def test_require_gateway_ready_accepts_a_path_that_needs_no_gateway(tmp_path: Path) -> None:
    outcome = gl.require_gateway_ready(
        _result(required=False), probe=FakeProbe(CLOSED), state_dir=tmp_path
    )
    assert outcome.state == "not_required"


# --- serialization and redaction --------------------------------------------


def test_outcome_serialization_redacts_url_userinfo(tmp_path: Path) -> None:
    outcome = _ensure(
        _result(gateway_url="http://user:secret@127.0.0.1:29999/v1"), FakeProbe(HEALTHY), tmp_path
    )
    payload = outcome.to_dict()
    assert "secret" not in payload["gateway_url"]
    assert "***:***@" in payload["gateway_url"]
    assert "secret" not in payload["detail"]
    assert "secret" not in outcome.describe()
    assert payload["state"] == "already_ready"
    assert payload["ready"] is True


def test_every_story_state_is_expressible() -> None:
    assert set(gl.GatewayState.__args__) == {  # type: ignore[attr-defined]
        "not_required",
        "already_ready",
        "started",
        "starting",
        "failed_to_start",
        "unhealthy",
    }
    assert frozenset({"not_required", "already_ready", "started"}) == gl.READY_STATES


def test_loopback_detection_covers_ipv6_and_localhost() -> None:
    assert gl.is_loopback_gateway("http://127.0.0.1:20128/v1")
    assert gl.is_loopback_gateway("http://localhost:20128/v1")
    assert gl.is_loopback_gateway("http://[::1]:20128/v1")
    assert not gl.is_loopback_gateway("https://gateway.example.com/v1")
    assert not gl.is_loopback_gateway("http://10.0.0.5:20128/v1")


# --- the lifecycle reads configuration only from the bootstrap contract ------


def test_lifecycle_consumes_bootstrap_fields_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    (home / ".config" / "verdict").mkdir(parents=True)
    config = home / ".config" / "verdict" / "verdict.yaml"
    config.write_text(
        "providers:\n"
        "  omniroute:\n"
        "    base_url: http://127.0.0.1:29999/v1\n"
        "    api_key_env: OMNIROUTE_API_KEY\n"
        "gateway_start_command: [fake-gateway, serve, --port, '29999']\n"
        "gateway_ready_timeout_s: 4\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.delenv("OMNIROUTE_BASE_URL", raising=False)
    monkeypatch.delenv("VERDICT_GATEWAY_START_COMMAND", raising=False)
    monkeypatch.delenv("VERDICT_GATEWAY_READY_TIMEOUT_S", raising=False)
    resolved = resolve_provider_bootstrap()
    launcher = CountingLauncher()
    clock = FakeClock()
    outcome = gl.ensure_gateway_ready(
        resolved,
        probe=FakeProbe(CLOSED, CLOSED, HEALTHY),
        launcher=launcher,
        clock=clock,
        sleep=clock.sleep,
        state_dir=tmp_path / "state",
    )
    assert outcome.state == "started"
    assert launcher.launches[0][0] == ("fake-gateway", "serve", "--port", "29999")
    assert resolved.gateway_ready_timeout_s == 4.0


def test_module_reads_no_configuration_and_never_uses_a_shell() -> None:
    """The lifecycle must consume the bootstrap contract, not configuration.

    Checked on the AST, not on prose: diagnostics legitimately *name*
    ``OMNIROUTE_BASE_URL`` as operator remediation, while the module must never
    *read* it. The only environment names lifecycle code may read are the ones
    that locate the state directory holding the lock.
    """
    import ast

    tree = ast.parse(Path(gl.__file__).read_text(encoding="utf-8"))
    env_names: set[str] = set()
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg == "shell":
                    assert (
                        isinstance(keyword.value, ast.Constant) and keyword.value.value is False
                    ), "the launcher must never run a shell"
            target = node.func
            reads_env = (isinstance(target, ast.Name) and target.id == "getenv") or (
                isinstance(target, ast.Attribute) and target.attr in {"getenv", "get"}
            )
            if reads_env:
                for argument in node.args:
                    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                        env_names.add(argument.value)
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "environ"
            and isinstance(node.slice, ast.Constant)
        ):
            env_names.add(str(node.slice.value))
    assert env_names <= {"VERDICT_HOME", "HOME"}, f"lifecycle code reads {env_names}"
    assert "yaml" not in imported, "the lifecycle module must not parse the routing config"


def test_start_argv_comes_from_the_bootstrap_contract(tmp_path: Path) -> None:
    launcher = CountingLauncher()
    _ensure(
        _result(start_command=("only-from-contract", "--flag")),
        FakeProbe(CLOSED, CLOSED, HEALTHY),
        tmp_path,
        launcher=launcher,
    )
    assert launcher.launches[0][0] == ("only-from-contract", "--flag")
