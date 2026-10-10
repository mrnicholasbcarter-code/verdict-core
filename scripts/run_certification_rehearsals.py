#!/usr/bin/env python3
"""Run the two live CI fixtures; never print provider output or credentials."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from verdict.orchestration.connections_snapshot import read_connections_snapshot
from verdict.orchestration.contracts import OrchestrationError
from verdict.orchestration.receipt import verify_run_receipt

GOALS = {
    "clean": (
        "Add textkit/slug.py with slugify(text) and textkit/words.py with word_count(text); "
        "each with its own pytest file; the full suite must pass. PARALLEL_WORK_UNITS 3"
    ),
    "chaos": (
        "Add textkit/stats.py with mean(values) and textkit/case.py with title_case(text); "
        "each with its own pytest file; the full suite must pass. PARALLEL_WORK_UNITS 3"
    ),
}


class RehearsalConfigurationError(ValueError):
    """Fixed, credential-free configuration messages that may be printed."""


def _connections_snapshot(root: Path) -> Path:
    raw = os.environ.get("VERDICT_CERT_CONNECTIONS", "")
    if not raw.strip():
        raise RehearsalConfigurationError("Missing protected VERDICT_CERT_CONNECTIONS snapshot")
    # Escape multiline JSON for the GitHub workflow command protocol.
    masked = raw.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::add-mask::{masked}", flush=True)
    fd, name = tempfile.mkstemp(prefix="verdict-connections-", suffix=".json", dir=root)
    path = Path(name)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
        read_connections_snapshot(path)
    except (OSError, OrchestrationError) as exc:
        path.unlink(missing_ok=True)
        raise RehearsalConfigurationError(
            "Invalid protected VERDICT_CERT_CONNECTIONS snapshot (requires sanitized evidence captured within 6 hours)"
        ) from exc
    return path


def run() -> None:
    root = Path(os.environ["RUNNER_TEMP"])
    snapshot_path = _connections_snapshot(root)
    evidence = root / "attested-rehearsals"
    evidence.mkdir(exist_ok=False)
    gateway = os.environ["VERDICT_CERT_GATEWAY_URL"]
    key = os.environ["VERDICT_OMNIROUTE_API_KEY"]
    if not gateway or not key:
        raise ValueError("Missing protected gateway configuration")
    env = os.environ.copy()
    env.pop("VERDICT_CERT_CONNECTIONS", None)
    env["VERDICT_CONNECTIONS_SNAPSHOT"] = str(snapshot_path)
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env["PATH"]
    for name, goal in GOALS.items():
        fixture = root / f"fixture-{name}"
        fixture.mkdir(exist_ok=False)
        (fixture / "textkit").mkdir()
        (fixture / "textkit" / "__init__.py").write_text("")
        (fixture / "tests").mkdir()
        (fixture / "tests" / "test_seed.py").write_text("def test_seed():\n    assert True\n")
        (fixture / "pytest.ini").write_text("[pytest]\npythonpath = .\n")
        (fixture / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n*.pyc\n")
        for command in (
            ["git", "init", "-q"],
            ["git", "config", "user.email", "certification@example.invalid"],
            ["git", "config", "user.name", "Certification Rehearsal"],
            ["git", "add", "."],
            ["git", "commit", "-qm", "Seed isolated rehearsal fixture"],
        ):
            subprocess.run(
                command, cwd=fixture, env=env, check=True, capture_output=True, timeout=30
            )
        runs = root / f"runs-{name}"
        command = [
            sys.executable,
            "-m",
            "verdict",
            "orchestrate",
            goal,
            "--repo",
            str(fixture),
            "--runs-dir",
            str(runs),
            "--gateway",
            gateway,
            "--executor",
            "direct-gateway",
            "--capacity",
            "free,subscription",
            "--max-attempts-per-node",
            "4",
            "--max-parallel",
            "3",
            "--attempt-timeout",
            "300",
            "--run-deadline",
            "1500",
            "--json",
        ]
        if name == "chaos":
            command.extend(
                [
                    "--inject",
                    "#1=quota",
                    "--inject",
                    "#2=no_final",
                    "--inject",
                    "worker#1=rate_limit",
                ]
            )
        result = subprocess.run(command, env=env, capture_output=True, timeout=1800)
        if result.returncode:
            raise ValueError(f"{name} rehearsal did not complete (exit {result.returncode})")
        children = list(runs.iterdir())
        if len(children) != 1 or not children[0].is_dir():
            raise ValueError("Expected one isolated run")
        run_dir = children[0]
        if verify_run_receipt(run_dir):
            raise ValueError(f"Invalid {name} receipt semantics")
        receipt = json.loads((run_dir / "receipt.json").read_text())
        if receipt.get("outcome") != "COMPLETE" or receipt.get("claimed_outcome") != "COMPLETE":
            raise ValueError(f"Incomplete {name} receipt")
        if receipt.get("producer", {}).get("git_sha") != os.environ["GITHUB_SHA"]:
            raise ValueError("Producer SHA differs from exact checkout")
        attempts = [a for n in receipt.get("nodes", []) for a in n.get("attempts", [])]
        failures = [
            a for a in attempts if a.get("fault_injected") is True and a.get("outcome") == "failure"
        ]
        if name == "chaos" and not failures:
            raise ValueError("Chaos must contain a controlled failure")
        if name == "clean" and any(a.get("fault_injected") is True for a in attempts):
            raise ValueError("Clean run contains injected faults")
        for file in run_dir.rglob("*"):
            if file.is_symlink():
                raise ValueError("Run evidence must not contain symlinks")
            if file.is_file() and any(
                secret.encode() in file.read_bytes() for secret in (gateway, key)
            ):
                raise ValueError("Run evidence contains protected configuration; refusing upload")
        shutil.copytree(run_dir, evidence / name)


if __name__ == "__main__":
    try:
        run()
    except RehearsalConfigurationError as exc:
        sys.exit(str(exc))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        # Exception text can include provider output, argv or a credentialed URL.
        sys.exit(f"Rehearsal production failed closed ({type(exc).__name__})")
