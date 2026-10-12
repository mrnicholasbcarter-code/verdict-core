"""Prime launch compatibility uses its offline CLI, not shipped documentation layout."""

from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess
import pytest
from verdict.orchestration.executors import PrimeHeadlessExecutor
from verdict.orchestration.recovery import FailureIntelligence
from verdict.orchestration.contracts import WorkerTerminal


def test_real_default_worker_argv_is_accepted_without_model_call(tmp_path: Path) -> None:
    binary = shutil.which("prime-agent")
    if binary is None:
        pytest.skip("prime-agent is not installed")
    executor = PrimeHeadlessExecutor(prime_bin=binary)
    command = executor._command("fixture", "cc/fixture", tmp_path)
    separator = command.index("--")
    result = subprocess.run(
        command[:separator] + ["--offline", "--help"], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr


def test_unknown_launch_option_is_local_block_without_route_cooldown() -> None:
    terminal = PrimeHeadlessExecutor()._interpret(
        stdout="",
        stderr="Error: Unknown option: -ne",
        returncode=1,
        route_id="cc/fixture",
        duration=0.1,
    )
    failure = FailureIntelligence().classify(terminal, now=datetime.now(timezone.utc))
    assert failure.category == "harness_configuration"
    assert failure.action == "BLOCK"
    assert failure.scope == "none"
    assert failure.cooldown_seconds == 0


def test_retry_policy_configuration_is_local_block() -> None:
    failure = FailureIntelligence().classify(
        WorkerTerminal(
            ok=False, error="harness_configuration: prime retry policy not one-shot: fixture"
        ),
        now=datetime.now(timezone.utc),
    )
    assert failure.action == "BLOCK"
    assert failure.scope == "none"
    assert failure.cooldown_seconds == 0
