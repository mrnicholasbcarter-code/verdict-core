#!/usr/bin/env python3
"""Generate immutable, SHA-bound certification bundles for verdict-core releases.

This script produces a complete certification bundle under artifacts/certification/<sha>/
containing machine-readable evidence and a generated CERTIFICATION.md summary.

BOD-195: Turn release certification from hand-edited documents into an executable,
repeatable product feature bound to one exact source SHA.
"""

import argparse
import hashlib
import html
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

# === Data Models ===


@dataclass
class StepResult:
    """Result of a single certification step."""

    step_id: str
    name: str
    status: Literal["PASS", "FAIL", "SKIPPED", "INCOMPLETE"]
    reason: str = ""  # Details for FAIL or SKIPPED
    duration_seconds: float = 0.0
    evidence: dict[str, Any] = field(default_factory=dict)
    _attestation_token: object | None = field(default=None, init=False, repr=False)


@dataclass
class CertificationManifest:
    """Top-level certification bundle manifest."""

    schema_version: str = "1"
    git_sha: str = ""
    git_dirty: bool = False
    started_at: str = ""
    finished_at: str = ""
    steps: list[StepResult] = field(default_factory=list)
    verdict: Literal["CERTIFIED", "INCOMPLETE", "FAILED"] = "INCOMPLETE"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "git_sha": self.git_sha,
            "git_dirty": self.git_dirty,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "steps": [
                {
                    "step_id": s.step_id,
                    "name": s.name,
                    "status": s.status,
                    "reason": s.reason,
                    "duration_seconds": s.duration_seconds,
                    "evidence": s.evidence,
                }
                for s in self.steps
            ],
            "verdict": self.verdict,
        }


@dataclass
class EnvironmentSnapshot:
    """Captured environment at certification time."""

    python_version: str
    platform: str
    uv_version: str
    node_version: str = ""  # Optional
    lockfile_sha256: str = ""
    env_var_names: list[str] = field(default_factory=list)  # Sorted, normalized

    def to_dict(self) -> dict[str, Any]:
        return {
            "python_version": self.python_version,
            "platform": self.platform,
            "uv_version": self.uv_version,
            "node_version": self.node_version,
            "lockfile_sha256": self.lockfile_sha256,
            "env_var_names": self.env_var_names,
        }


# === Utilities ===


def run_command(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 300,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Bound external work; represent failures as nonzero results with safe evidence."""
    try:
        return subprocess.run(
            cmd, cwd=cwd, env=env, capture_output=capture_output, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        result = subprocess.CompletedProcess(cmd, -1, "", "")
        result.command_error = {"timed_out": True, "timeout_seconds": timeout}  # type: ignore[attr-defined]
        return result
    except OSError as exc:
        result = subprocess.CompletedProcess(cmd, -1, "", "")
        result.command_error = {"error_class": type(exc).__name__}  # type: ignore[attr-defined]
        return result


def command_failure(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    """Expose bounded-command failures without leaking command paths or stderr."""
    failure = getattr(result, "command_error", None)
    return failure if isinstance(failure, dict) else {}


def command_failure_reason(result: subprocess.CompletedProcess[str]) -> str:
    failure = command_failure(result)
    if failure.get("timed_out"):
        return f"Timed out after {failure['timeout_seconds']} seconds"
    if failure.get("error_class"):
        return f"Command could not start: {failure['error_class']}"
    return f"Exit code {result.returncode}"


def sha256_file(path: Path) -> str:
    """Compute SHA256 of a file."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()


def utc_timestamp() -> str:
    """Return current UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()


def get_git_sha(repo_path: Path) -> str:
    """Get current git SHA."""
    result = run_command(["git", "rev-parse", "HEAD"], cwd=repo_path)
    if result.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", result.stdout.strip()):
        raise RuntimeError(f"Cannot read Git HEAD: {command_failure_reason(result)}")
    return result.stdout.strip()


def assert_git_sha(repo_path: Path, expected_sha: str) -> None:
    """Refuse to attribute evidence to a HEAD that changed during this run."""
    result = run_command(["git", "rev-parse", "HEAD"], cwd=repo_path)
    if result.returncode != 0 or not expected_sha or result.stdout.strip() != expected_sha:
        raise RuntimeError(
            "Git HEAD changed or cannot be read during certification: "
            + command_failure_reason(result)
        )


def is_git_dirty(repo_path: Path) -> bool:
    """Check if git working tree is dirty."""
    result = run_command(["git", "status", "--porcelain"], cwd=repo_path)
    if result.returncode != 0:
        raise RuntimeError(f"Cannot read Git status: {command_failure_reason(result)}")
    return bool(result.stdout.strip())


def capture_environment(repo_path: Path) -> EnvironmentSnapshot:
    """Capture current environment details."""
    # Python version
    py_version = platform.python_version()

    # Platform
    plat = platform.platform()

    # uv version (robust to missing tool)
    try:
        uv_result = run_command(["uv", "--version"])
        uv_version = uv_result.stdout.strip() if uv_result.returncode == 0 else "not available"
    except (FileNotFoundError, OSError):
        uv_version = "not available"

    # Node version (optional, robust to missing tool)
    try:
        node_result = run_command(["node", "--version"])
        node_version = node_result.stdout.strip() if node_result.returncode == 0 else ""
    except (FileNotFoundError, OSError):
        node_version = ""

    # Lockfile SHA256
    lockfile_path = repo_path / "uv.lock"
    lockfile_sha = sha256_file(lockfile_path) if lockfile_path.exists() else ""

    # Environment variable names (sorted, no values)
    env_names = sorted(os.environ.keys())

    return EnvironmentSnapshot(
        python_version=py_version,
        platform=plat,
        uv_version=uv_version,
        node_version=node_version,
        lockfile_sha256=lockfile_sha,
        env_var_names=env_names,
    )


# === Step Implementations ===


def _run_pytest(repo_path: Path, venv_bin: Path, *, dirty: bool) -> StepResult:
    """Record the command, exit code and parsed JUnit counts for one shell."""
    start = datetime.now(timezone.utc)
    # Keep both shells controlled: only the intentional bogus credential
    # differs. Arbitrary CI/user variables must not alter test selection.
    env = {
        "HOME": os.environ.get("HOME", ""),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PATH": str(venv_bin) + os.pathsep + os.environ.get("PATH", ""),
    }
    if dirty:
        env["LLMGATE_AUTH_TOKEN"] = "bogus-cert-dirty"
    name = "Test suite (dirty shell)" if dirty else "Test suite (clean shell)"
    step_id = "test_dirty" if dirty else "test_clean"
    with tempfile.TemporaryDirectory(prefix="cert-junit-") as tmpdir:
        xml_path = Path(tmpdir) / "junit.xml"
        command = [str(venv_bin / "python"), "-m", "pytest", f"--junitxml={xml_path}"]
        display_command = [
            "<checkout>/.venv/bin/python",
            "-m",
            "pytest",
            "--junitxml=<temporary-report>",
        ]
        result = run_command(command, cwd=repo_path, env=env, timeout=1800)
        evidence: dict[str, Any] = {
            "command": display_command,
            "exit_code": result.returncode,
            "junit": None,
            **command_failure(result),
        }
        if xml_path.exists():
            try:
                root = ET.parse(xml_path).getroot()
                suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
                if not suites:
                    raise ValueError("no testsuite in JUnit XML")
                totals = {
                    key: sum(int(suite.attrib[key]) for suite in suites)
                    for key in ("tests", "failures", "errors", "skipped")
                }
                totals["passed"] = totals["tests"] - sum(
                    totals[key] for key in ("failures", "errors", "skipped")
                )
                if totals["tests"] <= 0 or totals["passed"] < 0:
                    raise ValueError("invalid JUnit test counts")
                cases: dict[str, str] = {}
                for suite in suites:
                    for case in suite.iter("testcase"):
                        case_id = f"{case.attrib['classname']}::{case.attrib['name']}"
                        if case_id in cases:
                            raise ValueError("duplicate JUnit testcase ID")
                        outcomes = [
                            name
                            for name in ("failure", "error", "skipped")
                            if case.find(name) is not None
                        ]
                        if len(outcomes) > 1:
                            raise ValueError("conflicting JUnit testcase outcomes")
                        cases[case_id] = outcomes[0] if outcomes else "passed"
                if len(cases) != totals["tests"] or any(
                    sum(outcome == name for outcome in cases.values()) != totals[count]
                    for name, count in (
                        ("failure", "failures"),
                        ("error", "errors"),
                        ("skipped", "skipped"),
                        ("passed", "passed"),
                    )
                ):
                    raise ValueError("JUnit testcase outcomes disagree with totals")
                evidence["junit"] = {
                    **totals,
                    "sha256": sha256_file(xml_path),
                    "testcases": dict(sorted(cases.items())),
                }
            except (ET.ParseError, ValueError, KeyError, TypeError) as exc:
                evidence["junit_error"] = str(exc)
        else:
            evidence["junit_error"] = "JUnit XML not produced"

    junit = evidence["junit"]
    if result.returncode != 0 or (junit and (junit["failures"] or junit["errors"])):
        status: Literal["PASS", "FAIL", "SKIPPED", "INCOMPLETE"] = "FAIL"
        reason = f"pytest exit code {result.returncode}"
    elif junit is None:
        status = "INCOMPLETE"
        reason = evidence.get("junit_error", "JUnit evidence unavailable")
    else:
        status = "PASS"
        reason = f"{junit['passed']} passed, {junit['skipped']} skipped"
    return StepResult(
        step_id,
        name,
        status,
        reason,
        (datetime.now(timezone.utc) - start).total_seconds(),
        evidence,
    )


def step_test_clean_shell(repo_path: Path, venv_bin: Path) -> StepResult:
    return _run_pytest(repo_path, venv_bin, dirty=False)


def step_test_dirty_shell(repo_path: Path, venv_bin: Path) -> StepResult:
    return _run_pytest(repo_path, venv_bin, dirty=True)


def step_ruff_check(repo_path: Path, venv_bin: Path) -> StepResult:
    """Run ruff check."""
    start = datetime.now(timezone.utc)
    result = run_command([str(venv_bin / "ruff"), "check", "."], cwd=repo_path)
    duration = (datetime.now(timezone.utc) - start).total_seconds()
    evidence = {
        "command": ["<checkout>/.venv/bin/ruff", "check", "."],
        "exit_code": result.returncode,
        **command_failure(result),
    }

    if result.returncode == 0:
        return StepResult(
            step_id="ruff_check",
            name="Ruff lint",
            status="PASS",
            duration_seconds=duration,
            evidence=evidence,
        )
    else:
        return StepResult(
            step_id="ruff_check",
            name="Ruff lint",
            status="FAIL",
            reason=command_failure_reason(result),
            duration_seconds=duration,
            evidence=evidence,
        )


def step_ruff_format(repo_path: Path, venv_bin: Path) -> StepResult:
    """Run ruff format --check."""
    start = datetime.now(timezone.utc)
    result = run_command([str(venv_bin / "ruff"), "format", "--check", "."], cwd=repo_path)
    duration = (datetime.now(timezone.utc) - start).total_seconds()
    evidence = {
        "command": ["<checkout>/.venv/bin/ruff", "format", "--check", "."],
        "exit_code": result.returncode,
        **command_failure(result),
    }

    if result.returncode == 0:
        return StepResult(
            step_id="ruff_format",
            name="Ruff format check",
            status="PASS",
            duration_seconds=duration,
            evidence=evidence,
        )
    else:
        return StepResult(
            step_id="ruff_format",
            name="Ruff format check",
            status="FAIL",
            reason=command_failure_reason(result),
            duration_seconds=duration,
            evidence=evidence,
        )


def step_mypy(repo_path: Path, venv_bin: Path) -> StepResult:
    """Run mypy --strict."""
    start = datetime.now(timezone.utc)
    result = run_command([str(venv_bin / "mypy"), "--strict", "verdict"], cwd=repo_path)
    duration = (datetime.now(timezone.utc) - start).total_seconds()
    evidence = {
        "command": ["<checkout>/.venv/bin/mypy", "--strict", "verdict"],
        "exit_code": result.returncode,
        **command_failure(result),
    }

    # Count files checked from output (parse "Success: no issues found in N source files")
    import re

    match = re.search(r"Success: no issues found in (\d+) source files?", result.stdout)
    files_checked = int(match.group(1)) if match else None
    evidence["files_checked"] = files_checked

    if result.returncode == 0:
        return StepResult(
            step_id="mypy",
            name="Mypy strict type check",
            status="PASS",
            reason=f"Checked {files_checked} files" if files_checked is not None else "",
            duration_seconds=duration,
            evidence=evidence,
        )
    else:
        return StepResult(
            step_id="mypy",
            name="Mypy strict type check",
            status="FAIL",
            reason=command_failure_reason(result),
            duration_seconds=duration,
            evidence=evidence,
        )


def step_build(repo_path: Path) -> StepResult:
    """Build into an empty directory; copy only outputs from this invocation."""
    start = datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory(prefix="cert-build-") as tmpdir:
        command = ["uv", "build", "--out-dir", tmpdir]
        result = run_command(command, cwd=repo_path, timeout=600)
        artifacts = []
        if result.returncode == 0:
            for artifact in sorted(Path(tmpdir).iterdir()):
                if artifact.is_file():
                    dest = repo_path / "dist" / artifact.name
                    dest.parent.mkdir(exist_ok=True)
                    shutil.copy2(artifact, dest)
                    artifacts.append({"name": artifact.name, "sha256": sha256_file(dest)})
        evidence = {
            "command": ["uv", "build", "--out-dir", "<fresh-temporary-dir>"],
            "exit_code": result.returncode,
            **command_failure(result),
            "artifacts": artifacts,
        }
    if result.returncode != 0:
        status: Literal["PASS", "FAIL", "SKIPPED", "INCOMPLETE"] = "FAIL"
        reason = command_failure_reason(result)
    elif not artifacts or not any(a["name"].endswith(".whl") for a in artifacts):
        status, reason = "INCOMPLETE", "Build returned success without a wheel artifact"
    else:
        status, reason = "PASS", f"Built {len(artifacts)} artifact(s)"
    return StepResult(
        "build",
        "Package build",
        status,
        reason,
        (datetime.now(timezone.utc) - start).total_seconds(),
        evidence,
    )


def step_package_smoke(repo_path: Path, wheel_name: str | None = None) -> StepResult:
    """Install a wheel built during this run in a fresh venv."""
    start = datetime.now(timezone.utc)
    commands: list[dict[str, Any]] = []
    evidence: dict[str, Any] = {"wheel": wheel_name, "commands": commands, "help_has_usage": False}
    if wheel_name is None or Path(wheel_name).name != wheel_name:
        return StepResult(
            "package_smoke",
            "Package smoke test",
            "INCOMPLETE",
            "No wheel from this build",
            evidence=evidence,
        )
    wheel = repo_path / "dist" / wheel_name
    if not wheel.is_file():
        return StepResult(
            "package_smoke",
            "Package smoke test",
            "INCOMPLETE",
            "Built wheel is missing",
            evidence=evidence,
        )
    evidence["wheel_sha256"] = sha256_file(wheel)
    with tempfile.TemporaryDirectory(prefix="cert-smoke-") as tmpdir:
        venv_path = Path(tmpdir) / "smoke-venv"
        venv_bin = venv_path / "bin"
        actions = [
            (["uv", "venv", str(venv_path)], ["uv", "venv", "<fresh-venv>"]),
            (
                ["uv", "pip", "install", "--python", str(venv_bin / "python"), str(wheel)],
                ["uv", "pip", "install", "--python", "<fresh-venv>/bin/python", wheel_name],
            ),
            ([str(venv_bin / "verdict"), "--help"], ["<fresh-venv>/bin/verdict", "--help"]),
        ]
        for command, display in actions:
            result = run_command(command, cwd=repo_path)
            commands.append(
                {"command": display, "exit_code": result.returncode, **command_failure(result)}
            )
            if result.returncode != 0:
                return StepResult(
                    "package_smoke",
                    "Package smoke test",
                    "FAIL",
                    f"{display[0]} {command_failure_reason(result)}",
                    (datetime.now(timezone.utc) - start).total_seconds(),
                    evidence,
                )
        evidence["help_has_usage"] = "usage:" in result.stdout.lower()
    status: Literal["PASS", "FAIL", "SKIPPED", "INCOMPLETE"] = (
        "PASS" if evidence["help_has_usage"] else "FAIL"
    )
    return StepResult(
        "package_smoke",
        "Package smoke test",
        status,
        "" if status == "PASS" else "verdict --help lacked usage text",
        (datetime.now(timezone.utc) - start).total_seconds(),
        evidence,
    )


def step_security(repo_path: Path, venv_bin: Path) -> StepResult:
    """Run bandit (SAST) and pip-audit (dependency CVE scan).

    bandit is a declared dev dependency; missing binary -> FAIL.
    Runs with -c pyproject.toml -r verdict -ll (medium+ severity).
    pip-audit scans local packages; vulnerabilities -> FAIL, network/DB error -> INCOMPLETE.
    """
    start = datetime.now(timezone.utc)

    evidence: dict[str, Any] = {
        "bandit": {
            "command": [
                "bandit",
                "-q",
                "-c",
                "pyproject.toml",
                "-r",
                "verdict",
                "-ll",
                "-f",
                "json",
                "-o",
                "<temporary-report>",
            ],
            "exit_code": None,
            "medium_plus_findings": None,
            "report_sha256": None,
        },
        "pip_audit": {
            "command": [
                "pip-audit",
                "--local",
                "--skip-editable",
                "-f",
                "json",
                "-o",
                "<temporary-report>",
            ],
            "exit_code": None,
            "vulnerabilities": None,
            "report_sha256": None,
        },
    }

    # Bandit is a declared dev dependency; absent is FAIL, not SKIPPED
    bandit_bin = venv_bin / "bandit"
    if not bandit_bin.exists():
        return StepResult(
            step_id="security",
            name="Security checks",
            status="FAIL",
            reason="bandit declared dev dependency missing; run uv sync --extra dev",
            evidence={**evidence, "error_class": "FileNotFoundError"},
        )

    # Run bandit with -q and -o <tmpfile> to avoid progress bar in stdout
    with tempfile.TemporaryDirectory() as tmpdir:
        bandit_output_file = Path(tmpdir) / "bandit-output.json"
        bandit_result = run_command(
            [
                str(bandit_bin),
                "-q",
                "-c",
                "pyproject.toml",
                "-r",
                "verdict",
                "-ll",
                "-f",
                "json",
                "-o",
                str(bandit_output_file),
            ],
            cwd=repo_path,
        )

        evidence["bandit"]["exit_code"] = bandit_result.returncode
        evidence["bandit"].update(command_failure(bandit_result))
        if command_failure(bandit_result):
            return StepResult(
                "security",
                "Security checks",
                "FAIL",
                f"bandit: {command_failure_reason(bandit_result)}",
                (datetime.now(timezone.utc) - start).total_seconds(),
                evidence,
            )
        # Parse bandit results from file
        bandit_status = "PASS"
        bandit_reason = ""
        if bandit_output_file.exists():
            try:
                bandit_data = json.loads(bandit_output_file.read_text())
                if not isinstance(bandit_data.get("results"), list):
                    raise ValueError("bandit results missing")
                evidence["bandit"]["report_sha256"] = sha256_file(bandit_output_file)
                # -ll filters to medium+; count findings at that severity
                medium_plus = [
                    r
                    for r in bandit_data.get("results", [])
                    if r.get("issue_severity", "").upper() in ("MEDIUM", "HIGH")
                ]
                evidence["bandit"]["medium_plus_findings"] = len(medium_plus)
                if medium_plus:
                    bandit_status = "FAIL"
                    bandit_reason = f"bandit: {len(medium_plus)} medium+ finding(s)"
                elif bandit_result.returncode != 0:
                    bandit_status = "INCOMPLETE"
                    bandit_reason = f"bandit exit code {bandit_result.returncode} without findings"
            except (json.JSONDecodeError, ValueError):
                bandit_status = "INCOMPLETE" if bandit_result.returncode == 0 else "FAIL"
                bandit_reason = "bandit: invalid JSON report"
        else:
            bandit_status = "INCOMPLETE" if bandit_result.returncode == 0 else "FAIL"
            bandit_reason = "bandit: output file not created"

        # Run pip-audit (optionally write to file for symmetry)
        pip_audit_bin = venv_bin / "pip-audit"
        if not pip_audit_bin.exists():
            return StepResult(
                step_id="security",
                name="Security checks",
                status="FAIL",
                reason="pip-audit declared dev dependency missing; run uv sync --extra dev",
                evidence={**evidence, "error_class": "FileNotFoundError"},
            )

        pip_audit_output_file = Path(tmpdir) / "pip-audit-output.json"
        pip_audit_result = run_command(
            [
                str(pip_audit_bin),
                "--local",
                "--skip-editable",
                "-f",
                "json",
                "-o",
                str(pip_audit_output_file),
            ],
            cwd=repo_path,
        )

        evidence["pip_audit"]["exit_code"] = pip_audit_result.returncode
        evidence["pip_audit"].update(command_failure(pip_audit_result))
        if command_failure(pip_audit_result):
            return StepResult(
                "security",
                "Security checks",
                "FAIL",
                f"pip_audit: {command_failure_reason(pip_audit_result)}",
                (datetime.now(timezone.utc) - start).total_seconds(),
                evidence,
            )
        # Parse pip-audit results from file
        pip_audit_status = "PASS"
        pip_audit_reason = ""
        if pip_audit_output_file.exists():
            try:
                audit_data = json.loads(pip_audit_output_file.read_text())
                vulnerabilities = audit_data.get("dependencies")
                if not isinstance(vulnerabilities, list):
                    raise ValueError("pip-audit dependencies missing")
                evidence["pip_audit"]["report_sha256"] = sha256_file(pip_audit_output_file)
                vuln_list = [dep for dep in vulnerabilities if dep.get("vulns", [])]
                evidence["pip_audit"]["vulnerabilities"] = sum(
                    len(dep["vulns"]) for dep in vuln_list
                )
                if vuln_list:
                    pip_audit_status = "FAIL"
                    vuln_count = sum(len(dep["vulns"]) for dep in vuln_list)
                    pkg_names = ", ".join(dep["name"] for dep in vuln_list[:3])
                    pip_audit_reason = f"pip-audit: {vuln_count} vulnerability/ies in {pkg_names}"
                    if len(vuln_list) > 3:
                        pip_audit_reason += f" +{len(vuln_list) - 3} more"
                elif pip_audit_result.returncode != 0:
                    pip_audit_status = "INCOMPLETE"
                    pip_audit_reason = (
                        f"pip-audit exit code {pip_audit_result.returncode} without findings"
                    )
            except (json.JSONDecodeError, ValueError):
                pip_audit_status = "INCOMPLETE" if pip_audit_result.returncode == 0 else "FAIL"
                pip_audit_reason = "pip-audit: invalid JSON report"
        else:
            # Network or DB error: pip-audit writes errors to stderr with non-zero exit
            stderr_lower = pip_audit_result.stderr.lower()
            # Check for connection/network/timeout/DNS/SSL markers
            if pip_audit_result.returncode != 0 and (
                "connection" in stderr_lower
                or "timeout" in stderr_lower
                or "network" in stderr_lower
                or "dns" in stderr_lower
                or "ssl" in stderr_lower
                or "certificate" in stderr_lower
            ):
                pip_audit_status = "INCOMPLETE"
                pip_audit_reason = "pip-audit: network/DB error (no report)"
            else:
                pip_audit_status = "INCOMPLETE" if pip_audit_result.returncode == 0 else "FAIL"
                pip_audit_reason = "pip-audit: output file not created"

    duration = (datetime.now(timezone.utc) - start).total_seconds()

    # Combine results: FAIL if either fails, INCOMPLETE if pip-audit incomplete, else PASS
    if bandit_status == "FAIL" or pip_audit_status == "FAIL":
        combined_reason = "; ".join(filter(None, [bandit_reason, pip_audit_reason]))
        return StepResult(
            step_id="security",
            name="Security checks",
            status="FAIL",
            reason=combined_reason,
            duration_seconds=duration,
            evidence=evidence,
        )
    elif bandit_status == "INCOMPLETE" or pip_audit_status == "INCOMPLETE":
        return StepResult(
            step_id="security",
            name="Security checks",
            status="INCOMPLETE",
            reason="; ".join(filter(None, [bandit_reason, pip_audit_reason])),
            duration_seconds=duration,
            evidence=evidence,
        )
    else:
        return StepResult(
            step_id="security",
            name="Security checks",
            status="PASS",
            duration_seconds=duration,
            evidence=evidence,
        )


def step_docs_check(repo_path: Path, venv_bin: Path) -> StepResult:
    """Run scripts/check_doc_links.py."""
    start = datetime.now(timezone.utc)

    doc_script = repo_path / "scripts" / "check_doc_links.py"
    if not doc_script.exists():
        return StepResult(
            step_id="docs_check",
            name="Documentation link check",
            status="SKIPPED",
            reason="check_doc_links.py not found",
        )

    result = run_command([str(venv_bin / "python"), str(doc_script)], cwd=repo_path)
    duration = (datetime.now(timezone.utc) - start).total_seconds()
    evidence = {
        "command": ["<checkout>/.venv/bin/python", "scripts/check_doc_links.py"],
        "exit_code": result.returncode,
        **command_failure(result),
    }

    if result.returncode == 0:
        return StepResult(
            step_id="docs_check",
            name="Documentation link check",
            status="PASS",
            duration_seconds=duration,
            evidence=evidence,
        )
    else:
        return StepResult(
            step_id="docs_check",
            name="Documentation link check",
            status="FAIL",
            reason=command_failure_reason(result),
            duration_seconds=duration,
            evidence=evidence,
        )


def step_git_clean(repo_path: Path) -> StepResult:
    """Verify git status is clean after all steps."""
    result = run_command(["git", "status", "--porcelain"], cwd=repo_path)

    if result.returncode != 0:
        return StepResult(
            "git_clean",
            "Git clean check",
            "FAIL",
            command_failure_reason(result),
            evidence={
                "command": ["git", "status", "--porcelain"],
                "exit_code": result.returncode,
                **command_failure(result),
            },
        )
    if not result.stdout.strip():
        return StepResult(
            step_id="git_clean",
            name="Git clean check",
            status="PASS",
            reason="Working tree clean after all steps",
        )
    else:
        return StepResult(
            step_id="git_clean",
            name="Git clean check",
            status="FAIL",
            reason=f"{len(result.stdout.splitlines())} untracked/modified file(s)",
        )


def _producer_git_sha_from_receipt(receipt: dict[str, Any]) -> str | None:
    """Return only a recorded string SHA; missing, null or blank stays unknown."""
    producer = receipt.get("producer")
    if not isinstance(producer, dict):
        return None
    raw = producer.get("git_sha")
    if not isinstance(raw, str):
        return None
    return raw.strip() or None


def verdict_tree_changed_between(repo_path: Path, left_sha: str, right_sha: str) -> bool | None:
    """Return the verdict/ diff result, or None when Git cannot verify it."""
    # Event data is untrusted; only commit identities, never Git options or
    # revision expressions, may enter this command.
    if not all(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha) for sha in (left_sha, right_sha)):
        return None
    if left_sha == right_sha:
        return False
    result = run_command(
        ["git", "diff", "--quiet", left_sha, right_sha, "--", "verdict/"], cwd=repo_path, timeout=5
    )
    if result.returncode == 0:
        return False
    if result.returncode == 1:
        return True
    return None


# This token is deliberately not serialized. Evidence flags cannot mint PASS.
_VERIFIED_REHEARSALS = object()
ATTESTATION_REPO = "mrnicholasbcarter-code/verdict-core"
ATTESTATION_WORKFLOW = f"{ATTESTATION_REPO}/.github/workflows/certify-rehearsal.yml"
AttestationVerifier = Callable[[Path, Path, str, str], list[dict[str, Any]]]


class AttestationUnavailableError(RuntimeError):
    """The independent verifier cannot be started (not a verification failure)."""


def verify_rehearsal_attestation(
    receipt: Path, bundle: Path, repository: str, certified_sha: str
) -> list[dict[str, Any]]:
    """Delegate signature and certificate identity checks to GitHub's verifier.

    Only verified JSON statements may reach the local subject-digest check.
    Never treat raw bundle predicate fields as trusted signer identity.
    """
    if repository != ATTESTATION_REPO or not re.fullmatch(r"[0-9a-f]{40}", certified_sha):
        raise ValueError("Invalid attestation policy")
    command = [
        "gh",
        "attestation",
        "verify",
        str(receipt),
        "--bundle",
        str(bundle),
        "--repo",
        repository,
        "--signer-workflow",
        ATTESTATION_WORKFLOW,
        "--source-ref",
        "refs/heads/main",
        "--source-digest",
        certified_sha,
        "--signer-digest",
        certified_sha,
        "--cert-oidc-issuer",
        "https://token.actions.githubusercontent.com",
        "--deny-self-hosted-runners",
        "--predicate-type",
        "https://slsa.dev/provenance/v1",
        "--format",
        "json",
    ]
    result = run_command(command, timeout=120)
    if command_failure(result).get("error_class") in {"FileNotFoundError", "PermissionError"}:
        raise AttestationUnavailableError("GitHub attestation verifier unavailable")
    if result.returncode != 0:
        raise ValueError("GitHub attestation verification failed")
    verified = json.loads(result.stdout)
    if (
        not isinstance(verified, list)
        or not verified
        or not all(isinstance(v, dict) for v in verified)
    ):
        raise ValueError("Invalid verified attestation output")
    return verified


def _verified_subject_digest(verified: list[dict[str, Any]], expected: str) -> bool:
    """Accept only the exact SHA256 subject in a cryptographically verified statement."""
    if not verified:
        return False
    for entry in verified:
        statement = entry.get("verificationResult", {}).get("statement", {})
        if statement.get("predicateType") != "https://slsa.dev/provenance/v1":
            return False
        subjects = statement.get("subject")
        if not isinstance(subjects, list) or len(subjects) != 1:
            return False
        if not isinstance(subjects[0], dict) or subjects[0].get("digest") != {"sha256": expected}:
            return False
    return True


def step_rehearsals(
    repo_path: Path,
    rehearsal_dirs: dict[str, Path],
    output_dir: Path,
    certified_git_sha: str | None = None,
    *,
    attested_rehearsals: Path | None = None,
    attestation_verifier: AttestationVerifier = verify_rehearsal_attestation,
) -> StepResult:
    """Verify semantics and, only for explicit attested input, GitHub provenance.

    Local receipts remain non-certifying. Missing proof or unavailable gh is
    INCOMPLETE; failed signature, identity, SHA, digest or semantics is FAIL.
    """
    if attested_rehearsals is not None:
        if rehearsal_dirs:
            return StepResult("rehearsals", "Rehearsal verification", "FAIL", "Ambiguous inputs")
        rehearsal_dirs = {name: attested_rehearsals / name for name in ("clean", "chaos")}
    if not rehearsal_dirs:
        return StepResult(
            "rehearsals",
            "Rehearsal verification",
            "SKIPPED",
            "Live rehearsal requires gateway credentials (none provided)",
        )
    # Import through the checked-out package.  Receipt verification recomputes the
    # outcome, graph, review and events, not merely the (self-reported) digest.
    from verdict.orchestration.receipt import verify_run_receipt

    rehearsal_output = output_dir / "rehearsals"
    rehearsal_output.mkdir(parents=True, exist_ok=True)
    proof: dict[str, Any] = {}
    if certified_git_sha is None:
        try:
            certified_git_sha = get_git_sha(repo_path)
        except RuntimeError:
            certified_git_sha = None
    producer_reports: list[str] = []
    stale_notes: list[str] = []
    for name, run_dir in rehearsal_dirs.items():
        if not name or name in {".", ".."} or Path(name).name != name or name.startswith("."):
            return StepResult(
                "rehearsals", "Rehearsal verification", "FAIL", "Invalid rehearsal name"
            )
        if not run_dir.is_dir():
            return StepResult(
                "rehearsals", "Rehearsal verification", "FAIL", f"Missing rehearsal {name}"
            )
        source_files = ("events.jsonl", "receipt.json", "graph.json")
        if any(not (run_dir / file).is_file() for file in source_files):
            return StepResult(
                "rehearsals",
                "Rehearsal verification",
                "FAIL",
                f"Missing events.jsonl, receipt.json or graph.json in {name}",
            )
        target = rehearsal_output / name
        if target.exists():
            shutil.rmtree(target)
        target.mkdir()
        for file in (*source_files, "review.json"):
            if (run_dir / file).is_file():
                shutil.copy2(run_dir / file, target / file)
        try:
            receipt = json.loads((target / "receipt.json").read_text(encoding="utf-8"))
            if not isinstance(receipt, dict):
                raise ValueError("receipt must be an object")
            problems = verify_run_receipt(target)
            if problems:
                raise ValueError("receipt verification failed: " + "; ".join(problems[:3]))
            if receipt.get("outcome") != "COMPLETE" or receipt.get("claimed_outcome") != "COMPLETE":
                raise ValueError("run did not complete")
            attempts = [a for node in receipt.get("nodes", []) for a in node.get("attempts", [])]
            injected_failures = sum(
                a.get("fault_injected") is True and a.get("outcome") == "failure" for a in attempts
            )
            if name == "chaos" and injected_failures == 0:
                raise ValueError("no controlled injected failure recorded")
            if name == "clean" and any(a.get("fault_injected") is True for a in attempts):
                raise ValueError("clean rehearsal includes injected faults")
            producer_sha = _producer_git_sha_from_receipt(receipt)
            producer_reports.append(f"{name}={producer_sha or 'null'}")
            if attested_rehearsals is not None and (
                not certified_git_sha or producer_sha != certified_git_sha
            ):
                raise ValueError("attested producer SHA differs from certified SHA")
            changed: bool | None = None
            if producer_sha is None:
                stale_notes.append(f"{name} producer git_sha is null (cannot verify freshness)")
            elif not certified_git_sha:
                stale_notes.append(f"{name} certified git_sha is null (cannot verify freshness)")
            elif attested_rehearsals is not None:
                changed = False
            else:
                changed = verdict_tree_changed_between(repo_path, producer_sha, certified_git_sha)
                if changed is True:
                    stale_notes.append(
                        f"{name} producer {producer_sha} differs from certified"
                        f" {certified_git_sha} with verdict/ changes"
                    )
                elif changed is None:
                    stale_notes.append(
                        f"{name} producer {producer_sha} differs from certified"
                        f" {certified_git_sha} (could not verify verdict/ diff)"
                    )
            proof[name] = {
                "producer_git_sha": producer_sha,
                "certified_git_sha": certified_git_sha,
                "verdict_tree_changed": changed,
                "run_id": receipt.get("run_id"),
                "outcome": receipt["outcome"],
                "injected_failures": injected_failures,
                "route_identity_summary": receipt.get("route_identity_summary"),
                "reviewer": receipt.get("review", {}).get("reviewer"),
                "attempt_producers": sorted(
                    {str(a.get("executed_model")) for a in attempts if a.get("executed_model")}
                ),
                "attempt_identity_source": "receipt-reported; not independently attested",
                "attempts": [
                    {
                        "node_id": node.get("node_id"),
                        **{
                            key: attempt.get(key)
                            for key in (
                                "attempt",
                                "intended_route",
                                "executed_model",
                                "provider",
                                "route_id",
                                "route_identity",
                                "outcome",
                                "fault_injected",
                                "failure_category",
                            )
                        },
                    }
                    for node in receipt.get("nodes", [])
                    for attempt in node.get("attempts", [])
                ],
            }
            if attested_rehearsals is not None:
                assert certified_git_sha is not None  # Exact-SHA guard above already passed.
                bundle = run_dir / "attestation.json"
                if not bundle.is_file():
                    stale_notes.append(f"{name} attestation is missing")
                    continue
                if bundle.is_symlink():
                    raise ValueError("attestation must not be a symlink")
                copied_bundle = target / "attestation.json"
                shutil.copy2(bundle, copied_bundle)
                digest = sha256_file(target / "receipt.json")
                try:
                    verified = attestation_verifier(
                        target / "receipt.json", copied_bundle, ATTESTATION_REPO, certified_git_sha
                    )
                except AttestationUnavailableError:
                    stale_notes.append(f"{name} independent verifier unavailable")
                    continue
                if not _verified_subject_digest(verified, digest):
                    raise ValueError("attestation subject digest differs from receipt SHA256")
                proof[name]["attestation"] = {
                    "receipt_sha256": digest,
                    "bundle_sha256": sha256_file(copied_bundle),
                    "repository": ATTESTATION_REPO,
                    "signer_workflow": ATTESTATION_WORKFLOW,
                    "source_ref": "refs/heads/main",
                    "source_digest": certified_git_sha,
                }
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError) as exc:
            return StepResult(
                "rehearsals",
                "Rehearsal verification",
                "FAIL",
                f"Invalid rehearsal {name}: {exc}",
                evidence={"runs": proof},
            )

    notes = [f"Receipt semantics verified; producers: {', '.join(producer_reports)}"]
    notes.extend(stale_notes)
    if not {"clean", "chaos"}.issubset(proof):
        notes.append("Both clean and chaos (controlled-failure) rehearsals are required")
    if attested_rehearsals is not None and not stale_notes and set(proof) == {"clean", "chaos"}:
        result = StepResult(
            "rehearsals",
            "Rehearsal verification",
            "PASS",
            "GitHub OIDC provenance and clean/chaos receipt semantics verified",
            evidence={"runs": proof, "independent_producer_attestation": True},
        )
        result._attestation_token = _VERIFIED_REHEARSALS
        return result
    # These artifacts are produced by the very runtime under test. Rebuilding
    # its receipt does not independently attest to the producer or execution.
    notes.append("Independent producer attestation is unavailable")
    return StepResult(
        "rehearsals",
        "Rehearsal verification",
        "INCOMPLETE",
        "; ".join(notes),
        evidence={"runs": proof, "independent_producer_attestation": False},
    )


# === Main Certification Flow ===


def junit_parity(steps: list[StepResult]) -> bool:
    """Require identical testcase IDs/outcomes, not merely matching totals."""
    reports = {
        s.step_id: s.evidence.get("junit")
        for s in steps
        if s.step_id in {"test_clean", "test_dirty"}
    }
    if set(reports) != {"test_clean", "test_dirty"}:
        return False
    clean, dirty = reports["test_clean"], reports["test_dirty"]
    if not isinstance(clean, dict) or not isinstance(dirty, dict):
        return False
    keys = ("tests", "passed", "failures", "errors", "skipped")
    return (
        all(clean.get(key) == dirty.get(key) for key in keys)
        and isinstance(clean.get("testcases"), dict)
        and bool(clean["testcases"])
        and clean["testcases"] == dirty.get("testcases")
    )


def compute_verdict(
    steps: list[StepResult], *, git_dirty: bool
) -> Literal["CERTIFIED", "INCOMPLETE", "FAILED"]:
    """Compute certification verdict from steps and git state.

    Rules:
    - Any FAIL -> FAILED
    - Any INCOMPLETE or SKIPPED or git_dirty -> INCOMPLETE
    - Missing required steps or required command evidence -> INCOMPLETE
    - Otherwise -> CERTIFIED
    """
    has_failures = any(s.status == "FAIL" for s in steps)
    has_incomplete = any(s.status in ("INCOMPLETE", "SKIPPED") for s in steps)
    required = {
        "test_clean",
        "test_dirty",
        "ruff_check",
        "ruff_format",
        "mypy",
        "build",
        "package_smoke",
        "security",
        "docs_check",
        "git_clean",
        "rehearsals",
    }
    present = {step.step_id for step in steps}
    missing_evidence = any(
        not step.evidence.get("command") or step.evidence.get("exit_code") is None
        for step in steps
        if step.step_id
        in {"test_clean", "test_dirty", "ruff_check", "ruff_format", "mypy", "build", "docs_check"}
    )
    missing_evidence |= any(
        not step.evidence.get("junit")
        for step in steps
        if step.step_id in {"test_clean", "test_dirty"}
    )
    junit_steps = {
        s.step_id: s.evidence.get("junit")
        for s in steps
        if s.step_id in {"test_clean", "test_dirty"}
    }
    if len(junit_steps) == 2:
        missing_evidence |= not junit_parity(steps)

    # A serialized/self-asserted evidence flag cannot mint independent verification.
    missing_evidence |= any(
        step._attestation_token is not _VERIFIED_REHEARSALS
        for step in steps
        if step.step_id == "rehearsals"
    )
    missing_evidence |= any(
        not step.evidence.get("artifacts") for step in steps if step.step_id == "build"
    )
    missing_evidence |= any(
        len(step.evidence.get("commands", [])) != 3 or not step.evidence.get("help_has_usage")
        for step in steps
        if step.step_id == "package_smoke"
    )
    missing_evidence |= any(
        not step.evidence.get(tool, {}).get("report_sha256")
        for step in steps
        if step.step_id == "security"
        for tool in ("bandit", "pip_audit")
    )
    if has_failures:
        return "FAILED"
    if has_incomplete or git_dirty or present != required or missing_evidence:
        return "INCOMPLETE"
    return "CERTIFIED"


def run_checked_step(
    repo_path: Path, expected_sha: str, runner: Callable[[], StepResult]
) -> StepResult:
    """Check the source identity around every potentially long-running step."""
    assert_git_sha(repo_path, expected_sha)
    result = runner()
    assert_git_sha(repo_path, expected_sha)
    return result


def _run_certification(
    repo_path: Path,
    *,
    allow_dirty: bool = False,
    rehearsal_dirs: dict[str, Path] | None = None,
    attested_rehearsals: Path | None = None,
    attestation_verifier: AttestationVerifier = verify_rehearsal_attestation,
    output_dir: Path | None = None,
    staging_dir: Path | None = None,
    attempt_id: str | None = None,
) -> tuple[CertificationManifest, dict[str, Any]]:
    """Run full certification and return manifest and detailed results."""
    manifest = CertificationManifest()
    manifest.started_at = utc_timestamp()

    # Validate the destination, but never remove a prior same-SHA bundle here.
    manifest.git_sha = get_git_sha(repo_path)
    assert_git_sha(repo_path, manifest.git_sha)
    destination = output_dir or (repo_path / "artifacts" / "certification" / manifest.git_sha)
    sources = list((rehearsal_dirs or {}).values())
    if attested_rehearsals is not None:
        sources.append(attested_rehearsals)
    if any(
        source.resolve() == destination.resolve()
        or destination.resolve() in source.resolve().parents
        for source in sources
    ):
        raise ValueError("Rehearsal source must not be inside the output directory")
    prepare_bundle_dir(destination, repo_path, create=False)
    manifest.git_dirty = is_git_dirty(repo_path)

    if manifest.git_dirty and not allow_dirty:
        print("ERROR: Git working tree is dirty. Use --allow-dirty to force.", file=sys.stderr)
        sys.exit(1)

    if manifest.git_dirty:
        print("WARNING: Running certification on dirty tree. Verdict will be INCOMPLETE.")

    # Capture environment
    env_snapshot = capture_environment(repo_path)

    # Prepare venv path
    venv_bin = repo_path / ".venv" / "bin"
    if not venv_bin.exists():
        print("ERROR: .venv/bin not found. Run 'uv sync' first.", file=sys.stderr)
        sys.exit(1)

    # Sanity-check: the venv's Python must import verdict from this checkout,
    # not from a different editable install.  Using `python -m pytest` (instead
    # of the shebang'd pytest script) ensures the interpreter is always the one
    # in this venv, regardless of what shebang the script carries.
    sanity = run_command(
        [
            str(venv_bin / "python"),
            "-c",
            "import verdict; import pathlib; "
            "p = pathlib.Path(verdict.__file__).resolve().parents[1]; "
            f"expected = pathlib.Path('{repo_path}').resolve(); "
            "assert p == expected, f'verdict imported from {{p}}, expected {expected}'",
        ],
        cwd=repo_path,
    )
    if sanity.returncode != 0:
        print(f"ERROR: venv sanity check failed:\n{sanity.stderr}", file=sys.stderr)
        sys.exit(1)

    # A clean checkout can switch HEAD during environment capture or sanity checks.
    assert_git_sha(repo_path, manifest.git_sha)

    # Run all steps
    steps = []

    print("Running certification steps...")

    # Test steps
    print("  [1/11] Test suite (clean shell)...")
    steps.append(
        run_checked_step(
            repo_path, manifest.git_sha, lambda: step_test_clean_shell(repo_path, venv_bin)
        )
    )

    print("  [2/11] Test suite (dirty shell)...")
    steps.append(
        run_checked_step(
            repo_path, manifest.git_sha, lambda: step_test_dirty_shell(repo_path, venv_bin)
        )
    )

    # Lint/type steps
    print("  [3/11] Ruff lint...")
    steps.append(
        run_checked_step(repo_path, manifest.git_sha, lambda: step_ruff_check(repo_path, venv_bin))
    )

    print("  [4/11] Ruff format check...")
    steps.append(
        run_checked_step(repo_path, manifest.git_sha, lambda: step_ruff_format(repo_path, venv_bin))
    )

    print("  [5/11] Mypy strict...")
    steps.append(
        run_checked_step(repo_path, manifest.git_sha, lambda: step_mypy(repo_path, venv_bin))
    )

    # Build step
    print("  [6/11] Package build...")
    steps.append(run_checked_step(repo_path, manifest.git_sha, lambda: step_build(repo_path)))

    # Package smoke test
    print("  [7/11] Package smoke test...")
    build_step = steps[-1]
    wheels = [
        a["name"] for a in build_step.evidence.get("artifacts", []) if a["name"].endswith(".whl")
    ]
    steps.append(
        run_checked_step(
            repo_path,
            manifest.git_sha,
            lambda: step_package_smoke(
                repo_path, wheels[0] if build_step.status == "PASS" and len(wheels) == 1 else None
            ),
        )
    )

    # Security
    print("  [8/11] Security checks...")
    steps.append(
        run_checked_step(repo_path, manifest.git_sha, lambda: step_security(repo_path, venv_bin))
    )

    # Docs
    print("  [9/11] Documentation link check...")
    steps.append(
        run_checked_step(repo_path, manifest.git_sha, lambda: step_docs_check(repo_path, venv_bin))
    )

    # Rehearsals
    print("  [10/11] Rehearsal verification...")
    steps.append(
        run_checked_step(
            repo_path,
            manifest.git_sha,
            lambda: step_rehearsals(
                repo_path,
                rehearsal_dirs or {},
                staging_dir or destination,
                certified_git_sha=manifest.git_sha,
                attested_rehearsals=attested_rehearsals,
                attestation_verifier=attestation_verifier,
            ),
        )
    )

    print("  [11/11] Git clean check...")
    steps.append(run_checked_step(repo_path, manifest.git_sha, lambda: step_git_clean(repo_path)))
    manifest.steps = steps
    manifest.finished_at = utc_timestamp()
    assert_git_sha(repo_path, manifest.git_sha)

    # Determine verdict
    manifest.verdict = compute_verdict(steps, git_dirty=manifest.git_dirty)

    # Collect detailed results
    detailed = {"manifest": manifest.to_dict(), "environment": env_snapshot.to_dict()}

    return manifest, detailed


def markdown_cell(value: Any) -> str:
    """Escape untrusted text in Markdown headings, prose, lists, and table cells."""
    text = html.escape(str(value), quote=True)
    # Normalize line separators so fields cannot open a new Markdown block.
    text = re.sub(r"[\r\n\u0085\u2028\u2029]+", "\n", text)
    return "<br>".join(
        re.sub(r"([\\`*_{}\[\]()#+!|>~-])", r"\\\1", line) for line in text.split("\n")
    )


def generate_certification_md(
    manifest: CertificationManifest, env: EnvironmentSnapshot | dict[str, Any]
) -> str:
    """Generate CERTIFICATION.md from manifest and environment data."""
    # Normalize env to dict
    env_dict = env if isinstance(env, dict) else env.to_dict()

    lines = [
        f"# Release Certification: {markdown_cell(manifest.git_sha)}",
        "",
        f"**Verdict**: {markdown_cell(manifest.verdict)}",
        f"**Started**: {markdown_cell(manifest.started_at)}",
        f"**Finished**: {markdown_cell(manifest.finished_at)}",
        f"**Git SHA**: {markdown_cell(manifest.git_sha)}",
        f"**Git Dirty**: {markdown_cell(manifest.git_dirty)}",
        "",
        "## Environment",
        "",
        f"- **Python**: {markdown_cell(env_dict['python_version'])}",
        f"- **Platform**: {markdown_cell(env_dict['platform'])}",
        f"- **uv**: {markdown_cell(env_dict['uv_version'])}",
    ]

    if env_dict.get("node_version"):
        lines.append(f"- **Node**: {markdown_cell(env_dict['node_version'])}")

    lines.extend(
        [
            f"- **Lockfile SHA256**: {markdown_cell(env_dict['lockfile_sha256'][:16])}...",
            f"- **Environment variables**: {len(env_dict['env_var_names'])} (names only, normalized)",
            "",
            "## Certification Steps",
            "",
            "| Step | Status | Duration | Reason |",
            "|------|--------|----------|--------|",
        ]
    )

    for step in manifest.steps:
        duration_str = f"{step.duration_seconds:.1f}s" if step.duration_seconds > 0 else "-"
        reason_str = step.reason or "-"
        lines.append(
            f"| {markdown_cell(step.name)} | {markdown_cell(step.status)} | "
            f"{markdown_cell(duration_str)} | {markdown_cell(reason_str)} |"
        )

    lines.extend(
        [
            "",
            "## Normalization Notes",
            "",
            "The following fields are normalized or may differ between runs:",
            "",
            "- Timestamps (started_at, finished_at)",
            "- Duration measurements",
            "- Temporary file paths",
            "- Environment variable list (OS-dependent)",
            "",
            "Evidence includes live report hashes and host-dependent metadata. "
            "The same source SHA does not guarantee byte-identical bundles.",
        ]
    )

    return "\n".join(lines)


def prepare_bundle_dir(output_dir: Path, repo_path: Path, *, create: bool = True) -> None:
    """Remove stale default contents; never delete user files in custom destinations."""
    output_dir = output_dir.absolute()
    managed_root = (repo_path / "artifacts" / "certification").resolve()
    if output_dir == repo_path.resolve() or output_dir == managed_root:
        raise ValueError("Refusing to write bundle into checkout root or certification root")
    if output_dir.parent.resolve() == managed_root and output_dir.name in {".attempts", ".history"}:
        raise ValueError("Refusing reserved certification metadata directory")
    if (
        output_dir.is_symlink()
        or output_dir.parent.is_symlink()
        or (repo_path / "artifacts").is_symlink()
    ):
        raise ValueError("Refusing symlinked output directory")
    if output_dir.parent.resolve() == managed_root and re.fullmatch(
        r"[0-9a-f]{40}", output_dir.name
    ):
        if output_dir.name != get_git_sha(repo_path):
            raise ValueError("Refusing bundle path for a different Git HEAD")
        ignored = run_command(["git", "check-ignore", "-q", str(output_dir)], cwd=repo_path)
        tracked = run_command(
            ["git", "ls-files", "--", str(output_dir.relative_to(repo_path.resolve()))],
            cwd=repo_path,
        )
        if ignored.returncode != 0 or tracked.returncode != 0 or tracked.stdout.strip():
            raise ValueError("Refusing non-ignored or tracked bundle contents")
        # Existing evidence is immutable until a successful staged replacement.
    elif output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError("Custom output directory must be empty (will not delete user files)")
    if create:
        output_dir.mkdir(parents=True, exist_ok=True)


def write_bundle(
    output_dir: Path,
    manifest: CertificationManifest,
    env_snapshot: EnvironmentSnapshot | dict[str, Any],
    *,
    repo_path: Path | None = None,
) -> None:
    """Write evidence and recompute the final tracked git state before publishing verdict."""
    output_dir.mkdir(parents=True, exist_ok=True)

    # The reports are projections of the same step results serialized in the manifest.
    steps = {step.step_id: step for step in manifest.steps}
    groups = {
        "test-summary.json": ("test_clean", "test_dirty"),
        "lint-type-build.json": ("ruff_check", "ruff_format", "mypy", "build"),
        "package-smoke.json": ("package_smoke",),
        "security-summary.json": ("security",),
        "docs-check.json": ("docs_check",),
    }
    for filename, ids in groups.items():
        report = {
            step_id: {
                "status": steps[step_id].status,
                "reason": steps[step_id].reason,
                **steps[step_id].evidence,
            }
            for step_id in ids
            if step_id in steps
        }
        (output_dir / filename).write_text(json.dumps(report, indent=2, sort_keys=True))

    # manifest.json
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest.to_dict(), indent=2, sort_keys=True)
    )

    # environment.json
    env_dict = env_snapshot if isinstance(env_snapshot, dict) else env_snapshot.to_dict()
    (output_dir / "environment.json").write_text(json.dumps(env_dict, indent=2, sort_keys=True))

    # CERTIFICATION.md
    cert_md = generate_certification_md(manifest, env_snapshot)
    (output_dir / "CERTIFICATION.md").write_text(cert_md)

    # Check *after* writing the evidence. The earlier step runs before this
    # final write and cannot detect a tracked bundle edited by the writer.
    if repo_path is None:
        repo_path = output_dir.parent.parent.parent
    git_status = run_command(["git", "status", "--porcelain"], cwd=repo_path)
    first_git_failure = git_status if git_status.returncode != 0 else None
    (output_dir / "git-clean.txt").write_text(
        git_status.stdout if git_status.returncode == 0 else ""
    )
    # A tracked git-clean.txt can be changed by the snapshot itself. Check once
    # more after that write rather than trusting the earlier status.
    final_status = run_command(["git", "status", "--porcelain"], cwd=repo_path)
    if final_status.returncode != 0 or final_status.stdout != git_status.stdout:
        git_status = final_status
        (output_dir / "git-clean.txt").write_text(
            git_status.stdout if git_status.returncode == 0 else ""
        )
    git_step = next((step for step in manifest.steps if step.step_id == "git_clean"), None)
    if git_step is not None and (
        first_git_failure is not None or git_status.returncode != 0 or git_status.stdout.strip()
    ):
        failure_result = first_git_failure or git_status
        git_step.status = "FAIL"
        git_step.reason = "Git status failed or tree dirty after bundle write"
        git_step.evidence = {
            "command": ["git", "status", "--porcelain"],
            "exit_code": failure_result.returncode,
            **command_failure(failure_result),
        }
        manifest.verdict = compute_verdict(manifest.steps, git_dirty=manifest.git_dirty)
        (output_dir / "manifest.json").write_text(
            json.dumps(manifest.to_dict(), indent=2, sort_keys=True)
        )
        (output_dir / "CERTIFICATION.md").write_text(
            generate_certification_md(manifest, env_snapshot)
        )


def new_attempt_id() -> str:
    """Unique, UTC-sortable ID for an attempt or retained prior bundle."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ-") + uuid.uuid4().hex


def write_attempt_record(
    repo_path: Path,
    sha: str,
    attempt_id: str,
    status: str,
    reason: str,
    started_at: str,
    *,
    finished: bool = True,
    bundle_path: str | None = None,
) -> None:
    """Write an atomic, separate result marker; never modify published evidence."""
    root = repo_path / "artifacts" / "certification" / ".attempts"
    location = root / sha
    if root.is_symlink() or location.is_symlink() or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Unsafe attempt record directory")
    location.mkdir(parents=True, exist_ok=True)
    record = {
        "git_sha": sha,
        "attempt_id": attempt_id,
        "status": status,
        "reason": reason,
        "started_at": started_at,
        "finished_at": utc_timestamp() if finished else None,
        "bundle_path": bundle_path,
    }
    temporary = location / f".{attempt_id}.tmp"
    temporary.write_text(json.dumps(record, indent=2, sort_keys=True))
    os.replace(temporary, location / f"{attempt_id}.json")


def run_certification(
    repo_path: Path,
    *,
    allow_dirty: bool = False,
    rehearsal_dirs: dict[str, Path] | None = None,
    attested_rehearsals: Path | None = None,
    attestation_verifier: AttestationVerifier = verify_rehearsal_attestation,
    output_dir: Path | None = None,
    staging_dir: Path | None = None,
    attempt_id: str | None = None,
) -> tuple[CertificationManifest, dict[str, Any]]:
    """Record direct attempts, including early preflight aborts, outside the bundle."""
    sha = get_git_sha(repo_path)
    attempt_id = attempt_id or new_attempt_id()
    started_at = utc_timestamp()
    write_attempt_record(
        repo_path, sha, attempt_id, "ABORTED", "Attempt in progress", started_at, finished=False
    )
    try:
        manifest, detailed = _run_certification(
            repo_path,
            allow_dirty=allow_dirty,
            rehearsal_dirs=rehearsal_dirs,
            attested_rehearsals=attested_rehearsals,
            attestation_verifier=attestation_verifier,
            output_dir=output_dir,
            staging_dir=staging_dir,
            attempt_id=attempt_id,
        )
    except BaseException as exc:
        write_attempt_record(
            repo_path,
            sha,
            attempt_id,
            "ABORTED",
            f"{type(exc).__name__}: {str(exc).replace(str(repo_path), '<checkout>')[:200]}",
            started_at,
        )
        raise
    if output_dir is None and staging_dir is None:
        # A library-only attempt did not publish, even if its checks ran.
        write_attempt_record(
            repo_path,
            sha,
            attempt_id,
            "INCOMPLETE",
            "Checks completed but no bundle published",
            started_at,
        )
    return manifest, detailed


def publish_bundle(staging_dir: Path, output_dir: Path, repo_path: Path, expected_sha: str) -> None:
    """Retain an old SHA bundle before publishing a complete staged replacement."""
    assert_git_sha(repo_path, expected_sha)
    prepare_bundle_dir(output_dir, repo_path, create=False)
    managed = (
        output_dir.absolute()
        == (repo_path / "artifacts" / "certification" / expected_sha).absolute()
    )
    if output_dir.exists():
        if managed:
            history_root = repo_path / "artifacts" / "certification" / ".history"
            history_sha = history_root / expected_sha
            if history_root.is_symlink() or history_sha.is_symlink():
                raise ValueError("Refusing symlinked history directory")
            history_sha.mkdir(parents=True, exist_ok=True)
            os.replace(output_dir, history_sha / new_attempt_id())
        else:
            # Only a custom empty directory can be removed.
            output_dir.rmdir()
    # A crash after moving the old bundle leaves it intact under .history.
    assert_git_sha(repo_path, expected_sha)
    os.replace(staging_dir, output_dir)
    print(f"\nCertification bundle written to: {output_dir}")


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Generate immutable SHA-bound certification bundles for verdict-core"
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Bypass dirty-tree preflight; final dirt fails the git-clean step (FAILED verdict)",
    )
    parser.add_argument(
        "--rehearsal",
        action="append",
        metavar="NAME=PATH",
        help="Add rehearsal run directory (e.g., clean=/path/to/run, chaos=/path/to/run)",
    )
    parser.add_argument(
        "--attested-rehearsals",
        type=Path,
        help="Directory with clean/chaos receipts and attestation.json bundles from protected CI",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Override output directory (default: artifacts/certification/<sha>)",
    )

    args = parser.parse_args()

    # Parse rehearsal arguments
    rehearsal_dirs: dict[str, Path] = {}
    if args.rehearsal:
        for item in args.rehearsal:
            if "=" not in item:
                print(f"ERROR: Invalid rehearsal format: {item}", file=sys.stderr)
                print("Expected format: --rehearsal NAME=PATH", file=sys.stderr)
                sys.exit(1)
            name, path_str = item.split("=", 1)
            rehearsal_dirs[name] = Path(path_str)

    # Determine repo path (current directory)
    repo_path = Path.cwd()

    # Stage on the destination filesystem. Keep old evidence and an attempt marker
    # until a fully written replacement has been published.
    initial_sha = get_git_sha(repo_path)
    assert_git_sha(repo_path, initial_sha)
    output_dir = args.output_dir or (repo_path / "artifacts" / "certification" / initial_sha)
    attempt_id = new_attempt_id()
    started_at = utc_timestamp()
    try:
        # Validate output safety before creating any staging directories.
        prepare_bundle_dir(output_dir, repo_path, create=False)
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".cert-stage-", dir=output_dir.parent) as tmpdir:
            staging_dir = Path(tmpdir)
            manifest, detailed = run_certification(
                repo_path,
                allow_dirty=args.allow_dirty,
                rehearsal_dirs=rehearsal_dirs,
                attested_rehearsals=args.attested_rehearsals,
                output_dir=output_dir,
                staging_dir=staging_dir,
                attempt_id=attempt_id,
            )
            if manifest.git_sha != initial_sha:
                raise RuntimeError("Git HEAD changed during certification")
            assert_git_sha(repo_path, initial_sha)
            write_bundle(staging_dir, manifest, detailed["environment"], repo_path=repo_path)
            assert_git_sha(repo_path, manifest.git_sha)
            # A failed/incomplete rerun cannot replace a previous retained result.
            # Keep its full evidence next to (not inside) the separate attempt record.
            retained_attempt = None
            if (
                manifest.verdict != "CERTIFIED"
                and output_dir.exists()
                and (
                    output_dir.absolute()
                    == (repo_path / "artifacts" / "certification" / initial_sha).absolute()
                )
            ):
                attempt_root = repo_path / "artifacts" / "certification" / ".attempts"
                attempt_sha = attempt_root / initial_sha
                if attempt_root.is_symlink() or attempt_sha.is_symlink():
                    raise ValueError("Refusing symlinked attempt directory")
                attempt_sha.mkdir(parents=True, exist_ok=True)
                retained_attempt = attempt_sha / f"{attempt_id}.bundle"
                assert_git_sha(repo_path, manifest.git_sha)
                os.replace(staging_dir, retained_attempt)
            else:
                publish_bundle(staging_dir, output_dir, repo_path, manifest.git_sha)
    except BaseException as exc:
        write_attempt_record(
            repo_path,
            initial_sha,
            attempt_id,
            "ABORTED",
            f"{type(exc).__name__}: {str(exc).replace(str(repo_path), '<checkout>')[:200]}",
            started_at,
        )
        raise
    write_attempt_record(
        repo_path,
        initial_sha,
        attempt_id,
        manifest.verdict,
        "Attempt evidence retained without replacing prior bundle"
        if retained_attempt
        else "Bundle published",
        started_at,
        bundle_path=str(retained_attempt.relative_to(repo_path)) if retained_attempt else None,
    )

    # Print summary
    print(f"\nVerdict: {manifest.verdict}")
    print(f"SHA: {manifest.git_sha}")

    failed_steps = [s for s in manifest.steps if s.status == "FAIL"]
    if failed_steps:
        print("\nFailed steps:")
        for step in failed_steps:
            print(f"  - {step.name}: {step.reason}")

    sys.exit(0 if manifest.verdict == "CERTIFIED" else 1)


if __name__ == "__main__":
    main()
