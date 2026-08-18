"""Safe ownership, discovery, and reconciliation for global runtime services.

The runtime manager is deliberately conservative.  It discovers processes from
``/proc`` (or an injected inspector in tests), classifies them only when the
versioned service contract matches multiple identity signals, and never sends
signals during a planning operation.  Verdict does not require Ruflo or
RuVector to be installed; an absent service is reported as unavailable.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shlex
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

from verdict.runtime_contract import (
    DEFAULT_RUNTIME_STATE_DIR,
    RUNTIME_CONTRACT_VERSION,
    OwnershipRecord,
    ProcessInspector,
    ProcessSnapshot,
    RuntimeManagerError,
    RuntimePlan,
    RuntimeProcess,
    RuntimeServiceSpec,
    default_service_specs,
)
from verdict.security import redact_text


class ProcfsInspector:
    """Read process identity from Linux procfs without reading environments."""

    def __init__(self, proc_root: Path = Path("/proc")) -> None:
        self.proc_root = proc_root

    def snapshots(self) -> Sequence[ProcessSnapshot]:
        result: list[ProcessSnapshot] = []
        for entry in sorted(self.proc_root.iterdir(), key=lambda item: item.name):
            if not entry.name.isdecimal():
                continue
            snapshot = self.snapshot(int(entry.name))
            if snapshot is not None:
                result.append(snapshot)
        return result

    def snapshot(self, pid: int) -> ProcessSnapshot | None:
        process = self.proc_root / str(pid)
        try:
            raw_argv = (process / "cmdline").read_bytes().split(b"\0")
            argv = tuple(item.decode("utf-8", errors="replace") for item in raw_argv if item)
            if not argv:
                return None
            stat_fields = (process / "stat").read_text(encoding="utf-8").split()
            uid = (process / "status").read_text(encoding="utf-8").split("Uid:", 1)[1].split()[0]
            return ProcessSnapshot(
                pid=pid,
                uid=int(uid),
                start_time=stat_fields[21],
                argv=argv,
                cwd=os.readlink(process / "cwd"),
            )
        except (OSError, IndexError, ValueError):
            return None


def _default_state_dir() -> Path:
    configured = os.getenv("VERDICT_RUNTIME_STATE_DIR")
    return Path(configured).expanduser() if configured else DEFAULT_RUNTIME_STATE_DIR.expanduser()


class RuntimeManager:
    """Manage identity-safe runtime status, planning, and explicit apply."""

    def __init__(
        self,
        *,
        state_dir: Path | None = None,
        specs: Sequence[RuntimeServiceSpec] | None = None,
        inspector: ProcessInspector | None = None,
        uid: int | None = None,
        clock: Callable[[], float] | None = None,
        port_probe: Callable[[str], bool | None] | None = None,
        health_probe: Callable[[str], str] | None = None,
        home: Path | None = None,
        sleep: Callable[[float], None] | None = None,
        shutdown_timeout: float = 5.0,
    ) -> None:
        self.state_dir = (state_dir or _default_state_dir()).expanduser().resolve()
        self.home = (home or Path.home()).expanduser().resolve()
        self.specs = tuple(specs or default_service_specs(self.home))
        self.inspector = inspector or ProcfsInspector()
        self.uid = os.getuid() if uid is None else uid
        self.clock = clock or time.time
        self.port_probe = port_probe or _probe_endpoint
        self.health_probe = health_probe or _probe_health
        self.sleep = sleep or time.sleep
        self.shutdown_timeout = shutdown_timeout

    def status(self) -> RuntimePlan:
        """Return deterministic status without creating state or signaling processes."""
        return self._plan(apply=False)

    def reconcile_plan(self) -> RuntimePlan:
        """Return a read-only deterministic reconciliation plan."""
        return self._plan(apply=False)

    def reconcile_apply(self, *, service_ids: Sequence[str], consent: bool) -> RuntimePlan:
        """Apply only explicitly scoped, proven duplicate-stop actions."""
        if not consent:
            raise RuntimeManagerError("reconcile --apply requires explicit consent")
        selected = tuple(sorted(set(service_ids)))
        known = {spec.service_id for spec in self.specs}
        if not selected or not set(selected).issubset(known):
            raise RuntimeManagerError(
                "reconcile --apply requires one or more known --service values"
            )
        plan = self._plan(apply=False)
        if plan.errors:
            return plan
        actions = [
            action
            for action in plan.actions
            if action["service_id"] in selected and action["action"] == "stop-duplicate"
        ]
        errors = list(plan.errors)
        for action in actions:
            pid = int(action["pid"])
            expected = self.inspector.snapshot(pid)
            if expected is None or expected.start_time != action["start_time"]:
                errors.append(f"{action['service_id']}:pid-revalidation-failed:{pid}")
                continue
            if expected.argv_hash != action["argv_hash"]:
                errors.append(f"{action['service_id']}:command-revalidation-failed:{pid}")
                continue
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                continue
            except PermissionError:
                errors.append(f"{action['service_id']}:permission-denied:{pid}")
                continue
            deadline = time.monotonic() + self.shutdown_timeout
            while time.monotonic() < deadline and self.inspector.snapshot(pid) is not None:
                self.sleep(0.01)
            if self.inspector.snapshot(pid) is not None:
                errors.append(f"{action['service_id']}:shutdown-timeout:{pid}")
        final = self._plan(apply=False)
        return RuntimePlan(
            status="ready" if not errors and not final.actions else "blocked",
            contract_version=final.contract_version,
            state_dir=final.state_dir,
            services=final.services,
            processes=final.processes,
            actions=final.actions,
            errors=tuple(sorted(set(errors))),
        )

    def start(self, service_id: str, command: Sequence[str] | None = None) -> RuntimePlan:
        """Start a configured service exactly once, requiring an explicit argv."""
        spec = self._spec(service_id)
        configured = command or _configured_command(spec.launcher_env)
        if not configured:
            raise RuntimeManagerError(
                f"{service_id} has no launcher; set {spec.launcher_env} or pass an argv"
            )
        if not all(marker in "\0".join(configured) for marker in spec.command_markers):
            raise RuntimeManagerError(
                f"{service_id} launcher does not match the ownership contract"
            )
        with self._lock(spec):
            existing = self._canonical_process(spec)
            if existing is not None:
                return self.status()
            self.state_dir.mkdir(parents=True, exist_ok=True)
            process = subprocess.Popen(  # nosec B603: argv is explicit and shell=False.
                list(configured),
                cwd=str(self.state_dir),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                start_new_session=True,
            )
            deadline = self.clock() + 5.0
            observed: ProcessSnapshot | None = None
            while self.clock() < deadline:
                observed = self.inspector.snapshot(process.pid)
                if observed is not None:
                    break
                time.sleep(0.01)
            if observed is None:
                raise RuntimeManagerError(f"{service_id} did not expose process identity")
            self._write_ownership(
                spec,
                OwnershipRecord(
                    service_id=service_id,
                    pid=observed.pid,
                    uid=observed.uid if observed.uid is not None else self.uid,
                    start_time=observed.start_time or "unknown",
                    argv_hash=observed.argv_hash,
                    endpoint=spec.endpoint,
                    created_at=self.clock(),
                ),
            )
            self._write_pid(spec, observed.pid)
        return self.status()

    def _plan(self, *, apply: bool) -> RuntimePlan:
        del apply  # Kept in the private API so plan/apply cannot accidentally share semantics.
        process_snapshots = tuple(self.inspector.snapshots())
        classified: list[RuntimeProcess] = []
        actions: list[dict[str, Any]] = []
        errors: list[str] = []
        services: list[dict[str, Any]] = []
        for spec in self.specs:
            ownership = self._read_ownership(spec)
            matches = [item for item in process_snapshots if _matches(item, spec)]
            canonical = _ownership_matches(ownership, matches, self.uid)
            ownership_process = (
                next((item for item in process_snapshots if item.pid == ownership.pid), None)
                if ownership
                else None
            )
            if ownership and ownership_process is not None and ownership_process not in matches:
                classified.append(
                    RuntimeProcess(
                        ownership_process.pid,
                        spec.service_id,
                        f"uid:{ownership_process.uid}"
                        if ownership_process.uid is not None
                        else "unknown",
                        ownership_process.cwd,
                        _version_from_argv(ownership_process.argv),
                        spec.endpoint,
                        str(self.state_dir / spec.state_filename),
                        ownership_process.command,
                        ownership_process.start_time,
                        False,
                        "ambiguous",
                        "ownership record points to a process whose command no longer matches",
                    )
                )
                errors.append(f"{spec.service_id}:ambiguous-process-identity")
            for item in matches:
                verified = _identity_verified(item, spec, self.uid, self.home)
                if ownership and item.pid == ownership.pid:
                    classification = "canonical" if canonical is item else "ambiguous"
                    reason = (
                        "ownership record matches pid, uid, start time, and argv"
                        if canonical is item
                        else "ownership record does not match current process identity"
                    )
                elif canonical is not None and verified:
                    classification = "duplicate"
                    reason = "contract markers and owner identity match; canonical owner is recorded separately"
                else:
                    classification = "ambiguous"
                    reason = "matching markers lack sufficient ownership proof"
                classified.append(
                    RuntimeProcess(
                        item.pid,
                        spec.service_id,
                        f"uid:{item.uid}" if item.uid is not None else "unknown",
                        item.cwd,
                        _version_from_argv(item.argv),
                        spec.endpoint,
                        str(self.state_dir / spec.state_filename),
                        item.command,
                        item.start_time,
                        verified,
                        classification,
                        reason,
                    )
                )
                if classification == "duplicate" and len(matches) >= 2:
                    actions.append(
                        {
                            "action": "stop-duplicate",
                            "service_id": spec.service_id,
                            "pid": item.pid,
                            "start_time": item.start_time,
                            "argv_hash": item.argv_hash,
                            "reason": reason,
                        }
                    )
            port_state = self._port_state(spec)
            health_state = (
                self.health_probe(spec.health_endpoint)
                if spec.health_endpoint
                else "not-applicable"
            )
            service_status = "ready" if canonical is not None else "unavailable"
            if any(
                item.classification == "ambiguous"
                for item in classified
                if item.service_id == spec.service_id
            ):
                service_status = "ambiguous"
                errors.append(f"{spec.service_id}:ambiguous-process-identity")
            if port_state == "occupied" and canonical is None and not matches:
                service_status = "port-collision"
                errors.append(f"{spec.service_id}:port-collision")
            services.append(
                {
                    **spec.to_dict(state_dir=self.state_dir),
                    "status": service_status,
                    "owner_pid": canonical.pid if canonical else None,
                    "port_state": port_state,
                    "health": health_state,
                }
            )
        status = "ready" if not errors and not actions else "blocked"
        return RuntimePlan(
            status=status,
            contract_version=RUNTIME_CONTRACT_VERSION,
            state_dir=str(self.state_dir),
            services=tuple(services),
            processes=tuple(
                item.to_dict()
                for item in sorted(classified, key=lambda item: (item.service_id, item.pid))
            ),
            actions=tuple(sorted(actions, key=lambda item: (item["service_id"], item["pid"]))),
            errors=tuple(sorted(set(errors))),
        )

    def _spec(self, service_id: str) -> RuntimeServiceSpec:
        for spec in self.specs:
            if spec.service_id == service_id:
                return spec
        raise RuntimeManagerError(f"unknown runtime service: {service_id}")

    def _canonical_process(self, spec: RuntimeServiceSpec) -> ProcessSnapshot | None:
        record = self._read_ownership(spec)
        if record is None:
            return None
        current = self.inspector.snapshot(record.pid)
        if current is None or not _matches(current, spec):
            return None
        if not _identity_verified(current, spec, self.uid, self.home):
            return None
        return current if _ownership_matches(record, [current], self.uid) is current else None

    def _ownership_path(self, spec: RuntimeServiceSpec) -> Path:
        return self.state_dir / spec.state_filename

    def _read_ownership(self, spec: RuntimeServiceSpec) -> OwnershipRecord | None:
        try:
            raw = json.loads(self._ownership_path(spec).read_text(encoding="utf-8"))
            record = OwnershipRecord.from_dict(raw)
            if record.contract_version != RUNTIME_CONTRACT_VERSION:
                return None
            return record
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def _write_ownership(self, spec: RuntimeServiceSpec, record: OwnershipRecord) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self._ownership_path(spec)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(record.to_dict(), sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)

    def _write_pid(self, spec: RuntimeServiceSpec, pid: int) -> None:
        """Write a convenience PID file; ownership JSON remains authoritative."""
        path = self.state_dir / spec.pid_filename
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(f"{pid}\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)

    @contextlib.contextmanager
    def _lock(self, spec: RuntimeServiceSpec) -> Iterator[None]:
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self.state_dir / spec.lock_filename
        with lock_path.open("a+", encoding="utf-8") as handle:
            os.chmod(lock_path, 0o600)
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeManagerError(f"{spec.service_id}:lock-contention") from exc
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _port_state(self, spec: RuntimeServiceSpec) -> str:
        if not spec.endpoint:
            return "not-applicable"
        result = self.port_probe(spec.endpoint)
        return "occupied" if result is True else "available" if result is False else "unknown"


def _matches(process: ProcessSnapshot, spec: RuntimeServiceSpec) -> bool:
    command = "\0".join(process.argv)
    return all(marker in command for marker in spec.command_markers)


def _identity_verified(
    process: ProcessSnapshot, spec: RuntimeServiceSpec, uid: int, home: Path
) -> bool:
    if process.uid != uid or not _matches(process, spec):
        return False
    cwd = Path(process.cwd).resolve() if process.cwd else None
    return bool(cwd and (cwd == home or home in cwd.parents)) or any(
        str(home) in argument for argument in process.argv
    )


def _ownership_matches(
    record: OwnershipRecord | None, matches: Sequence[ProcessSnapshot], uid: int
) -> ProcessSnapshot | None:
    if record is None:
        return None
    for process in matches:
        if (
            process.pid == record.pid
            and process.uid == uid
            and process.uid == record.uid
            and process.start_time == record.start_time
            and process.argv_hash == record.argv_hash
        ):
            return process
    return None


def _configured_command(variable: str) -> tuple[str, ...] | None:
    value = os.getenv(variable)
    if not value:
        return None
    command = tuple(shlex.split(value))
    return command or None


def _probe_endpoint(endpoint: str) -> bool | None:
    try:
        parsed = endpoint.split("://", 1)[1].split("/", 1)[0]
        host, port_text = parsed.rsplit(":", 1)
        with socket.create_connection((host, int(port_text)), timeout=0.1):
            return True
    except (OSError, ValueError, IndexError):
        return False


def _probe_health(endpoint: str) -> str:
    """Perform a bounded GET without exposing response bodies or credentials."""
    try:
        request = urllib.request.Request(endpoint, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=1.0) as response:  # nosec B310
            return "healthy" if 200 <= response.status < 300 else "unhealthy"
    except (OSError, urllib.error.URLError, TimeoutError):
        return "unavailable"


def _version_from_argv(argv: Sequence[str]) -> str | None:
    for index, value in enumerate(argv):
        if value in {"--version", "-v"} and index + 1 < len(argv):
            return redact_text(argv[index + 1])
    return None


__all__ = [
    "DEFAULT_RUNTIME_STATE_DIR",
    "RUNTIME_CONTRACT_VERSION",
    "OwnershipRecord",
    "ProcessInspector",
    "ProcessSnapshot",
    "ProcfsInspector",
    "RuntimeManager",
    "RuntimeManagerError",
    "RuntimePlan",
    "RuntimeProcess",
    "RuntimeServiceSpec",
    "default_service_specs",
]
