"""Gateway lifecycle: ensure the configured gateway is ready, exactly once.

This module owns the action side of the gateway decision. Whether a gateway is
required, at which URL, with which start command and readiness budget, is
decided entirely by :mod:`verdict.provider_bootstrap`; nothing here reads
``verdict.yaml`` or the environment for configuration.

Boundaries:

- No configuration reading, no precedence rules, no provider invention.
- No discovery: an absent start command is a named refusal, never a guessed
  binary and never a port scan.
- A process is started only for a loopback gateway, only from an explicit argv
  with ``shell=False``, and only while holding the state-directory lock, so
  concurrent callers can never launch duplicates.
- A pre-existing gateway is reused and never signalled. Only a process this call
  launched is ever terminated, so a bounded wait leaves no orphan.
- A failed ensure is returned as a ``gateway_health`` diagnostic (or raised by
  :func:`require_gateway_ready`). Execution paths never switch silently.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import os
import signal
import subprocess  # nosec B404: launches an operator-supplied argv with shell=False.
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from verdict.provider_bootstrap import (
    BootstrapDiagnostic,
    BootstrapError,
    BootstrapResult,
    redact_start_command,
    redact_url,
)

__all__ = [
    "GatewayHealth",
    "GatewayLauncher",
    "GatewayLifecycleOutcome",
    "GatewayProcess",
    "GatewayState",
    "LaunchedGateway",
    "SubprocessGatewayLauncher",
    "ensure_gateway_ready",
    "gateway_lock_key",
    "gateway_lock_path",
    "gateway_state_dir",
    "http_gateway_probe",
    "inspect_gateway",
    "require_gateway_ready",
]

#: Lifecycle states. ``starting`` is the only transient one: it is reported to an
#: observer while a launch or a wait is in flight and is never a return value.
GatewayState = Literal[
    "not_required", "already_ready", "started", "starting", "failed_to_start", "unhealthy"
]

#: States in which a gateway-requiring execution path may proceed.
READY_STATES: frozenset[str] = frozenset({"not_required", "already_ready", "started"})

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "ip6-localhost"})
_INITIAL_BACKOFF_S = 0.1
_MAX_BACKOFF_S = 1.0
#: Seconds to wait for a signalled child at each escalation step. Bounded so a
#: wedged gateway cannot make the caller hang while being stopped.
_STOP_GRACE_S = 5.0


@dataclass(frozen=True)
class GatewayHealth:
    """Health answer from a caller-supplied probe.

    ``reachable`` means the gateway answered and is usable. ``responded``
    distinguishes "something is listening but unhealthy" from "nothing is
    listening": an unhealthy responder is never replaced with a second process,
    because that would bind a port another gateway already owns.

    A probe may also return the plainer ``provider_bootstrap.GatewayProbeResult``
    or raise. A raised exception, and a bare result without ``responded``, are
    both read as "did not answer".
    """

    reachable: bool
    responded: bool = False
    detail: str = ""


@dataclass(frozen=True)
class GatewayLifecycleOutcome:
    """Terminal (or observed transient) lifecycle state with its provenance."""

    state: GatewayState
    gateway_url: str | None
    launched: bool = False
    attempts: int = 0
    waited_for_owner: bool = False
    detail: str = ""
    diagnostic: BootstrapDiagnostic | None = None

    @property
    def ready(self) -> bool:
        """True when a gateway-requiring path may proceed."""
        return self.state in READY_STATES

    def to_dict(self) -> dict[str, Any]:
        """Serialize for ``--json`` output, receipts and structured logs."""
        return {
            "state": self.state,
            "ready": self.ready,
            "gateway_url": None if self.gateway_url is None else redact_url(self.gateway_url),
            "launched": self.launched,
            "attempts": self.attempts,
            "waited_for_owner": self.waited_for_owner,
            "detail": redact_url(self.detail),
            "diagnostic": None if self.diagnostic is None else self.diagnostic.to_dict(),
        }

    def describe(self) -> str:
        """Render one deterministic operator line."""
        shown = "none" if self.gateway_url is None else redact_url(self.gateway_url)
        line = f"gateway {self.state} url={shown}"
        if self.detail:
            line = f"{line}: {redact_url(self.detail)}"
        return line


class GatewayProcess(Protocol):
    """Minimal process handle the lifecycle needs. ``subprocess.Popen`` satisfies it."""

    @property
    def pid(self) -> int:  # pragma: no cover - protocol declaration
        ...

    def poll(self) -> int | None:  # pragma: no cover - protocol declaration
        ...

    def terminate(self) -> None:  # pragma: no cover - protocol declaration
        ...

    def kill(self) -> None:  # pragma: no cover - protocol declaration
        ...

    def wait(self, timeout: float | None = None) -> int:  # pragma: no cover - protocol
        ...


@dataclass(frozen=True)
class LaunchedGateway:
    """A process this call started, plus the process group it owns.

    ``pgid`` is recorded at launch and is the only group the lifecycle will ever
    signal. It is ``None`` when the launcher did not create a new session, in
    which case only the leader is signalled: signalling a group we did not create
    could reach the operator's own shell job.
    """

    process: GatewayProcess
    pgid: int | None = None


class GatewayLauncher(Protocol):
    """Starts a gateway from an explicit argv. Tests supply a counting fake.

    A launcher may return a bare process handle or a :class:`LaunchedGateway`
    carrying the process group it created. A bare handle means "no group is
    ours", so only the leader is signalled.
    """

    def launch(
        self, argv: tuple[str, ...], *, cwd: Path
    ) -> GatewayProcess | LaunchedGateway:  # pragma: no cover - protocol declaration
        ...


class SubprocessGatewayLauncher:
    """Launch the configured argv detached, with ``shell=False``.

    The argv comes from the bootstrap contract and is passed as a list, so no
    part of it is interpreted by a shell. Standard streams are detached from this
    process; the gateway keeps its own logging.

    ``start_new_session=True`` puts the child in a new session, so it is the
    leader of a brand-new process group that contains it and every descendant it
    spawns. That group id is recorded here, at launch, and is the only group the
    lifecycle will ever signal.

    The child inherits this process's environment as it stands. Verdict adds
    nothing to it, and in particular never copies credential-store values into
    it: whatever the operator exported is what the gateway sees. This is
    documented in ``docs/CONFIGURATION.md``.
    """

    def launch(self, argv: tuple[str, ...], *, cwd: Path) -> LaunchedGateway:
        """Start ``argv`` in ``cwd`` and return the handle plus its process group."""
        process = subprocess.Popen(  # nosec B603: explicit argv, shell=False.
            list(argv),
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            start_new_session=True,
            env=dict(os.environ),
        )
        return LaunchedGateway(process=process, pgid=_own_process_group(process))


def _own_process_group(process: GatewayProcess) -> int | None:
    """Process group id of a child we just made a group leader, else ``None``.

    The group is claimed only when the kernel agrees the child leads it
    (``getpgid(pid) == pid``). Anything else means we are not the group's owner,
    and an unowned group is never signalled.
    """
    try:
        pgid = os.getpgid(process.pid)
    except (OSError, AttributeError):
        return None
    return pgid if pgid == process.pid else None


def gateway_state_dir(*, home: Path | None = None, env: dict[str, str] | None = None) -> Path:
    """Verdict state directory holding the gateway lock.

    ``VERDICT_HOME`` wins, then ``$HOME/.verdict``. Tests point ``HOME`` at a
    temporary directory, so no test touches the operator's real state.
    """
    source = os.environ if env is None else env
    configured = (source.get("VERDICT_HOME") or "").strip()
    base = Path(configured) if configured else (home or Path(source.get("HOME") or Path.home()))
    if not configured:
        base = base / ".verdict"
    return base.expanduser() / "gateway"


def gateway_lock_key(gateway_url: str) -> str:
    """Canonical identity of a gateway for locking purposes.

    Two configurations that name the same listening socket must share one lock,
    or both processes would believe they are the launcher. ``localhost``,
    ``127.0.0.1`` and ``[::1]`` on the same port are the same gateway, so the
    loopback aliases collapse to one name. The scheme, a trailing slash and a
    trailing ``/v1`` are also not part of the identity: they address the same
    process.

    Anything the parser cannot make sense of falls back to the trimmed URL, so an
    unparsable value still gets a stable lock of its own rather than sharing one.
    """
    trimmed = gateway_url.strip().rstrip("/")
    parts = urlsplit(trimmed)
    host = (parts.hostname or "").strip().lower()
    if not host:
        return trimmed
    if host in _LOOPBACK_HOSTS:
        host = "loopback"
    port = parts.port
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    path = parts.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[: -len("/v1")]
    return f"{host}:{port}{path}"


def gateway_lock_path(gateway_url: str, *, state_dir: Path | None = None) -> Path:
    """Per-gateway lock file. One listening socket, one lock, one launcher."""
    digest = hashlib.sha256(gateway_lock_key(gateway_url).encode("utf-8")).hexdigest()[:16]
    return (state_dir or gateway_state_dir()) / f"gateway-{digest}.lock"


def http_gateway_probe(gateway_url: str, *, timeout: float = 2.0) -> GatewayHealth:
    """Read-only inventory probe: ``GET {gateway_url}/models``.

    The default production probe. It only reads; it never starts, stops or
    reconfigures anything. A non-2xx answer is a responding-but-unhealthy
    gateway, which the lifecycle refuses to duplicate.
    """
    import httpx

    url = f"{gateway_url.rstrip('/')}/models"
    try:
        response = httpx.get(url, timeout=timeout)
    except Exception as exc:
        return GatewayHealth(
            reachable=False, responded=False, detail=f"{type(exc).__name__}: {exc}"
        )
    if 200 <= response.status_code < 300:
        return GatewayHealth(reachable=True, responded=True, detail="inventory reachable")
    return GatewayHealth(
        reachable=False, responded=True, detail=f"HTTP {response.status_code} from {url}"
    )


def _health(outcome: Any) -> GatewayHealth:
    """Normalize any probe return value into a ``GatewayHealth``."""
    reachable = bool(getattr(outcome, "reachable", False))
    responded = bool(getattr(outcome, "responded", reachable))
    return GatewayHealth(
        reachable=reachable, responded=responded, detail=str(getattr(outcome, "detail", "") or "")
    )


def _probe_once(probe: Callable[[str], Any], gateway_url: str) -> GatewayHealth:
    try:
        return _health(probe(gateway_url))
    except Exception as exc:
        return GatewayHealth(
            reachable=False, responded=False, detail=f"{type(exc).__name__}: {redact_url(str(exc))}"
        )


def is_loopback_gateway(gateway_url: str) -> bool:
    """True when the URL names this host, the only place we may start a process."""
    host = (urlsplit(gateway_url).hostname or "").strip().lower()
    return host in _LOOPBACK_HOSTS


def _scrub_argv(text: str, argv: Sequence[str]) -> str:
    """Remove every start-command argument from a message before it is reported.

    An exception raised by the launcher can quote the argv it was given, and a
    start command routinely carries a token. The program name is kept, because an
    operator needs to know which binary failed; every argument after it is
    replaced. Longest first, so a short argument that is a substring of a longer
    one cannot leave a fragment behind.
    """
    scrubbed = str(text)
    for argument in sorted((str(item) for item in argv[1:]), key=len, reverse=True):
        if argument:
            scrubbed = scrubbed.replace(argument, "<redacted>")
    return scrubbed


def _diagnostic(
    code: str, *, detail: str, remediation: str, source: Any = None
) -> BootstrapDiagnostic:
    return BootstrapDiagnostic(
        code=code,
        diagnostic_class="gateway_health",
        field="gateway_url",
        source=source,
        detail=redact_url(detail),
        remediation=remediation,
    )


class LockUnavailableError(Exception):
    """The launch lock could not be opened safely. Carries operator remediation.

    Raised instead of surfacing a raw ``OSError``, so the caller can turn it into
    a ``gateway_health`` diagnostic rather than a traceback.
    """

    def __init__(self, detail: str, remediation: str) -> None:
        super().__init__(detail)
        self.detail = detail
        self.remediation = remediation


def _open_lock_file(lock_path: Path) -> int:
    """Open the lock file without ever following a symlink.

    ``O_NOFOLLOW`` applies to the final component, so a symlinked lock path is
    refused instead of being opened and chmodded through. Permissions are set with
    ``fchmod`` on the descriptor we already hold, not by path, so there is no
    window in which the name could be swapped for something else.
    """
    try:
        descriptor = os.open(
            lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600
        )
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.EMLINK}:
            raise LockUnavailableError(
                detail=(
                    f"the gateway launch lock {lock_path} is a symbolic link, so Verdict "
                    f"refuses to open it: following it could create or re-permission a file "
                    f"somewhere else"
                ),
                remediation=(
                    f"remove {lock_path} (it is only a lock, nothing is stored in it), or "
                    f"point VERDICT_HOME at a directory you control"
                ),
            ) from exc
        raise LockUnavailableError(
            detail=f"the gateway launch lock {lock_path} could not be opened: {exc.strerror}",
            remediation=(
                f"make {lock_path.parent} writable by this user, or set VERDICT_HOME to a "
                f"directory you control"
            ),
        ) from exc
    # A filesystem that refuses fchmod (some network mounts) is not a reason to
    # refuse to launch; the lock holds no data and O_CREAT already used 0600.
    with contextlib.suppress(OSError):
        os.fchmod(descriptor, 0o600)
    return descriptor


def _prepare_state_dir(state_dir: Path) -> None:
    """Create the state directory 0700 without following a symlinked final component."""
    try:
        state_dir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise LockUnavailableError(
            detail=f"the Verdict state directory {state_dir.parent} could not be created: {exc.strerror}",
            remediation=(
                f"make {state_dir.parent} writable by this user, or set VERDICT_HOME to a "
                f"directory you control"
            ),
        ) from exc
    if state_dir.is_symlink():
        raise LockUnavailableError(
            detail=(
                f"the gateway state directory {state_dir} is a symbolic link, so Verdict "
                f"refuses to create the launch lock inside it"
            ),
            remediation=(
                f"replace {state_dir} with a real directory, or set VERDICT_HOME to a "
                f"directory you control"
            ),
        )
    try:
        state_dir.mkdir(exist_ok=True, mode=0o700)
    except OSError as exc:
        raise LockUnavailableError(
            detail=f"the gateway state directory {state_dir} could not be created: {exc.strerror}",
            remediation=(
                f"make {state_dir.parent} writable by this user, or set VERDICT_HOME to a "
                f"directory you control"
            ),
        ) from exc


@contextlib.contextmanager
def _launch_lock(lock_path: Path) -> Iterator[bool]:
    """Hold the exclusive launch lock, or yield False without blocking.

    The holder is the only caller allowed to launch. A contender yields ``False``
    and waits for readiness instead, so two concurrent processes produce exactly
    one gateway.

    Raises :class:`LockUnavailableError` when the lock cannot be opened safely (a
    symlinked path, an unwritable state directory), so the caller reports a
    diagnostic instead of raising ``PermissionError`` at the operator.
    """
    _prepare_state_dir(lock_path.parent)
    descriptor = _open_lock_file(lock_path)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def inspect_gateway(
    result: BootstrapResult,
    *,
    probe: Callable[[str], Any],
    clock: Callable[[], float] | None = None,
) -> GatewayLifecycleOutcome:
    """Classify gateway readiness without starting anything.

    ``verdict doctor`` and any other report-only surface uses this: it probes at
    most once and never launches, signals or writes a lock.
    """
    del clock  # Accepted for symmetry with ensure_gateway_ready; a single probe is untimed.
    return _ensure(
        result,
        probe=probe,
        launcher=None,
        clock=time.monotonic,
        sleep=lambda _seconds: None,
        state_dir=None,
        observer=None,
        allow_start=False,
    )


def ensure_gateway_ready(
    result: BootstrapResult,
    *,
    probe: Callable[[str], Any],
    launcher: GatewayLauncher | None = None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
    state_dir: Path | None = None,
    observer: Callable[[GatewayLifecycleOutcome], None] | None = None,
    allow_start: bool = True,
) -> GatewayLifecycleOutcome:
    """Make the configured gateway ready, reusing a healthy one, never duplicating.

    Returns one of ``not_required``, ``already_ready``, ``started``,
    ``failed_to_start`` or ``unhealthy``. ``observer`` receives ``starting``
    snapshots while a launch or a wait is in flight. Everything external is
    injectable: ``probe``, ``launcher``, ``clock``, ``sleep`` and ``state_dir``.
    """
    return _ensure(
        result,
        probe=probe,
        launcher=launcher,
        clock=clock or time.monotonic,
        sleep=sleep or time.sleep,
        state_dir=state_dir,
        observer=observer,
        allow_start=allow_start,
    )


def require_gateway_ready(
    result: BootstrapResult,
    *,
    probe: Callable[[str], Any],
    launcher: GatewayLauncher | None = None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
    state_dir: Path | None = None,
    observer: Callable[[GatewayLifecycleOutcome], None] | None = None,
) -> GatewayLifecycleOutcome:
    """Ensure readiness on a path that needs the gateway, or fail closed.

    Raises ``BootstrapError("gateway_lifecycle_failed", ...)`` carrying the
    ``gateway_health`` diagnostic. The caller must not fall back to another
    execution path: a silent switch would execute work somewhere the operator
    did not choose.
    """
    outcome = ensure_gateway_ready(
        result,
        probe=probe,
        launcher=launcher,
        clock=clock,
        sleep=sleep,
        state_dir=state_dir,
        observer=observer,
    )
    if outcome.ready:
        return outcome
    diagnostic = outcome.diagnostic or _diagnostic(
        "gateway_unreachable",
        detail=outcome.detail or f"gateway is {outcome.state}",
        remediation="start the gateway, or run 'verdict doctor'",
    )
    raise BootstrapError("gateway_lifecycle_failed", (diagnostic,))


def _ensure(
    result: BootstrapResult,
    *,
    probe: Callable[[str], Any],
    launcher: GatewayLauncher | None,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    state_dir: Path | None,
    observer: Callable[[GatewayLifecycleOutcome], None] | None,
    allow_start: bool,
) -> GatewayLifecycleOutcome:
    if not result.gateway_required:
        # No probe at all: a path that does not need the gateway must not pay for
        # one, and must not report a gateway fault it does not depend on.
        return GatewayLifecycleOutcome(
            state="not_required",
            gateway_url=None,
            detail="no gateway provider in the resolved configuration",
        )
    url = result.gateway_url
    if not url:
        return GatewayLifecycleOutcome(
            state="failed_to_start",
            gateway_url=None,
            diagnostic=_diagnostic(
                "gateway_required_but_absent",
                detail="a gateway provider is configured but no gateway URL resolved",
                remediation=("set OMNIROUTE_BASE_URL, or add 'gateway_url' to the routing config"),
            ),
            detail="a gateway provider is configured but no gateway URL resolved",
        )

    def emit(state: GatewayState, **kwargs: Any) -> GatewayLifecycleOutcome:
        outcome = GatewayLifecycleOutcome(state=state, gateway_url=url, **kwargs)
        if observer is not None:
            observer(outcome)
        return outcome

    source = result.source_of("gateway_url")
    shown = redact_url(url)
    health = _probe_once(probe, url)
    attempts = 1
    if health.reachable:
        return emit(
            "already_ready",
            attempts=attempts,
            detail=health.detail or f"gateway at {shown} answered",
        )
    if health.responded:
        # Something owns this URL and reports itself unhealthy. Starting a second
        # process would fight it for the port; report instead.
        return emit(
            "unhealthy",
            attempts=attempts,
            detail=health.detail or f"gateway at {shown} responded but is not healthy",
            diagnostic=_diagnostic(
                "gateway_unhealthy",
                detail=f"gateway at {shown} responded but is not healthy: {health.detail}",
                remediation=(
                    f"inspect the process serving {shown}; restart it yourself once it is "
                    f"safe to do so"
                ),
                source=source,
            ),
        )
    if not allow_start:
        return emit(
            "unhealthy",
            attempts=attempts,
            detail=health.detail or f"gateway at {shown} did not answer",
            diagnostic=_diagnostic(
                "gateway_unreachable",
                detail=f"gateway at {shown} did not answer: {health.detail}",
                remediation=f"start the gateway at {shown}, or run 'verdict doctor'",
                source=source,
            ),
        )
    if not is_loopback_gateway(url):
        # A remote gateway is someone else's process; we can report on it but we
        # must never try to run it.
        return emit(
            "failed_to_start",
            attempts=attempts,
            detail=f"gateway at {shown} is not local, so it is not ours to start",
            diagnostic=_diagnostic(
                "gateway_remote_not_managed",
                detail=(
                    f"gateway at {shown} is not a loopback address; Verdict starts only a "
                    f"local gateway"
                ),
                remediation=(
                    "start the remote gateway out of band, or point the configuration at a "
                    "loopback gateway"
                ),
                source=source,
            ),
        )
    argv = result.gateway_start_command
    if not argv:
        return emit(
            "failed_to_start",
            attempts=attempts,
            detail=f"gateway at {shown} is absent and no start command is configured",
            diagnostic=BootstrapDiagnostic(
                code="gateway_start_command_missing",
                diagnostic_class="gateway_health",
                field="gateway_start_command",
                source=None,
                detail=(
                    f"gateway at {shown} is required and not running, and no "
                    f"gateway_start_command is configured, so Verdict has nothing to run "
                    f"(commands are never guessed or discovered)"
                ),
                remediation=(
                    f"set 'gateway_start_command' in {result.config_path} (a YAML list of "
                    f"arguments), or export VERDICT_GATEWAY_START_COMMAND, or start the "
                    f"gateway at {shown} yourself"
                ),
            ),
        )

    deadline = clock() + max(result.gateway_ready_timeout_s, 0.0)
    lock = gateway_lock_path(url, state_dir=state_dir)
    try:
        with _launch_lock(lock) as owner:
            if not owner:
                # Another process holds the launch lock. Wait for its gateway instead
                # of starting a duplicate.
                emit(
                    "starting",
                    attempts=attempts,
                    waited_for_owner=True,
                    detail="another Verdict process is starting the gateway",
                )
                return _wait_for_owner(
                    url=url,
                    probe=probe,
                    clock=clock,
                    sleep=sleep,
                    deadline=deadline,
                    attempts=attempts,
                    emit=emit,
                    source=source,
                )
            # Re-probe under the lock: the previous owner may have finished between
            # our first probe and acquiring the lock.
            health = _probe_once(probe, url)
            attempts += 1
            if health.reachable:
                return emit(
                    "already_ready",
                    attempts=attempts,
                    detail=health.detail or f"gateway at {shown} answered before launch",
                )
            emit("starting", attempts=attempts, detail=f"starting the gateway for {shown}")
            try:
                launched = _launched(
                    (launcher or SubprocessGatewayLauncher()).launch(tuple(argv), cwd=lock.parent)
                )
            except Exception as exc:
                # The exception text can quote the argv it was handed, and the argv can
                # carry a token, so every argument is scrubbed out of the report.
                return emit(
                    "failed_to_start",
                    attempts=attempts,
                    detail=f"launching the gateway failed: {type(exc).__name__}",
                    diagnostic=_diagnostic(
                        "gateway_start_failed",
                        detail=(
                            f"the configured gateway_start_command "
                            f"({redact_start_command(tuple(argv))}) could not be launched: "
                            f"{type(exc).__name__}: {_scrub_argv(str(exc), tuple(argv))}"
                        ),
                        remediation=(
                            f"check that the first argument of gateway_start_command is an "
                            f"executable on PATH in {result.config_path}"
                        ),
                        source=result.source_of("gateway_start_command"),
                    ),
                )
            return _wait_for_launch(
                url=url,
                probe=probe,
                clock=clock,
                sleep=sleep,
                deadline=deadline,
                attempts=attempts,
                emit=emit,
                child=launched,
                source=source,
                timeout_s=result.gateway_ready_timeout_s,
            )
    except LockUnavailableError as exc:
        # An unsafe or unusable lock path is an operator-fixable gateway fault, not
        # a traceback: the CLI catches BootstrapError, never OSError.
        return emit(
            "failed_to_start",
            attempts=attempts,
            detail=f"the gateway launch lock could not be used: {exc.detail}",
            diagnostic=_diagnostic(
                "gateway_start_failed",
                detail=exc.detail,
                remediation=exc.remediation,
                source=source,
            ),
        )


def _wait_for_owner(
    *,
    url: str,
    probe: Callable[[str], Any],
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    deadline: float,
    attempts: int,
    emit: Callable[..., GatewayLifecycleOutcome],
    source: Any,
) -> GatewayLifecycleOutcome:
    """Wait for the lock owner's gateway. Never launches, never signals."""
    shown = redact_url(url)
    backoff = _INITIAL_BACKOFF_S
    last = ""
    while clock() < deadline:
        sleep(backoff)
        backoff = min(backoff * 2, _MAX_BACKOFF_S)
        health = _probe_once(probe, url)
        attempts += 1
        last = health.detail
        if health.reachable:
            return emit(
                "started",
                attempts=attempts,
                waited_for_owner=True,
                detail=f"another Verdict process started the gateway at {shown}",
            )
        emit(
            "starting",
            attempts=attempts,
            waited_for_owner=True,
            detail=f"waiting for the gateway at {shown}",
        )
    return emit(
        "failed_to_start",
        attempts=attempts,
        waited_for_owner=True,
        detail=f"another Verdict process is starting the gateway at {shown} but it never became ready",
        diagnostic=_diagnostic(
            "gateway_ready_timeout",
            detail=(
                f"another Verdict process holds the gateway launch lock for {shown} but the "
                f"gateway did not become ready within the readiness budget: {last}"
            ),
            remediation=(
                f"raise gateway_ready_timeout_s, or inspect the process that is starting {shown}"
            ),
            source=source,
        ),
    )


def _wait_for_launch(
    *,
    url: str,
    probe: Callable[[str], Any],
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    deadline: float,
    attempts: int,
    emit: Callable[..., GatewayLifecycleOutcome],
    child: LaunchedGateway,
    source: Any,
    timeout_s: float,
) -> GatewayLifecycleOutcome:
    """Wait for the gateway we launched, bounded, leaving no orphan.

    Every exit from this function either observes the child already gone and reaps
    it, or stops its whole process group and reaps it. No path returns while a
    process we started is unreaped.
    """
    shown = redact_url(url)
    process = child.process
    backoff = _INITIAL_BACKOFF_S
    answered = False
    last = ""
    while clock() < deadline:
        exit_code = process.poll()
        if exit_code is not None:
            # The leader is gone, but its group may not be: a wrapper that dies
            # after spawning the gateway leaves descendants behind.
            stopped = _stop_launched(child)
            return emit(
                "failed_to_start",
                attempts=attempts,
                launched=True,
                detail=f"the gateway process exited with code {exit_code}; {stopped}",
                diagnostic=_diagnostic(
                    "gateway_process_exited",
                    detail=(
                        f"the gateway process started for {shown} exited with code "
                        f"{exit_code} before becoming ready. {stopped}"
                    ),
                    remediation=(
                        "run the configured gateway_start_command in a terminal to see why it exits"
                    ),
                    source=source,
                ),
            )
        health = _probe_once(probe, url)
        attempts += 1
        last = health.detail
        answered = answered or health.responded
        if health.reachable:
            return emit(
                "started",
                attempts=attempts,
                launched=True,
                detail=f"started the gateway at {shown}",
            )
        emit(
            "starting",
            attempts=attempts,
            launched=True,
            detail=f"waiting for the gateway we started at {shown}",
        )
        sleep(backoff)
        backoff = min(backoff * 2, _MAX_BACKOFF_S)
    stopped = _stop_launched(child)
    if answered:
        return emit(
            "unhealthy",
            attempts=attempts,
            launched=True,
            detail=f"the gateway we started at {shown} never reported healthy; {stopped}",
            diagnostic=_diagnostic(
                "gateway_unhealthy",
                detail=(
                    f"the gateway process started for {shown} responded but never reported "
                    f"healthy within {timeout_s}s: {last}. {stopped}"
                ),
                remediation=(
                    "check the gateway's own logs; raise gateway_ready_timeout_s if it "
                    "needs longer to warm up"
                ),
                source=source,
            ),
        )
    return emit(
        "failed_to_start",
        attempts=attempts,
        launched=True,
        detail=f"the gateway we started at {shown} never answered; {stopped}",
        diagnostic=_diagnostic(
            "gateway_ready_timeout",
            detail=(
                f"the gateway process started for {shown} did not answer within "
                f"{timeout_s}s: {last}. {stopped}"
            ),
            remediation=(
                f"raise gateway_ready_timeout_s, or run the configured "
                f"gateway_start_command in a terminal to see why {shown} stays closed"
            ),
            source=source,
        ),
    )


def _launched(outcome: Any) -> LaunchedGateway:
    """Normalize a launcher return value into a :class:`LaunchedGateway`."""
    if isinstance(outcome, LaunchedGateway):
        return outcome
    return LaunchedGateway(process=outcome, pgid=None)


def _reap(process: GatewayProcess, timeout: float) -> int | None:
    """Wait up to ``timeout`` for the child, returning its status or ``None``.

    ``None`` means it is still running. A handle without ``wait`` (a test fake)
    falls back to ``poll``.
    """
    waiter = getattr(process, "wait", None)
    if waiter is None:
        return process.poll()
    try:
        return int(waiter(timeout=timeout))
    except Exception:
        return process.poll()


def _signal_group(pgid: int | None, process: GatewayProcess, signal_number: int) -> None:
    """Signal the group we created, else only the leader we started.

    ``pgid`` is set only when :func:`_own_process_group` confirmed this process
    created that group, so no group belonging to anyone else is ever signalled.
    """
    if pgid is not None:
        os.killpg(pgid, signal_number)
        return
    if signal_number == signal.SIGKILL:
        process.kill()
    else:
        process.terminate()


def _stop_launched(launched: LaunchedGateway, *, grace_s: float = _STOP_GRACE_S) -> str:
    """Stop and reap the process this call launched, plus the group it leads.

    Escalates SIGTERM to SIGKILL on the group, then waits, so no zombie is left
    behind and no descendant of the gateway survives. The returned sentence states
    what actually happened — reaped, killed, or still running with its pid — and
    is embedded in the timeout diagnostic verbatim.

    Only a group recorded at launch is signalled. A pre-existing gateway has no
    such record, so it is never reachable from here.
    """
    process = launched.process
    pgid = launched.pgid
    if process.poll() is not None:
        _reap(process, grace_s)
        return "the process we started had already exited and was reaped"
    try:
        _signal_group(pgid, process, signal.SIGTERM)
    except Exception as exc:
        return (
            f"terminating the process we started (pid {process.pid}) failed: "
            f"{type(exc).__name__}: {exc}; it may still be running"
        )
    if _reap(process, grace_s) is not None:
        return (
            f"the process we started (pid {process.pid}) stopped on SIGTERM and was reaped, "
            f"so no orphan remains"
        )
    try:
        _signal_group(pgid, process, signal.SIGKILL)
    except Exception as exc:
        return (
            f"the process we started (pid {process.pid}) ignored SIGTERM and could not be "
            f"killed: {type(exc).__name__}: {exc}; it is still running"
        )
    if _reap(process, grace_s) is not None:
        return (
            f"the process we started (pid {process.pid}) was killed and reaped, so no orphan "
            f"remains"
        )
    return (
        f"the process we started (pid {process.pid}) did not exit after SIGKILL and is still "
        f"running; stop it yourself"
    )
