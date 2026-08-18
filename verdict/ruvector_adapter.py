"""Bounded, advisory-only RuVector readiness and capability negotiation.

This module owns no RuVector storage and does not import a RuVector client.
It probes an executable through an argv-only boundary, caps both duration and
output, and turns unavailable or unsupported commands into explicit degraded
readiness instead of pretending that advisory retrieval is available.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

ADAPTER_PROTOCOL_VERSION = "ruvector-adapter/v1"
DEFAULT_TIMEOUT_MS = 1_000
DEFAULT_MAX_OUTPUT_BYTES = 16_384
_VERSION_PATTERN = re.compile(r"(?i)\b(?:ruvector|rvf)[^\r\n]*?\bv?(\d+\.\d+\.\d+)\b")


class ReadinessStatus(str, Enum):
    """Advisory backend state; only READY permits advisory retrieval."""

    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ProbeResult:
    """Redacted process result with bounded output."""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    output_truncated: bool = False


@dataclass(frozen=True)
class RuVectorReadiness:
    """Versioned readiness report suitable for diagnostics/evidence."""

    status: ReadinessStatus
    executable: str
    version: str | None
    supported_commands: tuple[str, ...] = ()
    required_commands: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    adapter_version: str = ADAPTER_PROTOCOL_VERSION

    @property
    def advisory_retrieval_enabled(self) -> bool:
        """Whether callers may use RuVector as an advisory primitive."""

        return self.status is ReadinessStatus.READY

    @property
    def digest(self) -> str:
        payload = self.to_dict(include_digest=False)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return "sha256:" + hashlib.sha256(encoded).hexdigest()

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.adapter_version,
            "status": self.status.value,
            "executable": self.executable,
            "version": self.version,
            "supported_commands": list(self.supported_commands),
            "required_commands": list(self.required_commands),
            "limitations": list(self.limitations),
            "advisory_retrieval_enabled": self.advisory_retrieval_enabled,
        }
        if include_digest:
            payload["digest"] = self.digest
        return payload


Runner = Callable[[Sequence[str], float, int], ProbeResult]


def _default_runner(
    argv: Sequence[str], timeout_seconds: float, max_output_bytes: int
) -> ProbeResult:
    """Run one bounded argv-only probe without a shell."""

    try:
        completed = subprocess.run(
            list(argv), capture_output=True, timeout=timeout_seconds, check=False, shell=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ProbeResult(
            returncode=-1,
            stdout="",
            stderr="timeout" if isinstance(exc, subprocess.TimeoutExpired) else "unavailable",
            timed_out=isinstance(exc, subprocess.TimeoutExpired),
        )

    stdout_bytes = (
        completed.stdout if isinstance(completed.stdout, bytes) else str(completed.stdout).encode()
    )
    stderr_bytes = (
        completed.stderr if isinstance(completed.stderr, bytes) else str(completed.stderr).encode()
    )
    combined = stdout_bytes + stderr_bytes
    truncated = len(combined) > max_output_bytes
    if truncated:
        combined = combined[:max_output_bytes]
        stdout_bytes = combined
        stderr_bytes = b""
    return ProbeResult(
        returncode=completed.returncode,
        stdout=stdout_bytes.decode("utf-8", errors="replace"),
        stderr=stderr_bytes.decode("utf-8", errors="replace"),
        output_truncated=truncated,
    )


def _version(output: str) -> str | None:
    match = _VERSION_PATTERN.search(output)
    return f"{match.group(1)}" if match else None


def _command_names(output: str) -> tuple[str, ...]:
    """Extract conservative command names from a capability response."""

    try:
        decoded = json.loads(output)
    except json.JSONDecodeError:
        decoded = None
    if isinstance(decoded, dict) and isinstance(decoded.get("commands"), list):
        values = decoded["commands"]
    else:
        values = re.findall(r"(?m)^\s{2,}([a-z][a-z0-9_-]*)\b", output)
    return tuple(
        sorted(
            {
                item
                for item in values
                if isinstance(item, str) and re.fullmatch(r"[a-z][a-z0-9_-]*", item)
            }
        )
    )


@dataclass
class RuVectorAdapter:
    """Negotiate a RuVector executable without granting it policy authority."""

    executable: str = "ruvector"
    timeout_ms: int = DEFAULT_TIMEOUT_MS
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    runner: Runner = _default_runner
    _last_readiness: RuVectorReadiness | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if (
            not self.executable.strip()
            or any(char in self.executable for char in "\r\n\x00")
            or any(char in self.executable for char in " ;|&><`$()")
        ):
            raise ValueError("executable must be a safe command name")
        if self.timeout_ms <= 0 or self.max_output_bytes <= 0:
            raise ValueError("probe bounds must be positive")

    def negotiate(self, required_commands: Sequence[str] = ()) -> RuVectorReadiness:
        """Probe version and capabilities, degrading on any incomplete signal."""

        required = tuple(sorted(set(required_commands)))
        version_probe = self.runner(
            (self.executable, "--version"), self.timeout_ms / 1000, self.max_output_bytes
        )
        version = _version(version_probe.stdout + "\n" + version_probe.stderr)
        limitations: list[str] = []
        if version_probe.timed_out:
            limitations.append("version probe timed out")
        elif version_probe.output_truncated:
            limitations.append("version probe output truncated")
        elif version_probe.returncode != 0:
            limitations.append("version probe failed")
        elif version is None:
            limitations.append("version output was not recognized")

        capability_probe = self.runner(
            (self.executable, "capabilities", "--json"),
            self.timeout_ms / 1000,
            self.max_output_bytes,
        )
        if capability_probe.timed_out:
            limitations.append("capability probe timed out")
        elif capability_probe.output_truncated:
            limitations.append("capability probe output truncated")
        elif capability_probe.returncode != 0:
            limitations.append("capability negotiation unsupported")
        supported = _command_names(capability_probe.stdout)
        missing = tuple(command for command in required if command not in supported)
        limitations.extend(f"unsupported command: {command}" for command in missing)

        if limitations and version is None:
            status = ReadinessStatus.UNAVAILABLE
        elif limitations:
            status = ReadinessStatus.DEGRADED
        else:
            status = ReadinessStatus.READY
        report = RuVectorReadiness(
            status=status,
            executable=self.executable,
            version=version,
            supported_commands=supported,
            required_commands=required,
            limitations=tuple(sorted(set(limitations))),
        )
        self._last_readiness = report
        return report

    def can_use(self, command: str, readiness: RuVectorReadiness | None = None) -> bool:
        """Allow only a negotiated command from a ready report."""

        report = readiness or self._last_readiness
        return bool(
            report and report.advisory_retrieval_enabled and command in report.supported_commands
        )


__all__ = [
    "ADAPTER_PROTOCOL_VERSION",
    "DEFAULT_MAX_OUTPUT_BYTES",
    "DEFAULT_TIMEOUT_MS",
    "ProbeResult",
    "ReadinessStatus",
    "RuVectorAdapter",
    "RuVectorReadiness",
]
