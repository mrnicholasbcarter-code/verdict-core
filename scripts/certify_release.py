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
    status: Literal["PASS", "FAIL", "SKIPPED"]
    reason: str = ""  # Details for FAIL or SKIPPED
    duration_seconds: float = 0.0


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

    # uv version
    uv_result = run_command(["uv", "--version"])
    uv_version = uv_result.stdout.strip() if uv_result.returncode == 0 else "unknown"

    # Node version (optional)
    node_result = run_command(["node", "--version"])
    node_version = node_result.stdout.strip() if node_result.returncode == 0 else ""

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


def step_test_clean_shell(repo_path: Path, venv_bin: Path) -> StepResult:
    """Run pytest in a clean shell environment."""
    start = datetime.now(timezone.utc)

    # Create clean environment with only essential vars
    clean_env = {
        "HOME": os.environ.get("HOME", ""),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PATH": str(venv_bin) + ":" + os.environ.get("PATH", ""),
    }

    # Run pytest with junit-xml output (in temp location)
    import tempfile

    junit_fd, junit_path_str = tempfile.mkstemp(suffix="-clean.xml", prefix="junit-")
    os.close(junit_fd)  # Close the file descriptor, pytest will write to the path
    junit_path = Path(junit_path_str)
    result = run_command(
        [str(venv_bin / "pytest"), f"--junitxml={junit_path}"],
        cwd=repo_path,
        env=clean_env,
        timeout=600,  # 10 minutes max
    )

    duration = (datetime.now(timezone.utc) - start).total_seconds()

    # Parse junit XML for accurate counts
    if junit_path.exists():
        tree = ET.parse(junit_path)
        root = tree.getroot()
        testsuite = root if root.tag == "testsuite" else root.find("testsuite")

        if testsuite is not None:
            passed = (
                int(testsuite.get("tests", "0"))
                - int(testsuite.get("failures", "0"))
                - int(testsuite.get("errors", "0"))
                - int(testsuite.get("skipped", "0"))
            )
            failed = int(testsuite.get("failures", "0"))
            errors = int(testsuite.get("errors", "0"))
            skipped = int(testsuite.get("skipped", "0"))
        else:
            passed = failed = errors = skipped = 0
    else:
        passed = failed = errors = skipped = 0

    if result.returncode == 0 and failed == 0 and errors == 0:
        return StepResult(
            step_id="test_clean",
            name="Test suite (clean shell)",
            status="PASS",
            reason=f"{passed} passed, {skipped} skipped",
            duration_seconds=duration,
        )
    else:
        return StepResult(
            step_id="test_clean",
            name="Test suite (clean shell)",
            status="FAIL",
            reason=f"{failed} failed, {errors} errors, {passed} passed",
            duration_seconds=duration,
        )


def step_test_dirty_shell(repo_path: Path, venv_bin: Path) -> StepResult:
    """Run pytest in a dirty shell with bogus credentials."""
    start = datetime.now(timezone.utc)

    # Create dirty environment
    dirty_env = os.environ.copy()
    dirty_env["LLMGATE_AUTH_TOKEN"] = "bogus-cert-dirty"

    import tempfile

    junit_fd, junit_path_str = tempfile.mkstemp(suffix="-dirty.xml", prefix="junit-")
    os.close(junit_fd)
    junit_path = Path(junit_path_str)
    result = run_command(
        [str(venv_bin / "pytest"), f"--junitxml={junit_path}"],
        cwd=repo_path,
        env=dirty_env,
        timeout=600,
    )

    duration = (datetime.now(timezone.utc) - start).total_seconds()

    if junit_path.exists():
        tree = ET.parse(junit_path)
        root = tree.getroot()
        testsuite = root if root.tag == "testsuite" else root.find("testsuite")

        if testsuite is not None:
            passed = (
                int(testsuite.get("tests", "0"))
                - int(testsuite.get("failures", "0"))
                - int(testsuite.get("errors", "0"))
                - int(testsuite.get("skipped", "0"))
            )
            failed = int(testsuite.get("failures", "0"))
            errors = int(testsuite.get("errors", "0"))
            skipped = int(testsuite.get("skipped", "0"))
        else:
            passed = failed = errors = skipped = 0
    else:
        passed = failed = errors = skipped = 0

    if result.returncode == 0 and failed == 0 and errors == 0:
        return StepResult(
            step_id="test_dirty",
            name="Test suite (dirty shell)",
            status="PASS",
            reason=f"{passed} passed, {skipped} skipped",
            duration_seconds=duration,
        )
    else:
        return StepResult(
            step_id="test_dirty",
            name="Test suite (dirty shell)",
            status="FAIL",
            reason=f"{failed} failed, {errors} errors, {passed} passed",
            duration_seconds=duration,
        )


def step_ruff_check(repo_path: Path, venv_bin: Path) -> StepResult:
    """Run ruff check."""
    start = datetime.now(timezone.utc)
    result = run_command([str(venv_bin / "ruff"), "check", "."], cwd=repo_path)
    duration = (datetime.now(timezone.utc) - start).total_seconds()

    if result.returncode == 0:
        return StepResult(
            step_id="ruff_check", name="Ruff lint", status="PASS", duration_seconds=duration
        )
    else:
        return StepResult(
            step_id="ruff_check",
            name="Ruff lint",
            status="FAIL",
            reason=f"Exit code {result.returncode}",
            duration_seconds=duration,
        )


def step_ruff_format(repo_path: Path, venv_bin: Path) -> StepResult:
    """Run ruff format --check."""
    start = datetime.now(timezone.utc)
    result = run_command([str(venv_bin / "ruff"), "format", "--check", "."], cwd=repo_path)
    duration = (datetime.now(timezone.utc) - start).total_seconds()

    if result.returncode == 0:
        return StepResult(
            step_id="ruff_format",
            name="Ruff format check",
            status="PASS",
            duration_seconds=duration,
        )
    else:
        return StepResult(
            step_id="ruff_format",
            name="Ruff format check",
            status="FAIL",
            reason=f"Exit code {result.returncode}",
            duration_seconds=duration,
        )


def step_mypy(repo_path: Path, venv_bin: Path) -> StepResult:
    """Run mypy --strict."""
    start = datetime.now(timezone.utc)
    result = run_command([str(venv_bin / "mypy"), "--strict", "verdict"], cwd=repo_path)
    duration = (datetime.now(timezone.utc) - start).total_seconds()

    # Count files checked from output (parse "Success: no issues found in N source files")
    import re

    match = re.search(r"Success: no issues found in (\d+) source files?", result.stdout)
    files_checked = int(match.group(1)) if match else 0

    if result.returncode == 0:
        return StepResult(
            step_id="mypy",
            name="Mypy strict type check",
            status="PASS",
            reason=f"Checked {files_checked} files" if files_checked > 0 else "",
            duration_seconds=duration,
        )
    else:
        return StepResult(
            step_id="mypy",
            name="Mypy strict type check",
            status="FAIL",
            reason=f"Exit code {result.returncode}",
            duration_seconds=duration,
        )


def step_build(repo_path: Path) -> StepResult:
    """Run uv build and record artifact checksums."""
    start = datetime.now(timezone.utc)
    result = run_command(["uv", "build"], cwd=repo_path)
    duration = (datetime.now(timezone.utc) - start).total_seconds()

    if result.returncode == 0:
        # Find built artifacts
        dist_dir = repo_path / "dist"
        artifacts = []
        if dist_dir.exists():
            for artifact in dist_dir.glob("*"):
                if artifact.is_file():
                    sha = sha256_file(artifact)
                    artifacts.append(f"{artifact.name} (sha256:{sha[:16]}...)")

        return StepResult(
            step_id="build",
            name="Package build",
            status="PASS",
            reason=f"Built {len(artifacts)} artifact(s)",
            duration_seconds=duration,
        )
    else:
        return StepResult(
            step_id="build",
            name="Package build",
            status="FAIL",
            reason=f"Exit code {result.returncode}",
            duration_seconds=duration,
        )


def step_package_smoke(repo_path: Path) -> StepResult:
    """Install built wheel in fresh venv and run verdict --help."""
    start = datetime.now(timezone.utc)

    # Find the wheel
    dist_dir = repo_path / "dist"
    wheels = list(dist_dir.glob("*.whl"))
    if not wheels:
        return StepResult(
            step_id="package_smoke",
            name="Package smoke test",
            status="FAIL",
            reason="No wheel found to test",
            duration_seconds=0.0,
        )

    wheel = wheels[0]

    # Create temporary venv
    with tempfile.TemporaryDirectory() as tmpdir:
        venv_path = Path(tmpdir) / "smoke-venv"

        # Create venv using uv
        result = run_command(["uv", "venv", str(venv_path)])
        if result.returncode != 0:
            return StepResult(
                step_id="package_smoke",
                name="Package smoke test",
                status="FAIL",
                reason="Failed to create test venv",
                duration_seconds=0.0,
            )

        # Install wheel using uv (pip isn't in uv venvs by default)
        venv_bin = venv_path / "bin"
        result = run_command(
            ["uv", "pip", "install", "--python", str(venv_bin / "python"), str(wheel)]
        )
        if result.returncode != 0:
            return StepResult(
                step_id="package_smoke",
                name="Package smoke test",
                status="FAIL",
                reason="Failed to install wheel",
                duration_seconds=0.0,
            )

        # Run verdict --help
        result = run_command([str(venv_bin / "verdict"), "--help"])
        duration = (datetime.now(timezone.utc) - start).total_seconds()

        if result.returncode == 0 and "usage:" in result.stdout.lower():
            return StepResult(
                step_id="package_smoke",
                name="Package smoke test",
                status="PASS",
                duration_seconds=duration,
            )
        else:
            return StepResult(
                step_id="package_smoke",
                name="Package smoke test",
                status="FAIL",
                reason=f"verdict --help exit code {result.returncode}",
                duration_seconds=duration,
            )


def step_security(repo_path: Path, venv_bin: Path) -> StepResult:
    """Run security checks if available in dev deps."""
    start = datetime.now(timezone.utc)

    # Check if bandit is available
    try:
        bandit_check = run_command([str(venv_bin / "bandit"), "--version"])
    except FileNotFoundError:
        return StepResult(
            step_id="security",
            name="Security checks",
            status="SKIPPED",
            reason="bandit not available in dev dependencies",
        )

    if bandit_check.returncode != 0:
        return StepResult(
            step_id="security",
            name="Security checks",
            status="SKIPPED",
            reason="bandit not available in dev dependencies",
        )

    # Run bandit
    result = run_command([str(venv_bin / "bandit"), "-r", "verdict", "-f", "json"], cwd=repo_path)
    duration = (datetime.now(timezone.utc) - start).total_seconds()

    if result.returncode == 0:
        try:
            bandit_data = json.loads(result.stdout)
            issues = len(bandit_data.get("results", []))
            return StepResult(
                step_id="security",
                name="Security checks",
                status="PASS" if issues == 0 else "FAIL",
                reason=f"{issues} issue(s) found" if issues > 0 else "",
                duration_seconds=duration,
            )
        except json.JSONDecodeError:
            return StepResult(
                step_id="security",
                name="Security checks",
                status="FAIL",
                reason="Failed to parse bandit output",
                duration_seconds=duration,
            )
    else:
        return StepResult(
            step_id="security",
            name="Security checks",
            status="FAIL",
            reason=f"Exit code {result.returncode}",
            duration_seconds=duration,
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

    if result.returncode == 0:
        return StepResult(
            step_id="docs_check",
            name="Documentation link check",
            status="PASS",
            duration_seconds=duration,
        )
    else:
        return StepResult(
            step_id="docs_check",
            name="Documentation link check",
            status="FAIL",
            reason=f"Exit code {result.returncode}",
            duration_seconds=duration,
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
    steps.append(step_package_smoke(repo_path))

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
    has_failures = any(s.status == "FAIL" for s in steps)
    has_skipped_rehearsals = any(s.step_id == "rehearsals" and s.status == "SKIPPED" for s in steps)

    if has_failures:
        manifest.verdict = "FAILED"
    elif manifest.git_dirty or has_skipped_rehearsals:
        manifest.verdict = "INCOMPLETE"
    else:
        manifest.verdict = "CERTIFIED"

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
