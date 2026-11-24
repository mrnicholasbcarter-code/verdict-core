"""Deterministic proof runner for BOD-89 (local + CI, same DAG)."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from scripts.proof.contract import GateSpec, ProofContract

LOG_SLICE_CHARS = 4000
SPLIT_MARKER = "not timing"
SPLIT_ALGORITHM = "least_duration"
# The collector lives beside the runner, not under the repository being proven.
COLLECTOR_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ci_shard_union.py"
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
    shard: dict[str, int] | None = None
    targets: list[str] = field(default_factory=list)

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
            "shard": dict(self.shard) if self.shard is not None else None,
            "targets": list(self.targets),
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
        "unit_timing": "test",
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


class ShardError(ValueError):
    """Raised when a leg index cannot be used with the contract or the mode."""


@dataclass(frozen=True)
class ShardPosition:
    """One leg of a sharded full proof: ``index`` of ``count``, both starting at 1."""

    index: int
    count: int

    @property
    def directory(self) -> str:
        return f"proof-shard-{self.index}"

    def as_dict(self) -> dict[str, int]:
        return {"index": self.index, "count": self.count}


def resolve_shard(contract: ProofContract, mode: str, index: int | None) -> ShardPosition | None:
    """Validate a leg index against the contract; ``None`` means an unsharded run."""
    if index is None:
        return None
    if mode != "full":
        raise ShardError(f"--shard is only valid in full mode; got mode {mode!r}")
    count = contract.shards
    if count is None:
        raise ShardError("the contract declares no modes.full.shards, so --shard cannot be used")
    if isinstance(index, bool) or not 1 <= index <= count:
        raise ShardError(f"--shard must be between 1 and {count}; got {index}")
    return ShardPosition(index=index, count=count)


def selection_args(gate: GateSpec, position: ShardPosition | None) -> list[str]:
    """Arguments sharding appends after a gate's own command (none when unsharded).

    The runner and the aggregate both call this, so they cannot disagree about
    what a leg was asked to run.
    """
    if position is None or gate.shard is None:
        return []
    junit = f"--junitxml={position.directory}/junit-{gate.id}.xml"
    if gate.shard == "split":
        return [
            "-m",
            SPLIT_MARKER,
            "--splits",
            str(position.count),
            "--group",
            str(position.index),
            "--splitting-algorithm",
            SPLIT_ALGORITHM,
            junit,
        ]
    return [junit] if position.index == 1 else []


def selection_collect_args(command: Sequence[str]) -> list[str]:
    """The pytest arguments that select tests: the command without the program and report."""
    return [part for part in command[1:] if not part.startswith("--junitxml=")]


@dataclass(frozen=True)
class PlannedGate:
    """What one gate does in one run: the command it records, and whether it runs."""

    gate: GateSpec
    command: tuple[str, ...]
    skip_reason: str | None = None


def plan_gates(
    contract: ProofContract,
    *,
    mode: str,
    targets: Sequence[str],
    shard: ShardPosition | None,
    llm_boundary: bool = False,
) -> list[PlannedGate]:
    """Decide, for every gate of a mode, its command and whether it runs.

    One function decides this for the runner and for the aggregate, so a skip is
    authorized by the contract only where this function says so. Gates are listed
    in the order the runner reports them: those that run (or are skipped in
    place) in contract order, then the ``llm_boundary`` gates that are not enabled.
    """
    selected = contract.gates_for_mode(mode, llm_boundary=llm_boundary)
    all_phase_gates = contract.gates_for_mode(mode, llm_boundary=True)
    skipped_ids = {g.id for g in all_phase_gates} - {g.id for g in selected}
    # Without --targets the targeted gate would rerun the exact full-suite
    # command; in a mode that also runs unit_full, record it as skipped.
    runs_full_suite = any(g.phase == "unit_full" for g in selected)
    planned: list[PlannedGate] = []
    for gate in selected:
        base = _resolve_command(gate, targets)
        if gate.targets_placeholder and not targets and runs_full_suite:
            planned.append(
                PlannedGate(
                    gate, tuple(base), "skipped: no --targets; unit_full runs the same suite"
                )
            )
        elif gate.shard == "leader" and (shard is None or shard.index != 1):
            reason = (
                f"skipped: unsharded run, so unit_full runs the tests of {gate.id}"
                if shard is None
                else f"skipped: only leg 1 runs {gate.id}"
            )
            planned.append(PlannedGate(gate, tuple(base), reason))
        else:
            planned.append(PlannedGate(gate, tuple([*base, *selection_args(gate, shard)])))
    for gate_id in sorted(skipped_ids):
        gate = next(g for g in contract.gates if g.id == gate_id)
        planned.append(PlannedGate(gate, tuple(gate.command), "skipped: llm_boundary not enabled"))
    return planned


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
        shard: int | None = None,
    ) -> ProofReport:
        target_list = list(targets or [])
        position = resolve_shard(self.contract, mode, shard)
        planned = plan_gates(
            self.contract, mode=mode, targets=target_list, shard=position, llm_boundary=llm_boundary
        )

        report = ProofReport(
            schema_version=1,
            ok=True,
            mode=mode,
            cache_mode="no-cache" if no_cache else "cache",
            runner=self.contract.runner,
            contract_name=self.contract.name,
            head_sha=_git_head(self.root),
            shard=position.as_dict() if position is not None else None,
            targets=list(target_list),
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

        for item in planned:
            gate = item.gate
            if item.skip_reason is not None:
                report.gates.append(
                    GateResult(
                        id=gate.id,
                        phase=gate.phase,
                        classify=gate.classify,
                        command=list(item.command),
                        exit_code=0,
                        status="skip",
                        duration_ms=0,
                        log_slice=item.skip_reason,
                        required=gate.required,
                    )
                )
                continue
            result = self._run_gate(item, position=position, env=base_env)
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

    def _record_selection(
        self, item: PlannedGate, position: ShardPosition, env: dict[str, str]
    ) -> GateResult | None:
        """Write the test ids a sharded gate will select; return a failure if that fails.

        The collector runs outside the gate, with ``PYTEST_ADDOPTS`` cleared, so the
        recorded selection is the one the gate's own arguments make and nothing a
        workflow option adds. If it cannot be recorded the gate does not run.
        """
        gate = item.gate
        ids_file = self.root / position.directory / f"ids-{gate.id}.json"
        ids_file.parent.mkdir(parents=True, exist_ok=True)
        ids_file.unlink(missing_ok=True)
        argv = [
            sys.executable,
            str(COLLECTOR_PATH),
            "--emit-ids",
            str(ids_file),
            "--",
            *selection_collect_args(item.command),
        ]
        collector_env = {k: v for k, v in env.items() if k != "PYTEST_ADDOPTS"}
        started = time.perf_counter()
        problem: str | None = None
        exit_code = 1
        log = ""
        try:
            completed = self._executor(argv, env=collector_env, cwd=self.root)
        except OSError as exc:
            problem = f"selection collector could not start: {exc}"
        else:
            exit_code = completed.exit_code
            log = _slice_log(completed.stdout, completed.stderr)
            if completed.exit_code != 0:
                problem = f"selection collector exited {completed.exit_code}"
            elif not ids_file.is_file():
                problem = "selection collector wrote no ids file"
        if problem is None:
            return None
        return GateResult(
            id=gate.id,
            phase=gate.phase,
            classify=gate.classify,
            command=list(item.command),
            exit_code=exit_code or 1,
            status="fail",
            duration_ms=int((time.perf_counter() - started) * 1000),
            log_slice=f"{problem}; {gate.id} did not run\n{log}".strip()[-LOG_SLICE_CHARS:],
            required=gate.required,
        )

    def _run_gate(
        self, item: PlannedGate, *, position: ShardPosition | None, env: dict[str, str]
    ) -> GateResult:
        gate = item.gate
        command = list(item.command)
        if position is not None and gate.shard is not None:
            failed = self._record_selection(item, position, env)
            if failed is not None:
                return failed
        gate_env = {k: v for k, v in env.items() if k not in gate.env_clear}
        started = time.perf_counter()
        try:
            completed = self._executor(command, env=gate_env, cwd=self.root)
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
