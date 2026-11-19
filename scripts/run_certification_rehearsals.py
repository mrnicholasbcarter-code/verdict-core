#!/usr/bin/env python3
"""Run the two live CI fixtures; never print provider output or credentials."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from verdict.orchestration.connections_snapshot import read_connections_snapshot
from verdict.orchestration.contracts import (
    PLANNER_MAX_CAPABILITY_TIER,
    PLANNER_MIN_CONTEXT_TOKENS,
    PLANNER_REQUIRED_CAPABILITIES,
    OrchestrationError,
)
from verdict.orchestration.receipt import verify_run_receipt
from verdict.orchestration.runtime import REDACT_GATEWAY_ORIGIN_ENV

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


# Bounded preflight configuration: strictly FREE, small explicit bounds.
PREFLIGHT_MAX_PER_PROVIDER = 2
# 8, not 4: with 4 the local empty-home proof (2026-11-19) planned but then
# exhausted the worker pool on 3 proven routes.
PREFLIGHT_MAX_ROUTES = 8
PREFLIGHT_WALL_SECONDS = 600  # 10 minutes

# Fixed, credential-free substrings of run_finished reasons, first match wins.
# The artifact only ever carries the enum, never the reason text.
_REASON_CLASSES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("planning_none_eligible", ("planning failed on every eligible frontier model",)),
    ("gateway_auth", ("failed (HTTP 401)", "failed (HTTP 403)")),
    ("gateway_discovery", ("gateway /",)),
    ("operator_policy", ("operator policy",)),
    ("harness_inventory_unavailable", ("harness_inventory_unavailable",)),
)
_METADATA_ERRORS = frozenset({"metadata_unavailable"})


def classify_run_finished(data: Any) -> str:
    """Map a run_finished payload to a closed enum; raw text never leaves here."""
    if not isinstance(data, dict):
        return "unclassified"
    reason = data.get("reason")
    if isinstance(reason, str):
        for name, needles in _REASON_CLASSES:
            if any(needle in reason for needle in needles):
                return name
    return "startup" if data.get("phase") == "startup" else "unclassified"


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _planner_eligible(row: Any) -> bool:
    """A screen PASS the frontier planner can admit (same floor as plan_with_failover)."""
    if not isinstance(row, dict) or row.get("result") != "pass":
        return False
    tier, context = row.get("capability_tier"), row.get("context_window")
    tools_ok = "tools" not in PLANNER_REQUIRED_CAPABILITIES or row.get("supports_tools") is True
    return (
        _count(tier) is not None
        and tier <= PLANNER_MAX_CAPABILITY_TIER
        and tools_ok
        and _count(context) is not None
        and context >= PLANNER_MIN_CONTEXT_TOKENS
    )


def preflight_summary(report: Any) -> dict[str, Any]:
    """Counts and enums only, safe to upload."""
    report = report if isinstance(report, dict) else {}
    routes = report.get("routes")
    rows = routes if isinstance(routes, list) else []
    metadata_error = report.get("metadata_error")
    return {
        "routes_checked": _count(report.get("routes_checked")),
        "passed": _count(report.get("passed")),
        "planner_eligible_passed": sum(_planner_eligible(row) for row in rows),
        "metadata_error": (
            None
            if metadata_error is None
            else metadata_error
            if metadata_error in _METADATA_ERRORS
            else "unclassified"
        ),
    }


def _write_failure(root: Path, report: dict[str, Any]) -> None:
    (root / "certification-failure.json").write_text(json.dumps(report))


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


def _run_preflight(root: Path, env: dict[str, str]) -> dict[str, Any]:
    """Run a bounded FREE-only identify screen to produce fresh agentic proof.

    Returns the safe preflight summary on success. Raises
    RehearsalConfigurationError when no pass is one the planner can admit
    (fail-closed): a tier-3 pass satisfies nothing the planner needs.
    """
    verdict_home = env.get("VERDICT_HOME")
    if not verdict_home:
        raise RehearsalConfigurationError("VERDICT_HOME not set for preflight")

    cmd = [
        sys.executable,
        "-m",
        "verdict",
        "identify",
        "--screen",
        "--allow-live-probe",
        "--capacity",
        "free",
        "--max-per-provider",
        str(PREFLIGHT_MAX_PER_PROVIDER),
        "--max-routes",
        str(PREFLIGHT_MAX_ROUTES),
        "--max-capability-tier",
        str(PLANNER_MAX_CAPABILITY_TIER),
        "--json",
    ]

    # Use the same VERDICT_HOME and env as orchestrate. `identify` has no
    # --gateway flag and otherwise defaults to loopback, which a CI runner
    # does not have; point it at the certification gateway orchestrate uses.
    preflight_env = env.copy()
    preflight_env["VERDICT_HOME"] = verdict_home
    preflight_env["OMNIROUTE_BASE_URL"] = env["VERDICT_CERT_GATEWAY_URL"]

    result = subprocess.run(
        cmd,
        env=preflight_env,
        capture_output=True,
        timeout=PREFLIGHT_WALL_SECONDS + 30,  # allow some overhead
    )

    # These messages are printed verbatim by __main__; child output can carry
    # provider text, the key or the gateway URL, so it is never included.
    if result.returncode != 0:
        raise RehearsalConfigurationError(
            f"Preflight identify screen failed (exit {result.returncode})"
        )

    try:
        report = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RehearsalConfigurationError("Preflight screen produced invalid JSON") from exc
    if not isinstance(report, dict):
        raise RehearsalConfigurationError("Preflight screen produced invalid JSON")

    summary = preflight_summary(report)
    if not summary["routes_checked"] or not summary["passed"]:
        _write_failure(root, {"stage": "preflight", "preflight": summary})
        raise RehearsalConfigurationError(
            "Preflight screen produced no qualified FREE routes: "
            f"routes_checked={summary['routes_checked']}, passed={summary['passed']}. "
            "Rehearsal cannot proceed without fresh agentic proof."
        )
    if not summary["planner_eligible_passed"]:
        _write_failure(root, {"stage": "preflight", "preflight": summary})
        raise RehearsalConfigurationError(
            "Preflight screen produced no planner-eligible FREE routes "
            f"(tier<={PLANNER_MAX_CAPABILITY_TIER}, tools, "
            f"{PLANNER_MIN_CONTEXT_TOKENS // 1000}k context): "
            f"routes_checked={summary['routes_checked']}, passed={summary['passed']}."
        )
    return summary


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
    # Set VERDICT_HOME for the preflight and orchestrate to share the health cache
    verdict_home = root / "verdict-certification"
    env["VERDICT_HOME"] = str(verdict_home)
    # The gateway address is a protected secret: the run must not write it into
    # its evidence. The scan below still refuses any file that contains it.
    env[REDACT_GATEWAY_ORIGIN_ENV] = "1"

    # Bounded FREE-only preflight: produce genuine fresh agentic proof
    # before attempting orchestration. Fail-closed if no route qualifies.
    preflight = _run_preflight(root, env)

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
            "free",
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
            run_finished_outcome = None
            run_finished_reason = "unclassified"
            if runs.exists():
                children = list(runs.iterdir())
                if children and children[0].is_dir():
                    run_dir = children[0]
                    events_file = run_dir / "events.jsonl"
                    if events_file.exists():
                        for line in events_file.read_text().splitlines():
                            try:
                                event = json.loads(line.strip())
                                if event.get("type") == "run_finished":
                                    outcome = event.get("data", {}).get("outcome")
                                    run_finished_outcome = (
                                        outcome
                                        if isinstance(outcome, str)
                                        and outcome in {"COMPLETE", "BLOCKED", "CANCELLED"}
                                        else None
                                    )
                                    run_finished_reason = classify_run_finished(event.get("data"))
                            except Exception:
                                pass
            report = {
                "rehearsal": name,
                "stage": "orchestration",
                "error_category": "child_exit",
                "run_finished_outcome": run_finished_outcome,
                "run_finished_reason": run_finished_reason,
                "preflight": preflight,
            }
            _write_failure(root, report)
            raise ValueError(f"{name} rehearsal orchestration failed (exit {result.returncode})")
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
