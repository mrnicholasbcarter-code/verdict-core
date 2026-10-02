#!/usr/bin/env python3
"""Generate immutable, SHA-bound certification bundles for verdict-core releases.

This script produces a complete certification bundle under artifacts/certification/<sha>/
containing machine-readable evidence and a generated CERTIFICATION.md summary.

BOD-195: Turn release certification from hand-edited documents into an executable,
repeatable product feature bound to one exact source SHA.
"""

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
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
    timeout: float | None = None,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a command and return the result."""
    return subprocess.run(
        cmd, cwd=cwd, env=env, capture_output=capture_output, text=True, timeout=timeout
    )


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
    return result.stdout.strip()


def is_git_dirty(repo_path: Path) -> bool:
    """Check if git working tree is dirty."""
    result = run_command(["git", "status", "--porcelain"], cwd=repo_path)
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
    env = (
        os.environ.copy()
        if dirty
        else {
            "HOME": os.environ.get("HOME", ""),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "PATH": str(venv_bin) + ":" + os.environ.get("PATH", ""),
        }
    )
    if dirty:
        env["LLMGATE_AUTH_TOKEN"] = "bogus-cert-dirty"
    name = "Test suite (dirty shell)" if dirty else "Test suite (clean shell)"
    step_id = "test_dirty" if dirty else "test_clean"
    with tempfile.TemporaryDirectory(prefix="cert-junit-") as tmpdir:
        xml_path = Path(tmpdir) / "junit.xml"
        command = [str(venv_bin / "python"), "-m", "pytest", f"--junitxml={xml_path}"]
        display_command = [*command[:-1], "--junitxml=<temporary-report>"]
        result = run_command(command, cwd=repo_path, env=env, timeout=1800)
        evidence: dict[str, Any] = {
            "command": display_command,
            "exit_code": result.returncode,
            "junit": None,
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
                evidence["junit"] = {**totals, "sha256": sha256_file(xml_path)}
            except (ET.ParseError, ValueError, KeyError) as exc:
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
    evidence = {"command": [str(venv_bin / "ruff"), "check", "."], "exit_code": result.returncode}

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
            reason=f"Exit code {result.returncode}",
            duration_seconds=duration,
            evidence=evidence,
        )


def step_ruff_format(repo_path: Path, venv_bin: Path) -> StepResult:
    """Run ruff format --check."""
    start = datetime.now(timezone.utc)
    result = run_command([str(venv_bin / "ruff"), "format", "--check", "."], cwd=repo_path)
    duration = (datetime.now(timezone.utc) - start).total_seconds()
    evidence = {
        "command": [str(venv_bin / "ruff"), "format", "--check", "."],
        "exit_code": result.returncode,
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
            reason=f"Exit code {result.returncode}",
            duration_seconds=duration,
            evidence=evidence,
        )


def step_mypy(repo_path: Path, venv_bin: Path) -> StepResult:
    """Run mypy --strict."""
    start = datetime.now(timezone.utc)
    result = run_command([str(venv_bin / "mypy"), "--strict", "verdict"], cwd=repo_path)
    duration = (datetime.now(timezone.utc) - start).total_seconds()
    evidence = {
        "command": [str(venv_bin / "mypy"), "--strict", "verdict"],
        "exit_code": result.returncode,
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
            reason=f"Exit code {result.returncode}",
            duration_seconds=duration,
            evidence=evidence,
        )


def step_build(repo_path: Path) -> StepResult:
    """Build into an empty directory; copy only outputs from this invocation."""
    start = datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory(prefix="cert-build-") as tmpdir:
        command = ["uv", "build", "--out-dir", tmpdir]
        result = run_command(command, cwd=repo_path)
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
            "artifacts": artifacts,
        }
    if result.returncode != 0:
        status: Literal["PASS", "FAIL", "SKIPPED", "INCOMPLETE"] = "FAIL"
        reason = f"Exit code {result.returncode}"
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
            commands.append({"command": display, "exit_code": result.returncode})
            if result.returncode != 0:
                return StepResult(
                    "package_smoke",
                    "Package smoke test",
                    "FAIL",
                    f"{display[0]} exit code {result.returncode}",
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
            evidence=evidence,
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
                evidence=evidence,
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
        "command": [str(venv_bin / "python"), str(doc_script)],
        "exit_code": result.returncode,
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
            reason=f"Exit code {result.returncode}",
            duration_seconds=duration,
            evidence=evidence,
        )


def step_git_clean(repo_path: Path) -> StepResult:
    """Verify git status is clean after all steps."""
    result = run_command(["git", "status", "--porcelain"], cwd=repo_path)

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


def step_rehearsals(
    repo_path: Path, rehearsal_dirs: dict[str, Path], output_dir: Path
) -> StepResult:
    """Copy and verify rehearsal run data."""
    if not rehearsal_dirs:
        return StepResult(
            step_id="rehearsals",
            name="Rehearsal verification",
            status="SKIPPED",
            reason="Live rehearsal requires gateway credentials (none provided)",
        )

    rehearsal_output = output_dir / "rehearsals"
    rehearsal_output.mkdir(exist_ok=True)

    verified_count = 0
    for name, run_dir in rehearsal_dirs.items():
        if not run_dir.exists():
            return StepResult(
                step_id="rehearsals",
                name="Rehearsal verification",
                status="FAIL",
                reason=f"Rehearsal directory not found: {run_dir}",
            )

        # Copy events.jsonl and receipt.json
        target_dir = rehearsal_output / name
        target_dir.mkdir(exist_ok=True)

        events_src = run_dir / "events.jsonl"
        receipt_src = run_dir / "receipt.json"

        if not events_src.exists() or not receipt_src.exists():
            return StepResult(
                step_id="rehearsals",
                name="Rehearsal verification",
                status="FAIL",
                reason=f"Missing events.jsonl or receipt.json in {name}",
            )

        shutil.copy2(events_src, target_dir / "events.jsonl")
        shutil.copy2(receipt_src, target_dir / "receipt.json")

        # Verify events digest
        events_sha = sha256_file(events_src)
        receipt_data = json.loads(receipt_src.read_text())
        expected_digest = receipt_data.get("events_digest", "")

        if f"sha256:{events_sha}" != expected_digest:
            return StepResult(
                step_id="rehearsals",
                name="Rehearsal verification",
                status="FAIL",
                reason=f"Events digest mismatch in {name}",
            )

        verified_count += 1

    return StepResult(
        step_id="rehearsals",
        name="Rehearsal verification",
        status="PASS",
        reason=f"Verified {verified_count} rehearsal(s)",
    )


# === Main Certification Flow ===


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


def run_certification(
    repo_path: Path, *, allow_dirty: bool = False, rehearsal_dirs: dict[str, Path] | None = None
) -> tuple[CertificationManifest, dict[str, Any]]:
    """Run full certification and return manifest and detailed results."""
    manifest = CertificationManifest()
    manifest.started_at = utc_timestamp()

    # Check git state
    manifest.git_sha = get_git_sha(repo_path)
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

    # Run all steps
    steps = []

    print("Running certification steps...")

    # Test steps
    print("  [1/11] Test suite (clean shell)...")
    steps.append(step_test_clean_shell(repo_path, venv_bin))

    print("  [2/11] Test suite (dirty shell)...")
    steps.append(step_test_dirty_shell(repo_path, venv_bin))

    # Lint/type steps
    print("  [3/11] Ruff lint...")
    steps.append(step_ruff_check(repo_path, venv_bin))

    print("  [4/11] Ruff format check...")
    steps.append(step_ruff_format(repo_path, venv_bin))

    print("  [5/11] Mypy strict...")
    steps.append(step_mypy(repo_path, venv_bin))

    # Build step
    print("  [6/11] Package build...")
    steps.append(step_build(repo_path))

    # Package smoke test
    print("  [7/11] Package smoke test...")
    build_step = steps[-1]
    wheels = [
        a["name"] for a in build_step.evidence.get("artifacts", []) if a["name"].endswith(".whl")
    ]
    steps.append(
        step_package_smoke(
            repo_path, wheels[0] if build_step.status == "PASS" and len(wheels) == 1 else None
        )
    )

    # Security
    print("  [8/11] Security checks...")
    steps.append(step_security(repo_path, venv_bin))

    # Docs
    print("  [9/11] Documentation link check...")
    steps.append(step_docs_check(repo_path, venv_bin))

    # Git clean
    print("  [10/11] Git clean check...")
    steps.append(step_git_clean(repo_path))

    # Rehearsals
    print("  [11/11] Rehearsal verification...")
    output_dir = repo_path / "artifacts" / "certification" / manifest.git_sha
    output_dir.mkdir(parents=True, exist_ok=True)
    steps.append(step_rehearsals(repo_path, rehearsal_dirs or {}, output_dir))

    manifest.steps = steps
    manifest.finished_at = utc_timestamp()

    # Determine verdict
    manifest.verdict = compute_verdict(steps, git_dirty=manifest.git_dirty)

    # Collect detailed results
    detailed = {"manifest": manifest.to_dict(), "environment": env_snapshot.to_dict()}

    return manifest, detailed


def generate_certification_md(
    manifest: CertificationManifest, env: EnvironmentSnapshot | dict[str, Any]
) -> str:
    """Generate CERTIFICATION.md from manifest and environment data."""
    # Normalize env to dict
    env_dict = env if isinstance(env, dict) else env.to_dict()

    lines = [
        f"# Release Certification: {manifest.git_sha}",
        "",
        f"**Verdict**: {manifest.verdict}",
        f"**Started**: {manifest.started_at}",
        f"**Finished**: {manifest.finished_at}",
        f"**Git SHA**: {manifest.git_sha}",
        f"**Git Dirty**: {manifest.git_dirty}",
        "",
        "## Environment",
        "",
        f"- **Python**: {env_dict['python_version']}",
        f"- **Platform**: {env_dict['platform']}",
        f"- **uv**: {env_dict['uv_version']}",
    ]

    if env_dict.get("node_version"):
        lines.append(f"- **Node**: {env_dict['node_version']}")

    lines.extend(
        [
            f"- **Lockfile SHA256**: {env_dict['lockfile_sha256'][:16]}...",
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
        lines.append(f"| {step.name} | {step.status} | {duration_str} | {reason_str} |")

    lines.extend(
        [
            "",
            "## Normalization Notes",
            "",
            "The following fields are normalized and may differ between deterministic runs:",
            "",
            "- Timestamps (started_at, finished_at)",
            "- Duration measurements",
            "- Temporary file paths",
            "- Environment variable list (OS-dependent)",
            "",
            "All other fields should be identical for the same source SHA and inputs.",
        ]
    )

    return "\n".join(lines)


def write_bundle(
    output_dir: Path,
    manifest: CertificationManifest,
    env_snapshot: EnvironmentSnapshot | dict[str, Any],
) -> None:
    """Write all bundle files to output directory."""
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

    # git-clean.txt (final git status)
    repo_path = output_dir.parent.parent.parent  # Go up to repo root
    git_status = run_command(["git", "status", "--porcelain"], cwd=repo_path)
    (output_dir / "git-clean.txt").write_text(git_status.stdout)

    print(f"\nCertification bundle written to: {output_dir}")


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Generate immutable SHA-bound certification bundles for verdict-core"
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Allow certification on dirty git tree (forces INCOMPLETE verdict)",
    )
    parser.add_argument(
        "--rehearsal",
        action="append",
        metavar="NAME=PATH",
        help="Add rehearsal run directory (e.g., clean=/path/to/run, chaos=/path/to/run)",
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

    # Run certification
    manifest, detailed = run_certification(
        repo_path, allow_dirty=args.allow_dirty, rehearsal_dirs=rehearsal_dirs
    )

    # Write bundle
    output_dir = args.output_dir or (repo_path / "artifacts" / "certification" / manifest.git_sha)

    write_bundle(output_dir, manifest, detailed["environment"])

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
