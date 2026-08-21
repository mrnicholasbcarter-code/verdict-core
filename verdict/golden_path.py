"""Offline, fail-closed autonomous-development golden path (#266).

This module deliberately has no provider, router, or credential dependencies.  It
inspects a real Git checkout, writes one redacted record to durable MemoryPlane,
and runs an explicitly bounded verification command.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from verdict.memory_plane import MemoryPlane, MemoryRecord

SCHEMA_VERSION = "1"
_VOLATILE = {"occurred_at", "duration_ms"}


class Stage(str, Enum):
    DISCOVERY = "discovery"
    MEMORY = "memory"
    VERIFICATION = "verification"


class StageStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _safe_command(command: Sequence[str]) -> tuple[str, ...]:
    if not command or any(not isinstance(part, str) or not part.strip() for part in command):
        raise ValueError("verification command must contain non-empty arguments")
    # Commands are evidence, not arbitrary task text.  Keep output portable and bounded.
    if any(
        len(part) > 160
        or any(marker in part.lower() for marker in ("api_key", "token=", "password"))
        for part in command
    ):
        raise ValueError("verification command contains unsafe or oversized text")
    return tuple(command)


@dataclass(frozen=True)
class StageReceipt:
    stage: Stage
    status: StageStatus
    mission_id: str
    receipt_id: str
    source_revision: str | None
    evidence: dict[str, Any]
    limitations: tuple[str, ...] = ()
    occurred_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.mission_id or not self.receipt_id:
            raise ValueError("receipt identity is required")
        if any("/home/" in str(v) or "api_key" in str(v).lower() for v in self.evidence.values()):
            raise ValueError("receipt evidence contains private or secret material")

    @property
    def evidence_digest(self) -> str:
        stable = {key: value for key, value in self.evidence.items() if key not in _VOLATILE}
        return _digest(stable)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "stage": self.stage.value,
            "status": self.status.value,
            "mission_id": self.mission_id,
            "receipt_id": self.receipt_id,
            "source_revision": self.source_revision,
            "evidence": self.evidence,
            "evidence_digest": self.evidence_digest,
            "limitations": list(self.limitations),
            "occurred_at": self.occurred_at,
        }


@dataclass(frozen=True)
class GoldenPathReport:
    mission_id: str
    source_revision: str | None
    stages: tuple[StageReceipt, ...]
    decision: str
    limitations: tuple[str, ...] = ()

    @property
    def report_digest(self) -> str:
        return _digest(self._canonical())

    def _canonical(self) -> dict[str, Any]:
        payload = self.to_dict(include_digest=False)
        for stage in payload["stages"]:
            stage.pop("occurred_at", None)
            evidence = stage.get("evidence", {})
            for key in _VOLATILE:
                evidence.pop(key, None)
        return payload

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "mission_id": self.mission_id,
            "source_revision": self.source_revision,
            "stages": [item.to_dict() for item in self.stages],
            "decision": self.decision,
            "limitations": list(self.limitations),
        }
        if include_digest:
            payload["report_digest"] = self.report_digest
        return payload

    def summary(self) -> str:
        return f"{self.decision}: " + " -> ".join(f"{r.stage}={r.status}" for r in self.stages)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", *args), cwd=repo, text=True, capture_output=True, check=True, timeout=5
    )
    return result.stdout.strip()


def _receipt_id(mission: str, stage: Stage) -> str:
    return f"{stage.value}:{hashlib.sha256(f'{mission}:{stage.value}'.encode()).hexdigest()[:16]}"


def _independent_memory_hash(path: str | Path, namespace: str, key: str, scope: str) -> str:
    """Read the persisted record through a separate Python process."""
    script = (
        "import sys; from verdict.memory_plane import MemoryPlane; "
        "p=MemoryPlane(sys.argv[1]); r=p.get(sys.argv[2], sys.argv[3], scope=sys.argv[4]); "
        "print(r.content_hash if r else '')"
    )
    result = subprocess.run(
        (sys.executable, "-c", script, str(path), namespace, key, scope),
        text=True,
        capture_output=True,
        check=True,
        timeout=5,
    )
    return result.stdout.strip()


def run_golden_path(
    objective: str,
    repo: str | Path,
    *,
    memory_path: str | Path,
    verification_command: Sequence[str] = ("git", "status", "--short"),
    timeout_seconds: float = 10.0,
    allow_dirty: bool = False,
    owned_paths: Sequence[str] = (),
    clock: float | None = None,
) -> GoldenPathReport:
    """Run discovery, durable memory, then bounded verification in that order."""
    if not objective.strip() or len(objective) > 400:
        raise ValueError("objective must be non-empty and bounded")
    if timeout_seconds <= 0 or timeout_seconds > 300:
        raise ValueError("timeout_seconds must be between 0 and 300")
    root = Path(repo).resolve()
    now = time.time() if clock is None else clock
    try:
        root_revision = _git(root, "rev-parse", "HEAD")
        dirty = bool(_git(root, "status", "--porcelain"))
        if dirty and not allow_dirty:
            raise RuntimeError("repository is dirty")
        label = root.name or "repository"
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        mission = "mission:" + _digest({"objective": objective, "repo": root.name})[7:23]
        failed = StageReceipt(
            Stage.DISCOVERY,
            StageStatus.FAILED,
            mission,
            _receipt_id(mission, Stage.DISCOVERY),
            None,
            {"repository": root.name, "reason": type(exc).__name__},
            ("source identity unavailable",),
            now,
        )
        unavailable = tuple(
            StageReceipt(
                stage,
                StageStatus.UNKNOWN,
                mission,
                _receipt_id(mission, stage),
                None,
                {"reason": "discovery prerequisite failed"},
                ("stage not run",),
                now,
            )
            for stage in (Stage.MEMORY, Stage.VERIFICATION)
        )
        return GoldenPathReport(mission, None, (failed, *unavailable), "denied", failed.limitations)

    mission = (
        "mission:"
        + _digest({"objective": objective, "repo": label, "revision": root_revision})[7:23]
    )
    discovery = StageReceipt(
        Stage.DISCOVERY,
        StageStatus.PASSED,
        mission,
        _receipt_id(mission, Stage.DISCOVERY),
        root_revision,
        {
            "repository": label,
            "git": "present",
            "clean": not dirty,
            "source_revision": root_revision,
        },
        (),
        now,
    )
    receipts: list[StageReceipt] = [discovery]
    memory_evidence = {
        "record_key": mission,
        "source_revision": root_revision,
        "retrieval": "independent-reopen",
    }
    try:
        content = json.dumps(
            {
                "mission_id": mission,
                "source_revision": root_revision,
                "objective_digest": _digest(objective),
            },
            sort_keys=True,
        )
        record = MemoryRecord(
            f"golden:{mission}",
            "autodev",
            mission,
            content,
            "golden-path",
            scope=root_revision,
            metadata={"repository": label},
            provenance={"source_revision": root_revision},
            confidence=1.0,
        )
        with MemoryPlane(memory_path) as plane:
            stored = plane.put(record)
        independent_hash = _independent_memory_hash(memory_path, "autodev", mission, root_revision)
        if not independent_hash or independent_hash != stored.content_hash:
            raise RuntimeError("durable memory retrieval mismatch")
        receipts.append(
            StageReceipt(
                Stage.MEMORY,
                StageStatus.PASSED,
                mission,
                _receipt_id(mission, Stage.MEMORY),
                root_revision,
                memory_evidence,
                (),
                now,
            )
        )
    except Exception as exc:
        receipts.append(
            StageReceipt(
                Stage.MEMORY,
                StageStatus.UNAVAILABLE,
                mission,
                _receipt_id(mission, Stage.MEMORY),
                root_revision,
                {"reason": type(exc).__name__},
                ("durable memory proof unavailable",),
                now,
            )
        )
        receipts.append(
            StageReceipt(
                Stage.VERIFICATION,
                StageStatus.UNKNOWN,
                mission,
                _receipt_id(mission, Stage.VERIFICATION),
                root_revision,
                {"reason": "memory prerequisite failed"},
                ("stage not run",),
                now,
            )
        )
        return GoldenPathReport(
            mission, root_revision, tuple(receipts), "denied", ("memory prerequisite failed",)
        )

    command = _safe_command(verification_command)
    try:
        started = time.monotonic()
        result = subprocess.run(
            command, cwd=root, text=True, capture_output=True, timeout=timeout_seconds, check=False
        )
        duration = round((time.monotonic() - started) * 1000, 3)
        changed = tuple(filter(None, _git(root, "status", "--porcelain").splitlines()))
        changed_names = tuple(line[3:] for line in changed if len(line) >= 4)
        outside = tuple(
            name
            for name in changed_names
            if not any(
                name == allowed or name.startswith(allowed.rstrip("/") + "/")
                for allowed in owned_paths
            )
        )
        status = (
            StageStatus.PASSED if result.returncode == 0 and not outside else StageStatus.FAILED
        )
        evidence = {
            "command": list(command),
            "exit_code": result.returncode,
            "timed_out": False,
            "duration_ms": duration,
            "changed_paths": len(changed_names),
            "outside_owned_paths": len(outside),
            "stdout_digest": _digest(result.stdout[:4096]),
            "stderr_digest": _digest(result.stderr[:4096]),
        }
        limitations = (
            ("verification changed paths outside the declared boundary",) if outside else ()
        )
    except subprocess.TimeoutExpired:
        status, evidence, limitations = (
            StageStatus.FAILED,
            {"command": list(command), "timed_out": True},
            ("verification timeout",),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        status, evidence, limitations = (
            StageStatus.FAILED,
            {"command": list(command), "reason": type(exc).__name__},
            ("verification could not execute",),
        )
    receipts.append(
        StageReceipt(
            Stage.VERIFICATION,
            status,
            mission,
            _receipt_id(mission, Stage.VERIFICATION),
            root_revision,
            evidence,
            limitations,
            now,
        )
    )
    decision = "accepted" if all(r.status == StageStatus.PASSED for r in receipts) else "denied"
    return GoldenPathReport(mission, root_revision, tuple(receipts), decision, limitations)
