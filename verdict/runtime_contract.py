"""Versioned, privacy-safe contracts for global runtime ownership."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from verdict.security import redact_text

RUNTIME_CONTRACT_VERSION = "1"
DEFAULT_RUNTIME_STATE_DIR = Path("~/.verdict/runtime")


class RuntimeManagerError(RuntimeError):
    """Raised when a runtime operation cannot be proven safe."""


@dataclass(frozen=True)
class RuntimeServiceSpec:
    """Versioned ownership and identity contract for one global service."""

    service_id: str
    display_name: str
    kind: str
    endpoint: str | None
    health_endpoint: str | None
    launcher_env: str
    command_markers: tuple[str, ...]
    state_filename: str
    pid_filename: str
    lock_filename: str
    global_state_root: str

    def to_dict(self, *, state_dir: Path) -> dict[str, Any]:
        return {
            "service_id": self.service_id,
            "display_name": self.display_name,
            "kind": self.kind,
            "endpoint": self.endpoint,
            "health_endpoint": self.health_endpoint,
            "launcher_env": self.launcher_env,
            "command_markers": list(self.command_markers),
            "state_path": str(state_dir / self.state_filename),
            "pid_path": str(state_dir / self.pid_filename),
            "lock_path": str(state_dir / self.lock_filename),
            "global_state_root": self.global_state_root,
            "contract_version": RUNTIME_CONTRACT_VERSION,
        }


@dataclass(frozen=True)
class ProcessSnapshot:
    """Privacy-safe process identity data used by the classifier."""

    pid: int
    uid: int | None
    start_time: str | None
    argv: tuple[str, ...]
    cwd: str | None

    @property
    def command(self) -> str:
        return redact_text(" ".join(self.argv))

    @property
    def argv_hash(self) -> str:
        return hashlib.sha256("\0".join(self.argv).encode()).hexdigest()


class ProcessInspector(Protocol):
    """Minimal process inspection boundary, replaceable in deterministic tests."""

    def snapshots(self) -> Sequence[ProcessSnapshot]: ...

    def snapshot(self, pid: int) -> ProcessSnapshot | None: ...


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


@dataclass(frozen=True)
class OwnershipRecord:
    """Persisted identity proof for the one canonical owner of a service."""

    service_id: str
    pid: int
    uid: int
    start_time: str
    argv_hash: str
    endpoint: str | None
    created_at: float
    contract_version: str = RUNTIME_CONTRACT_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "service_id": self.service_id,
            "pid": self.pid,
            "uid": self.uid,
            "start_time": self.start_time,
            "argv_hash": self.argv_hash,
            "endpoint": self.endpoint,
            "created_at": self.created_at,
            "contract_version": self.contract_version,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> OwnershipRecord:
        return cls(
            service_id=str(value["service_id"]),
            pid=int(value["pid"]),
            uid=int(value["uid"]),
            start_time=str(value["start_time"]),
            argv_hash=str(value["argv_hash"]),
            endpoint=value.get("endpoint"),
            created_at=float(value["created_at"]),
            contract_version=str(value.get("contract_version", "")),
        )


@dataclass(frozen=True)
class RuntimeProcess:
    """Classified matching process suitable for a machine-readable report."""

    pid: int
    service_id: str
    owner: str
    workspace: str | None
    version: str | None
    endpoint: str | None
    state_path: str
    command: str
    start_time: str | None
    identity_verified: bool
    classification: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "service_id": self.service_id,
            "owner": self.owner,
            "workspace": self.workspace,
            "version": self.version,
            "endpoint": self.endpoint,
            "state_path": self.state_path,
            "command": self.command,
            "start_time": self.start_time,
            "identity_verified": self.identity_verified,
            "classification": self.classification,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class RuntimePlan:
    """Deterministic status and proposed actions."""

    status: str
    contract_version: str
    state_dir: str
    services: tuple[dict[str, Any], ...]
    processes: tuple[dict[str, Any], ...]
    actions: tuple[dict[str, Any], ...]
    errors: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.status == "ready"

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": "runtime",
            "status": self.status,
            "passed": self.passed,
            "contract_version": self.contract_version,
            "state_dir": self.state_dir,
            "services": [dict(item) for item in self.services],
            "processes": [dict(item) for item in self.processes],
            "actions": [dict(item) for item in self.actions],
            "errors": list(self.errors),
        }


def default_service_specs(home: Path | None = None) -> tuple[RuntimeServiceSpec, ...]:
    """Return the canonical global service contract."""
    root = (home or Path.home()).expanduser().resolve()
    return (
        RuntimeServiceSpec(
            "ruflo-daemon",
            "Ruflo/claude-flow global daemon",
            "daemon",
            None,
            None,
            "VERDICT_RUFLO_DAEMON_COMMAND",
            ("ruflo-daemon",),
            "ruflo-daemon.ownership.json",
            "ruflo-daemon.pid",
            "ruflo-daemon.lock",
            str(root / ".claude-flow"),
        ),
        RuntimeServiceSpec(
            "ruflo-mcp",
            "Ruflo global MCP bridge",
            "mcp",
            "http://127.0.0.1:20133/mcp",
            "http://127.0.0.1:20133/healthz",
            "VERDICT_RUFLO_MCP_COMMAND",
            ("ruflo-global-mcp", "20133"),
            "ruflo-mcp.ownership.json",
            "ruflo-mcp.pid",
            "ruflo-mcp.lock",
            str(root / ".claude-flow"),
        ),
        RuntimeServiceSpec(
            "ruvector-mcp",
            "RuVector global MCP bridge",
            "mcp",
            "http://127.0.0.1:20130/mcp",
            "http://127.0.0.1:20130/healthz",
            "VERDICT_RUVECTOR_MCP_COMMAND",
            ("ruvector-global-mcp", "20130"),
            "ruvector-mcp.ownership.json",
            "ruvector-mcp.pid",
            "ruvector-mcp.lock",
            str(root / ".ruvector"),
        ),
    )


__all__ = [
    "DEFAULT_RUNTIME_STATE_DIR",
    "RUNTIME_CONTRACT_VERSION",
    "OwnershipRecord",
    "ProcessInspector",
    "ProcessSnapshot",
    "ProcfsInspector",
    "RuntimeManagerError",
    "RuntimePlan",
    "RuntimeProcess",
    "RuntimeServiceSpec",
    "default_service_specs",
]
