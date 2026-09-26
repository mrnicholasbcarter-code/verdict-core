"""Gateway lifecycle coverage: ensure-ready without duplicates.

Every test is offline and process-free. Probes, launchers, clocks and sleeps are
fakes; the state directory is a tmp_path. Nothing opens a socket, nothing starts
a real gateway, and nothing touches the operator's ~/.verdict or the shared
gateway on 127.0.0.1:20128.
"""

from __future__ import annotations

import threading
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
    """Monotonic clock advanced only by the injected sleep."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


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


def test_lock_path_is_per_url_and_under_the_state_dir(tmp_path: Path) -> None:
    first = gl.gateway_lock_path("http://127.0.0.1:20128/v1", state_dir=tmp_path)
    second = gl.gateway_lock_path("http://127.0.0.1:29999/v1", state_dir=tmp_path)
    assert first != second
    assert first.parent == tmp_path
    # A trailing slash is the same gateway, so it must not get a second lock.
    assert gl.gateway_lock_path("http://127.0.0.1:20128/v1/", state_dir=tmp_path) == first


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
