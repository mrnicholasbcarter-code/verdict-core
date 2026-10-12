"""Fixed controller operation leases and Linux supervised worker ownership.

Leases describe bounded waits, not progress or health. A supervised worker
registers itself before exec under the same lock used to revoke a generation.
No model/provider API is used here.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import fcntl
import json
import math
import os
import re
import secrets
import signal
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from verdict.orchestration.contracts import WorkerExecutor, WorkerTerminal

STATE_FILE = "controller-operations.json"
RUN_DIR_ENV = "VERDICT_SUPERVISED_RUN_DIR"
CGROUP_ROOT_ENV = "VERDICT_CONTROLLER_CGROUP_ROOT"
TOKEN_ENV = "VERDICT_CONTROLLER_TOKEN"
DEADLINE_ENV = "VERDICT_CONTROLLER_DEADLINE"
LAUNCH_ENV = "VERDICT_CONTROLLER_LAUNCH"
OPERATION_ENV = "VERDICT_CONTROLLER_OPERATION"
_operation_id: contextvars.ContextVar[str] = contextvars.ContextVar("operation_id", default="")


class OwnershipError(RuntimeError):
    """Ownership cannot be established or cleanup cannot be verified."""


def process_identity(pid: int) -> dict[str, Any] | None:
    """Read Linux boot/start/session identity; a vanished process is absent."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
        fields = text[text.rindex(")") + 2 :].split()
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return {
            "pid": pid,
            "boot": boot,
            "start": int(fields[19]),
            "pgid": int(fields[2]),
            "sid": int(fields[3]),
            "state": fields[0],
        }
    except FileNotFoundError:
        return None
    except (OSError, ValueError, IndexError) as exc:
        raise OwnershipError("cannot read Linux process identity") from exc


def same_process(actual: Mapping[str, Any] | None, expected: Mapping[str, Any]) -> bool:
    """Compare durable identity, never just a numeric PID."""
    return actual is not None and all(
        actual.get(k) == expected.get(k) for k in ("pid", "boot", "start", "pgid", "sid")
    )


def read_state(run_dir: Path) -> dict[str, Any]:
    """Read one atomic state snapshot. Malformed state is not ownership evidence."""
    try:
        state = json.loads((run_dir / STATE_FILE).read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise OwnershipError("unreadable controller operation state") from exc
    if not isinstance(state, dict):
        raise OwnershipError("invalid controller operation state")
    return state


class OperationLeaseGuard:
    """Validate fixed, owner-bound operation deadlines independently of progress."""

    PHASE_CAPS: ClassVar[dict[str, float]] = {"planner": 600.0, "plan_repair": 600.0, "worker": 900.0}

    def __init__(self) -> None:
        self._seen: dict[str, str] = {}
        self._ended: set[str] = set()
        self._invalid: set[str] = set()
        self._logical: dict[str, str] = {}

    def inspect(
        self,
        state: dict[str, Any],
        *,
        token: str,
        owner: Mapping[str, Any] | None,
        now: float,
        overall_deadline: float,
    ) -> tuple[str, dict[str, Any] | None]:
        """Return NONE, WAITING_BOUNDED, or EXPIRED. Invalid leases never defer stall."""
        if state.get("active") is not True or state.get("token") != token or owner is None:
            return "NONE", None
        operations = state.get("operations")
        if not isinstance(operations, dict):
            return "NONE", None
        waiting: list[dict[str, Any]] = []
        for op_id, operation in operations.items():
            if not isinstance(operation, dict) or not isinstance(op_id, str):
                continue
            frozen = json.dumps(
                {k: v for k, v in operation.items() if k != "status"}, sort_keys=True
            )
            previous = self._seen.setdefault(op_id, frozen)
            raw_owner = operation.get("owner")
            owner_key = (
                [raw_owner.get(k) for k in ("pid", "boot", "start", "pgid", "sid")]
                if isinstance(raw_owner, dict)
                else []
            )
            logical = json.dumps(
                [
                    operation.get("token"),
                    owner_key,
                    operation.get("phase"),
                    operation.get("attempt_id"),
                ],
                sort_keys=True,
            )
            original_id = self._logical.setdefault(logical, op_id)
            if operation.get("status") == "ended":
                self._ended.add(op_id)
                continue
            phase = str(operation.get("phase"))
            attempt_id = str(operation.get("attempt_id", ""))
            attempt_match = re.fullmatch(r"plan-([1-6])(-repair)?", attempt_id)
            worker_match = re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}-a[1-9][0-9]*", attempt_id)
            started, deadline = operation.get("started"), operation.get("deadline")
            cap = self.PHASE_CAPS.get(phase)
            valid_attempt = (
                attempt_match is not None
                and phase in {"planner", "plan_repair"}
                and (bool(attempt_match.group(2)) == (phase == "plan_repair"))
            ) or (worker_match is not None and phase == "worker")
            valid = (
                op_id not in self._ended
                and op_id not in self._invalid
                and original_id == op_id
                and valid_attempt
                and frozen == previous
                and operation.get("id") == op_id
                and operation.get("status") == "active"
                and operation.get("token") == token
                and isinstance(operation.get("attempt_id"), str)
                and bool(operation["attempt_id"])
                and isinstance(operation.get("owner"), dict)
                and same_process(operation["owner"], owner)
                and cap is not None
                and isinstance(started, (int, float))
                and not isinstance(started, bool)
                and isinstance(deadline, (int, float))
                and not isinstance(deadline, bool)
                and math.isfinite(started)
                and math.isfinite(deadline)
                and 0 <= started <= now
                and started < deadline <= overall_deadline
                and deadline - started <= float(cap or 0)
            )
            if not valid:
                self._invalid.add(op_id)
                continue
            assert isinstance(deadline, (int, float))
            if now >= deadline:
                return "EXPIRED", operation
            waiting.append(operation)
        # Earliest active deadline wins: another operation cannot hide an expired one.
        if waiting:
            return "WAITING_BOUNDED", min(waiting, key=lambda op: op["deadline"])
        return "NONE", None


def _write_state(run_dir: Path, state: dict[str, Any]) -> None:
    fd, name = tempfile.mkstemp(prefix=".controller-operations-", dir=run_dir)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(state, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, run_dir / STATE_FILE)
        directory = os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)


@contextmanager
def _locked_state(run_dir: Path) -> Iterator[dict[str, Any]]:
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / ".controller-operations.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            # Fail closed immediately: no stopped lock owner can block deadline/cancel.
            raise OwnershipError("controller ownership lock unavailable") from exc
        state = read_state(run_dir)
        yield state
        _write_state(run_dir, state)


def activate_generation(run_dir: Path, token: str, deadline: float) -> None:
    """Authorize a new generation only after callers verified prior cleanup."""
    with _locked_state(run_dir) as state:
        if state.get("workers") or state.get("pending"):
            raise OwnershipError("owned workers must be cleaned before generation activation")
        state.clear()
        state.update(
            token=token,
            active=True,
            deadline=deadline,
            operations={},
            workers=[],
            pending=[],
            controller_spawn_pending=True,
        )


def bind_controller(run_dir: Path, token: str, owner: dict[str, Any] | None) -> None:
    """Keep controller identity durable for fail-closed supervisor recovery."""
    with _locked_state(run_dir) as state:
        if state.get("token") != token or state.get("active") is not True:
            raise OwnershipError("controller generation revoked before binding")
        state["controller"] = owner
        state["controller_spawn_pending"] = False


def revoke_generation(run_dir: Path, token: str) -> None:
    """Close the registration gate before controller termination or replacement."""
    with _locked_state(run_dir) as state:
        if state.get("token") != token:
            raise OwnershipError("controller generation ownership changed")
        state["active"] = False


@contextmanager
def operation_scope(
    run_dir: Path | None,
    *,
    phase: str,
    attempt_id: str,
    timeout_seconds: float,
    clock: Callable[[], float] = time.monotonic,
) -> Iterator[str]:
    """Publish once before awaiting; close this exact operation in finally.

    No supervisor means no lease file. The scope does not claim that its body
    obeys a timeout; callers must actually bound their operation.
    """
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0 or not phase or not attempt_id:
        raise ValueError("operation needs identity and a finite positive timeout")
    supervised = os.environ.get(RUN_DIR_ENV)
    token = os.environ.get(TOKEN_ENV)
    if not supervised or not token:
        yield ""
        return
    directory = Path(supervised).resolve()
    if run_dir is not None and run_dir.resolve() != directory:
        raise OwnershipError("operation run directory differs from supervised run")
    owner = process_identity(os.getpid())
    if owner is None:
        raise OwnershipError("controller identity unavailable")
    op_id = secrets.token_hex(16)
    started = clock()
    with _locked_state(directory) as state:
        if state.get("token") != token or state.get("active") is not True:
            raise OwnershipError("controller generation revoked")
        deadline = min(started + timeout_seconds, float(state["deadline"]))
        if deadline <= started:
            raise OwnershipError("overall controller deadline spent")
        state["operations"][op_id] = {
            "id": op_id,
            "token": token,
            "phase": phase,
            "attempt_id": attempt_id,
            "owner": owner,
            "started": started,
            "deadline": deadline,
            "status": "active",
        }
    context_token = _operation_id.set(op_id)
    try:
        yield op_id
    finally:
        _operation_id.reset(context_token)
        with _locked_state(directory) as state:
            if state.get("token") == token:
                operation = state.get("operations", {}).get(op_id)
                if isinstance(operation, dict) and operation.get("status") == "active":
                    operation["status"] = "ended"
                    operation["ended"] = clock()


# Share of the fixed whole-call bound reserved for the executor to settle its
# OWN timeout: launch preparation runs before an adapter starts its timer, and a
# Prime process-group kill may wait up to two term-grace periods. Without this
# reserve the outer bound always fires first and replaces the adapter-attested
# terminal with an unattested one that session feedback must not learn from.
_EXECUTOR_SETTLE_FRACTION = 0.1
_EXECUTOR_SETTLE_CAP_SECONDS = 15.0


def executor_timeout_within(timeout_seconds: float) -> float:
    """Executor-own timeout that lets its attested terminal land inside the bound."""
    reserve = min(_EXECUTOR_SETTLE_CAP_SECONDS, timeout_seconds * _EXECUTOR_SETTLE_FRACTION)
    return timeout_seconds - reserve


async def bounded_executor_call(
    executor: WorkerExecutor,
    prompt: str,
    *,
    route_id: str,
    cwd: Path,
    timeout_seconds: float,
    run_dir: Path | None,
    phase: str,
    attempt_id: str,
) -> WorkerTerminal:
    """Bound the whole initial/repair call, including direct HTTP multi-phase I/O.

    The executor receives a slightly shorter timeout so a cooperating adapter
    times out, cleans up and returns its own attested terminal first. The fixed
    whole-call bound (and the lease deadline) stay ``timeout_seconds`` and only
    fire for an executor that does not honour its timeout.
    """
    # Lazy import keeps the self-registering launcher lightweight and standalone.
    from verdict.orchestration.contracts import WorkerTerminal

    started = time.monotonic()
    with operation_scope(
        run_dir, phase=phase, attempt_id=attempt_id, timeout_seconds=timeout_seconds
    ):
        try:
            return await asyncio.wait_for(
                executor.run(
                    prompt,
                    route_id=route_id,
                    cwd=cwd,
                    timeout_seconds=executor_timeout_within(timeout_seconds),
                ),
                timeout_seconds,
            )
        except asyncio.TimeoutError:
            return WorkerTerminal(
                ok=False,
                error="timeout",
                model=route_id,
                duration_seconds=time.monotonic() - started,
            )


def _cgroup_mount(root: Path) -> Path:
    """Require a real cgroup2 mount, not a regular-directory file imitation."""
    mounts: list[Path] = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        before, separator, after = line.partition(" - ")
        if separator and after.split()[0] == "cgroup2":
            mount = Path(before.split()[4])
            if root.is_relative_to(mount):
                mounts.append(mount)
    if not mounts:
        raise OwnershipError("containment root is not on a cgroup2 mount")
    mount = max(mounts, key=lambda path: len(path.parts))
    if root == mount or root.stat().st_dev != mount.stat().st_dev:
        raise OwnershipError("containment requires a delegated subtree on cgroup2")
    return mount


def _safe_directory(path: Path) -> Path:
    if not path.is_absolute() or any(part.is_symlink() for part in [path, *path.parents]):
        raise OwnershipError("containment path must be absolute without symlinks")
    return path.resolve(strict=True)


def prepare_containment(root: Path, token: str) -> dict[str, Any]:
    """Create only a token-named leaf in an explicitly delegated cgroup-v2 subtree."""
    try:
        root = _safe_directory(root)
        mount = _cgroup_mount(root)
    except OSError as exc:
        raise OwnershipError("containment delegated root unavailable") from exc
    if not (root / "cgroup.type").exists() or not os.access(root, os.W_OK):
        raise OwnershipError("containment root is not a writable delegated cgroup-v2 subtree")
    if not re.fullmatch(r"[a-f0-9]{32}", token):
        raise OwnershipError("invalid containment generation token")
    leaf = root / f"verdict-{token}"
    try:
        leaf.mkdir(mode=0o700)
        for name in ("cgroup.procs", "cgroup.kill"):
            if not os.access(leaf / name, os.W_OK):
                raise OwnershipError(f"containment requires writable {name}")
        if not (leaf / "cgroup.events").is_file():
            raise OwnershipError("containment requires cgroup.events")
        info = leaf.stat()
        return {
            "path": str(leaf),
            "root": str(root),
            "token": token,
            "device": info.st_dev,
            "inode": info.st_ino,
            "mount": str(mount),
            "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        }
    except (OSError, OwnershipError) as exc:
        with contextlib.suppress(OSError):
            leaf.rmdir()
        raise OwnershipError(f"supervised Prime containment unavailable: {exc}") from exc


def _containment_path(record: dict[str, Any], token: str) -> Path:
    try:
        root = _safe_directory(Path(record["root"]))
        leaf = _safe_directory(Path(record["path"]))
        mount = _cgroup_mount(root)
        info = leaf.stat()
        if (
            record.get("token") != token
            or leaf.parent != root
            or leaf.name != f"verdict-{token}"
            or str(mount) != record["mount"]
            or info.st_dev != record["device"]
            or info.st_ino != record["inode"]
            or record["boot"] != Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        ):
            raise OwnershipError("containment identity changed")
        # The controller/supervisor must never be in the leaf they kill.
        if str(os.getpid()) in (leaf / "cgroup.procs").read_text().split():
            raise OwnershipError("refusing to kill or rejoin caller containment")
        return leaf
    except (OSError, KeyError, TypeError) as exc:
        raise OwnershipError("containment identity unavailable") from exc


def join_containment(record: dict[str, Any], token: str) -> None:
    """Join before registration/exec. Normal fork, clone and setsid inherit this leaf."""
    leaf = _containment_path(record, token)
    try:
        (leaf / "cgroup.procs").write_text(str(os.getpid()))
    except OSError as exc:
        raise OwnershipError("cannot join supervised worker containment") from exc


async def kill_containment(record: dict[str, Any], token: str, timeout: float) -> None:
    """Kernel cgroup.kill plus populated=0 is the worker-absence authority."""
    leaf = _containment_path(record, token)
    try:
        (leaf / "cgroup.kill").write_text("1")
        until = time.monotonic() + max(timeout, 0.01)
        while True:
            events = dict(
                line.split() for line in (leaf / "cgroup.events").read_text().splitlines()
            )
            if events.get("populated") == "0":
                return
            if events.get("populated") != "1" or time.monotonic() >= until:
                raise OwnershipError("contained worker absence could not be verified")
            await asyncio.sleep(0.01)
    except (OSError, ValueError) as exc:
        raise OwnershipError("contained worker cleanup unavailable") from exc


def owned_worker_command(command: list[str], environment: dict[str, str]) -> list[str]:
    """Wrap supervised launches; unsupervised command and behavior stay unchanged."""
    if not environment.get(RUN_DIR_ENV):
        return command
    if not environment.get(TOKEN_ENV):
        raise OwnershipError("supervised worker has no generation token")
    environment[OPERATION_ENV] = _operation_id.get()
    launch_id = secrets.token_hex(16)
    directory = Path(environment[RUN_DIR_ENV])
    with _locked_state(directory) as state:
        if state.get("token") != environment[TOKEN_ENV] or state.get("active") is not True:
            raise OwnershipError("worker launch generation revoked")
        if not state.get("containment"):
            configured = environment.get(CGROUP_ROOT_ENV)
            if not configured:
                raise OwnershipError(
                    f"supervised Prime requires explicit {CGROUP_ROOT_ENV} delegation"
                )
            state["containment"] = prepare_containment(Path(configured), environment[TOKEN_ENV])
        state["pending"].append(launch_id)
    environment[LAUNCH_ENV] = launch_id
    return [sys.executable, str(Path(__file__).resolve()), *command]


def register_worker(
    run_dir: Path, token: str, operation_id: str, launch_id: str, command: list[str] | None = None
) -> None:
    """Register this detached session BEFORE worker exec; revoked launch never executes."""
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise OwnershipError("supervised worker ownership requires Linux pidfds")
    identity = process_identity(os.getpid())
    if identity is None or identity["pgid"] != os.getpid() or identity["sid"] != os.getpid():
        raise OwnershipError("supervised worker must lead a new session and process group")
    # Join the validated durable leaf BEFORE acquiring the exec gate. Thus a
    # stopped launcher holding the lock is already kernel-contained and killable.
    initial = read_state(run_dir)
    containment = initial.get("containment")
    if initial.get("active") is not True:
        raise OwnershipError("worker launch generation revoked")
    if initial.get("token") != token or not isinstance(containment, dict):
        raise OwnershipError("worker containment generation unavailable")
    join_containment(containment, token)
    with _locked_state(run_dir) as state:
        if state.get("active") is not True or state.get("token") != token:
            raise OwnershipError("worker launch generation revoked")
        if time.monotonic() >= float(state["deadline"]):
            raise OwnershipError("worker launch overall deadline spent")
        if launch_id not in state.get("pending", []):
            raise OwnershipError("worker launch intent unavailable")
        if state.get("containment") != containment:
            raise OwnershipError("worker containment changed after join")
        state["pending"].remove(launch_id)
        state["workers"].append(
            {"identity": identity, "token": token, "operation_id": operation_id}
        )
        if command is not None:
            if not command:
                raise OwnershipError("missing worker command")
            # Hold the stable lock through exec. CLOEXEC releases it only when
            # payload execution starts, so revocation cannot cross the check/exec gap.
            _write_state(run_dir, state)
            os.execvpe(command[0], command, os.environ)


def _signal_identity(identity: dict[str, Any], sig: signal.Signals) -> None:
    """Open pidfd then recheck identity. Signal that handle, never a reused PID/group."""
    try:
        fd = os.pidfd_open(identity["pid"])
    except ProcessLookupError:
        return
    except OSError as exc:
        raise OwnershipError("cannot open owned worker pidfd") from exc
    try:
        if not same_process(process_identity(identity["pid"]), identity):
            raise OwnershipError("worker identity changed before signal")
        with contextlib.suppress(ProcessLookupError):
            signal.pidfd_send_signal(fd, sig)
    finally:
        os.close(fd)


async def cleanup_workers(
    run_dir: Path,
    *,
    grace_seconds: float = 1.0,
    snapshot: dict[str, Any] | None = None,
    revocation_verified: bool = True,
) -> None:
    """Kill containment and verify populated=0 before allowing replacement.

    Normal fork/setsid descendants are contained. Deliberate same-UID containment
    escape or coordination-state tampering is outside this trusted-controller model.
    """
    state = snapshot if snapshot is not None else read_state(run_dir)
    if state.get("active") is True and revocation_verified:
        raise OwnershipError("cannot clean workers of an active generation")
    workers = state.get("workers", [])
    if not isinstance(workers, list):
        raise OwnershipError("invalid owned worker registry")
    identities: list[dict[str, Any]] = []
    for worker in workers:
        if (
            not isinstance(worker, dict)
            or worker.get("token") != state.get("token")
            or not isinstance(worker.get("identity"), dict)
        ):
            raise OwnershipError("invalid worker ownership record")
        identities.append(worker["identity"])
    containment = state.get("containment")
    if isinstance(containment, dict):
        # Kill known containment even if pending intents or revocation uncertainty
        # will prevent restart. No /proc scan or numeric PID signal is absence proof.
        await kill_containment(containment, str(state.get("token", "")), grace_seconds)
    elif identities:
        # Without containment we cannot safely assert fork-closure absence.
        # Stop only exact known handles, then fail closed (never restart).
        for identity in identities:
            actual = process_identity(identity["pid"])
            if actual is not None:
                _signal_identity(identity, signal.SIGKILL)
        raise OwnershipError("worker containment absence unverified")
    for identity in identities:
        actual = process_identity(identity["pid"])
        if actual is not None and not same_process(actual, identity):
            raise OwnershipError("worker PID identity changed; refusing unrelated signals")
    if not revocation_verified:
        raise OwnershipError("known workers stopped; generation revocation unverified")
    if state.get("pending"):
        raise OwnershipError(
            "known workers stopped; pending worker launch cleanup unknown; restart refused"
        )
    with _locked_state(run_dir) as current:
        if current.get("token") != state.get("token") or current.get("active") is True:
            raise OwnershipError("generation changed during worker cleanup")
        if isinstance(containment, dict):
            leaf = _containment_path(containment, str(state["token"]))
            try:
                leaf.rmdir()
            except OSError as exc:
                raise OwnershipError("empty containment removal unverified") from exc
            current["containment"] = None
        current["workers"] = []


def _launch() -> None:
    try:
        register_worker(
            Path(os.environ[RUN_DIR_ENV]),
            os.environ[TOKEN_ENV],
            os.environ.get(OPERATION_ENV, ""),
            os.environ[LAUNCH_ENV],
            sys.argv[1:],
        )
    except (OwnershipError, OSError, KeyError) as exc:
        print(f"owned worker launch refused: {exc}", file=sys.stderr)
        raise SystemExit(125) from exc


if __name__ == "__main__":
    _launch()
