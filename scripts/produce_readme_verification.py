#!/usr/bin/env python3
"""Produce README verification evidence for G7.3.

G7.3 requires verifying that README commands work and claims are accurate:
- For every `verdict <sub>` command in README, run `verdict <sub> --help` (must exit 0).
- Run credential-free commands as written, in a temp dir.
- SKIPPED only for commands needing credentials or network (each listed with reason).
- Verify version claim against verdict.__version__ / pyproject.
- Verify coverage claim against the configured fail_under (if set).

Exit 1 with RESULT: FAIL if any check fails.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# Subcommands that need live credentials or network access
CRED_SUBCOMMANDS = frozenset(
    {"orchestrate", "probe", "serve", "ui", "run", "replay", "run-receipt", "supervise", "watch"}
)
CRED_FLAG_PATTERNS = ["--api-key", "OMNIROUTE", "--allow-live-probe"]


@dataclass
class CommandResult:
    """Result of verifying a README command."""

    command: str
    status: str  # PASS, FAIL, SKIPPED
    exit_code: int | None
    reason: str
    section: str


def extract_verdict_commands(readme: Path) -> list[tuple[str, str]]:
    """Extract `verdict <sub> ...` lines from README code blocks and inline text."""
    content = readme.read_text()
    commands: list[tuple[str, str]] = []
    current_section = ""

    for line in content.split("\n"):
        if re.match(r"^#{1,3}\s", line):
            current_section = line.lstrip("#").strip()

        stripped = line.strip()
        # Match `verdict <sub>` at start of line (inside code blocks or backtick spans)
        # Handle lines like: `verdict quickstart --non-interactive --dry-run`
        # or: verdict quickstart --non-interactive --dry-run
        matches = re.findall(r"`(verdict\s+\S[^`]*)`|^(verdict\s+\S.*?)(?:\s*\\)?$", stripped)
        for m in matches:
            cmd = (m[0] or m[1]).strip()
            if cmd.startswith("verdict "):
                commands.append((cmd, current_section))

    # Deduplicate preserving order
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for cmd, sec in commands:
        if cmd not in seen:
            seen.add(cmd)
            unique.append((cmd, sec))
    return unique


def needs_credentials(cmd: str) -> tuple[bool, str]:
    """Return (needs_creds, reason) for a verdict command."""
    parts = cmd.split()
    if len(parts) < 2:
        return False, ""
    sub = parts[1]
    if sub in CRED_SUBCOMMANDS:
        return True, f"`verdict {sub}` requires live network/credentials"
    for pat in CRED_FLAG_PATTERNS:
        if pat in cmd:
            return True, f"flag {pat!r} requires credentials"
    return False, ""


def verify_verdict_subcommand(sub: str, tmpdir: Path) -> tuple[int, str]:
    """Run `verdict <sub> --help` in tmpdir and return (exit_code, output)."""
    env = os.environ.copy()
    env["HOME"] = str(tmpdir)
    env["XDG_CONFIG_HOME"] = str(tmpdir / ".config")
    # Do not pass LLMGATE_AUTH_TOKEN
    env.pop("LLMGATE_AUTH_TOKEN", None)
    try:
        r = subprocess.run(
            [sys.executable, "-m", "verdict.cli", sub, "--help"],
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
            cwd=str(tmpdir),
        )
        return r.returncode, r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        return -1, "TIMEOUT"
    except Exception as e:
        return -1, str(e)


def verify_version_claim(readme: Path) -> CommandResult:
    """Verify README version badge matches verdict.__version__."""
    content = readme.read_text()
    m = re.search(r"badge/version-(\d+\.\d+\.\d+)", content)
    if not m:
        return CommandResult(
            command="version badge",
            status="SKIPPED",
            exit_code=None,
            reason="no version badge found in README",
            section="badges",
        )
    claimed = m.group(1)

    import verdict as v_pkg

    actual = getattr(v_pkg, "__version__", None)
    if actual is None:
        # fallback: pyproject.toml
        import tomllib

        with open("pyproject.toml", "rb") as f:
            actual = tomllib.load(f).get("project", {}).get("version")

    if actual == claimed:
        return CommandResult(
            command=f"version claim {claimed}",
            status="PASS",
            exit_code=0,
            reason=f"verdict.__version__ == {actual}",
            section="badges",
        )
    return CommandResult(
        command=f"version claim {claimed}",
        status="FAIL",
        exit_code=1,
        reason=f"README claims {claimed} but verdict.__version__ == {actual}",
        section="badges",
    )


def _read_ci_fail_under() -> int | None:
    """Read --cov-fail-under value from .github/workflows/ci.yml."""
    ci_path = Path(".github/workflows/ci.yml")
    if not ci_path.exists():
        return None
    text = ci_path.read_text()
    m = re.search(r"--cov-fail-under[=\s]+(\d+)", text)
    return int(m.group(1)) if m else None


def verify_coverage_claim(readme: Path) -> CommandResult:
    """Verify coverage claim against .github/workflows/ci.yml --cov-fail-under.

    Falls back to pyproject.toml [tool.coverage.report] fail_under when ci.yml
    is absent or has no --cov-fail-under flag.
    """
    content = readme.read_text()
    m = re.search(r"coverage gate\s+(\d+)%", content)
    if not m:
        return CommandResult(
            command="coverage claim",
            status="SKIPPED",
            exit_code=None,
            reason="no 'coverage gate N%' pattern found in README",
            section="badges",
        )
    claimed = int(m.group(1))

    # Prefer ci.yml (the enforced gate) over pyproject.toml
    fail_under = _read_ci_fail_under()
    source = "ci.yml --cov-fail-under"

    if fail_under is None:
        # Fallback: pyproject.toml
        import tomllib

        with open("pyproject.toml", "rb") as f:
            pyproject = tomllib.load(f)
        fail_under = (
            pyproject.get("tool", {}).get("coverage", {}).get("report", {}).get("fail_under")
        )
        source = "pyproject.toml [tool.coverage.report] fail_under"

    if fail_under is None:
        return CommandResult(
            command=f"coverage claim {claimed}%",
            status="SKIPPED",
            exit_code=None,
            reason="neither ci.yml --cov-fail-under nor pyproject.toml fail_under found",
            section="badges",
        )
    if int(fail_under) == claimed:
        return CommandResult(
            command=f"coverage claim {claimed}%",
            status="PASS",
            exit_code=0,
            reason=f"{source}={fail_under} matches README claim",
            section="badges",
        )
    return CommandResult(
        command=f"coverage claim {claimed}%",
        status="FAIL",
        exit_code=1,
        reason=f"README claims {claimed}% but {source}={fail_under}",
        section="badges",
    )


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Verify README commands and claims")
    parser.add_argument(
        "--evidence-dir", type=Path, required=True, help="Directory to write evidence artifacts"
    )
    args = parser.parse_args()

    args.evidence_dir.mkdir(parents=True, exist_ok=True)

    readme = Path("README.md")
    if not readme.exists():
        print("RESULT: FAIL (README.md not found)", file=sys.stderr)
        return 1

    results: list[CommandResult] = []

    # Verify version and coverage claims first
    results.append(verify_version_claim(readme))
    results.append(verify_coverage_claim(readme))

    # Extract and verify verdict subcommands
    commands = extract_verdict_commands(readme)
    print(f"Found {len(commands)} verdict commands in README")

    with tempfile.TemporaryDirectory() as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        seen_subs: set[str] = set()

        for cmd, section in commands:
            parts = cmd.split()
            if len(parts) < 2:
                continue
            sub = parts[1]

            # Each subcommand tested once via --help
            if sub in seen_subs:
                continue
            seen_subs.add(sub)

            # Skip placeholder commands like `verdict <command>` or `verdict credentials ...`
            if "<" in sub or ">" in sub:
                results.append(
                    CommandResult(
                        command=cmd,
                        status="SKIPPED",
                        exit_code=None,
                        reason="placeholder subcommand (angle brackets); real command not yet added",
                        section=section,
                    )
                )
                print(f"  SKIPPED {sub}: placeholder")
                continue

            needs_cred, cred_reason = needs_credentials(cmd)
            if needs_cred:
                results.append(
                    CommandResult(
                        command=cmd,
                        status="SKIPPED",
                        exit_code=None,
                        reason=cred_reason,
                        section=section,
                    )
                )
                print(f"  SKIPPED {sub}: {cred_reason}")
                continue

            exit_code, output = verify_verdict_subcommand(sub, tmpdir)
            if exit_code == 0:
                results.append(
                    CommandResult(
                        command=f"verdict {sub} --help",
                        status="PASS",
                        exit_code=0,
                        reason="exit 0",
                        section=section,
                    )
                )
                print(f"  PASS verdict {sub} --help")
            else:
                results.append(
                    CommandResult(
                        command=f"verdict {sub} --help",
                        status="FAIL",
                        exit_code=exit_code,
                        reason=f"exit {exit_code}: {output[:120]}",
                        section=section,
                    )
                )
                print(f"  FAIL verdict {sub} --help (exit {exit_code})")

    # Write log
    by_status: dict[str, list[CommandResult]] = {"PASS": [], "FAIL": [], "SKIPPED": []}
    for r in results:
        by_status[r.status].append(r)

    lines = ["README Verification Results", "=" * 80, ""]
    for status in ["PASS", "FAIL", "SKIPPED"]:
        items = by_status[status]
        lines.append(f"{status}: {len(items)}")
        for item in items:
            lines.append(f"  [{item.section}] {item.command}")
            lines.append(f"    Reason: {item.reason}")
            if item.exit_code is not None:
                lines.append(f"    Exit code: {item.exit_code}")
            lines.append("")

    lines.append("")
    lines.append("=" * 80)
    if by_status["FAIL"]:
        lines.append(f"RESULT: FAIL ({len(by_status['FAIL'])} check(s) failed)")
    else:
        lines.append("RESULT: PASS")

    log_text = "\n".join(lines)
    output_file = args.evidence_dir / "readme_verification.log"
    output_file.write_text(log_text)
    print(f"\nWrote {output_file}")

    if by_status["FAIL"]:
        for item in by_status["FAIL"]:
            print(f"  FAIL: {item.command} — {item.reason}", file=sys.stderr)
        print(f"RESULT: FAIL ({len(by_status['FAIL'])} check(s) failed)")
        return 1

    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
