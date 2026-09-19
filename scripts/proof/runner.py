"""Deterministic proof runner for BOD-89 (local + CI, same DAG)."""

from __future__ import annotations

import os
import re
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from scripts.proof.contract import GateSpec, ProofContract

LOG_SLICE_CHARS = 4000
_PYTEST_NODE = re.compile(r"(?P<file>[\w./\\-]+\.py)::(?P<test>[\w\[\]-]+)\s+(?:FAILED|ERROR)")


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str


Executor = Callable[[list[str], dict[str, str], Path], CommandResult]


@dataclass
class GateResult:
    id: str
    phase: str
    classify: str
    command: list[str]
    exit_code: int
    status: str
    duration_ms: int
    log_slice: str
    file: str | None = None
    test: str | None = None
    required: bool = True

    def to_failure(self) -> dict[str, Any]:
        return {
            "gate": self.id,
            "command": list(self.command),
            "file": self.file,
            "test": self.test,
            "exit_code": self.exit_code,
            "log_slice": self.log_slice,
            "classify": self.classify,
            "status": self.status,
            "required": self.required,
        }


@dataclass
class ProofReport:
    schema_version: int
    ok: bool
    mode: str
    cache_mode: str
    runner: str
    contract_name: str
    head_sha: str | None
    gates: list[GateResult] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    otel: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "ok": self.ok,
            "mode": self.mode,
            "cache_mode": self.cache_mode,
            "runner": self.runner,
            "contract_name": self.contract_name,
            "head_sha": self.head_sha,
            "gates": [asdict(gate) for gate in self.gates],
            "failures": list(self.failures),
            "otel": self.otel,
        }


def classify_failure(gate_id: str, _log: str) -> str:
    """Map a gate id to a stable failure class for CI-fixer hydration."""
    mapping = {
        "format": "format",
        "lint": "lint",
        "type": "type",
        "schema": "schema",
        "unit_targeted": "test",
        "unit_full": "test",
        "security_semgrep": "security",
        "security_bandit": "security",
        "secrets": "secret",
        "build": "build",
        "acceptance": "acceptance",
        "ast_grep": "structural",
        "structural_risk": "structural",
        "promptfoo": "adversarial",
    }
    return mapping.get(gate_id, "unknown")


def parse_pytest_failure(log: str) -> tuple[str | None, str | None]:
    match = _PYTEST_NODE.search(log)
    if not match:
        return None, None
    return match.group("file"), match.group("test")


def default_executor(argv: list[str], env: dict[str, str], cwd: Path) -> CommandResult:
    try:
        completed = subprocess.run(
            argv, cwd=cwd, env=env, capture_output=True, text=True, check=False
        )
    except FileNotFoundError:
        raise
    return CommandResult(
        exit_code=int(completed.returncode),
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )


def _slice_log(stdout: str, stderr: str) -> str:
    combined = stderr.strip() + ("\n" if stderr.strip() and stdout.strip() else "") + stdout.strip()
    if len(combined) <= LOG_SLICE_CHARS:
        return combined
    return combined[-LOG_SLICE_CHARS:]


def _resolve_command(gate: GateSpec, targets: Sequence[str]) -> list[str]:
    command = list(gate.command)
    if gate.targets_placeholder:
        rendered: list[str] = []
        for part in command:
            if part == gate.targets_placeholder:
                rendered.extend(targets if targets else ["tests/"])
            else:
                rendered.append(part)
        return rendered
    return command


def _git_head(root: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=False
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


class ProofRunner:
    """Execute the proof contract DAG with raw exit status preservation."""

    def __init__(
        self,
        contract: ProofContract,
        *,
        root: Path,
        executor: Callable[..., CommandResult] | None = None,
    ) -> None:
        self.contract = contract
        self.root = root
        self._executor = executor or self._adapt_default

    def _adapt_default(self, argv: list[str], *, env: dict[str, str], cwd: Path) -> CommandResult:
        return default_executor(argv, env, cwd)

    def run(
        self,
        *,
        mode: str = "targeted",
        targets: Sequence[str] | None = None,
        no_cache: bool = False,
        llm_boundary: bool = False,
        otel: bool = False,
    ) -> ProofReport:
        target_list = list(targets or [])
        selected = self.contract.gates_for_mode(mode, llm_boundary=llm_boundary)
        # Also record skipped llm_boundary gates for machine-readable transparency
        all_phase_gates = self.contract.gates_for_mode(mode, llm_boundary=True)
        skipped_ids = {g.id for g in all_phase_gates} - {g.id for g in selected}

        report = ProofReport(
            schema_version=1,
            ok=True,
            mode=mode,
            cache_mode="no-cache" if no_cache else "cache",
            runner=self.contract.runner,
            contract_name=self.contract.name,
            head_sha=_git_head(self.root),
        )
        spans: list[dict[str, Any]] = []
        base_env = os.environ.copy()
        base_env["PROOF_SECRETS_LOCAL_ONLY"] = "1"
        base_env.pop("PROOF_UPLOAD_SECRETS", None)
        if no_cache:
            base_env["PROOF_NO_CACHE"] = "1"
            base_env["PYTHONDONTWRITEBYTECODE"] = "1"
            base_env["MYPY_CACHE_DIR"] = str(self.root / ".proof-mypy-nocache")
            base_env["RUFF_CACHE_DIR"] = str(self.root / ".proof-ruff-nocache")

        for gate in selected:
            result = self._run_gate(gate, targets=target_list, env=base_env)
            report.gates.append(result)
            if otel:
                spans.append(
                    {
                        "name": f"proof.gate.{gate.id}",
                        "attributes": {
                            "proof.gate": gate.id,
                            "proof.phase": gate.phase,
                            "proof.classify": gate.classify,
                            "proof.status": result.status,
                            "proof.exit_code": result.exit_code,
                            "proof.cache_mode": report.cache_mode,
                        },
                        "status": "OK" if result.status == "pass" else "ERROR",
                        "duration_ms": result.duration_ms,
                    }
                )
            if result.status in {"fail", "blocked"}:
                if result.required:
                    report.ok = False
                report.failures.append(result.to_failure())
                continue

        for gate_id in sorted(skipped_ids):
            gate = next(g for g in self.contract.gates if g.id == gate_id)
            report.gates.append(
                GateResult(
                    id=gate.id,
                    phase=gate.phase,
                    classify=gate.classify,
                    command=list(gate.command),
                    exit_code=0,
                    status="skip",
                    duration_ms=0,
                    log_slice="skipped: llm_boundary not enabled",
                    required=gate.required,
                )
            )

        if otel:
            report.otel = {
                "resource": {
                    "service.name": "verdict-proof",
                    "proof.contract": self.contract.name,
                    "proof.mode": mode,
                },
                "spans": spans,
            }
        return report

    def _run_gate(
        self, gate: GateSpec, *, targets: Sequence[str], env: dict[str, str]
    ) -> GateResult:
        command = _resolve_command(gate, targets)
        started = time.perf_counter()
        try:
            completed = self._executor(command, env=dict(env), cwd=self.root)
        except FileNotFoundError:
            duration_ms = int((time.perf_counter() - started) * 1000)
            status = "blocked" if gate.required else "skip"
            return GateResult(
                id=gate.id,
                phase=gate.phase,
                classify=gate.classify,
                command=command,
                exit_code=127,
                status=status,
                duration_ms=duration_ms,
                log_slice=f"required binary missing: {command[0]}"
                if gate.required
                else f"optional binary missing: {command[0]}",
                required=gate.required,
            )
        duration_ms = int((time.perf_counter() - started) * 1000)
        log_slice = _slice_log(completed.stdout, completed.stderr)
        file_name: str | None = None
        test_name: str | None = None
        if gate.classify == "test":
            file_name, test_name = parse_pytest_failure(log_slice)
        status = "pass" if completed.exit_code == 0 else "fail"
        return GateResult(
            id=gate.id,
            phase=gate.phase,
            classify=gate.classify,
            command=command,
            exit_code=completed.exit_code,
            status=status,
            duration_ms=duration_ms,
            log_slice=log_slice,
            file=file_name,
            test=test_name,
            required=gate.required,
        )
