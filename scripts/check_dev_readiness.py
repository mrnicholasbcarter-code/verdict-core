#!/usr/bin/env python3
"""Read-only check that this host runs the tools CI and the push gate expect (BOD-219).

Reports, and never changes:

* ``vgate``, the operator push gate, exists and is executable;
* the ``pre-push`` hook (found with ``git rev-parse --git-path hooks``, so a
  linked worktree reports the shared hook) exists, is executable and runs vgate;
* the ``openspec`` on PATH reports the version the CI ``lint`` job pins in
  ``.github/workflows/ci.yml``;
* the PyPA ``build`` frontend that the proof contract's ``build`` gate runs
  (``python -m build --wheel``) works in the project interpreter;
* ``ocr`` (open-code-review) on PATH reports the version
  ``.github/workflows/certify-rehearsal.yml`` pins.

The workflows are the single source of truth for both pins: this script and
``tests/test_check_openspec_tasks.py`` read them, and nothing else restates them.
Every problem prints the exact repair. Exit 0 when ready, 1 on any problem.

Usage:
    .venv/bin/python scripts/check_dev_readiness.py [--root PATH] [--vgate PATH]
                                                    [--python PATH]
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = ".github/workflows/ci.yml"
REHEARSAL_WORKFLOW = ".github/workflows/certify-rehearsal.yml"
OPENSPEC_PACKAGE = "@fission-ai/openspec"
OCR_ASSET_URL = (
    "https://github.com/alibaba/open-code-review/releases/download/"
    "{version}/opencodereview-linux-amd64"
)
SYNC_COMMAND = "uv sync --frozen --extra dev --extra server --extra dashboard"
PROBE_TIMEOUT_SECONDS = 60
NPX_TIMEOUT_SECONDS = 600
_VERSION = re.compile(r"\bv?(\d+\.\d+\.\d+)\b")


class PinnedCliUnavailableError(RuntimeError):
    """The OpenSpec CLI version CI pins cannot run on this host."""


@dataclass(frozen=True)
class Finding:
    """One readiness check: what was found and, on a problem, the exact repair."""

    name: str
    ok: bool
    detail: str
    repair: str = ""


def openspec_pin(root: Path = ROOT) -> str:
    """Return the OpenSpec CLI version the CI ``lint`` job runs."""
    text = (root / CI_WORKFLOW).read_text(encoding="utf-8")
    pins: list[str] = sorted(
        set(re.findall(re.escape(OPENSPEC_PACKAGE) + r"@(\d+\.\d+\.\d+)\b", text))
    )
    if len(pins) != 1:
        raise ValueError(
            f"{CI_WORKFLOW} must pin exactly one {OPENSPEC_PACKAGE} version, found {pins or 'none'}"
        )
    return pins[0]


def ocr_pin(root: Path = ROOT) -> tuple[str, str]:
    """Return the ``ocr`` release tag and binary SHA-256 the rehearsal workflow installs."""
    text = (root / REHEARSAL_WORKFLOW).read_text(encoding="utf-8")
    version = re.search(r"^\s*OCR_VERSION:\s*['\"]?(v\d+\.\d+\.\d+)['\"]?\s*$", text, re.MULTILINE)
    digest = re.search(r"^\s*OCR_SHA256:\s*['\"]?([0-9a-f]{64})['\"]?\s*$", text, re.MULTILINE)
    if version is None or digest is None:
        raise ValueError(f"{REHEARSAL_WORKFLOW} must set OCR_VERSION and OCR_SHA256")
    return version.group(1), digest.group(1)


def _run(
    argv: Sequence[str], *, cwd: Path | None = None, timeout: float = PROBE_TIMEOUT_SECONDS
) -> subprocess.CompletedProcess[str] | None:
    env = {**os.environ, "OPENSPEC_TELEMETRY": "0"}
    try:
        return subprocess.run(
            list(argv),
            cwd=cwd,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _reported_version(argv: Sequence[str], *, timeout: float = PROBE_TIMEOUT_SECONDS) -> str | None:
    """Return the first ``X.Y.Z`` that ``argv`` prints, or None if it fails."""
    result = _run(argv, timeout=timeout)
    if result is None or result.returncode != 0:
        return None
    match = _VERSION.search(f"{result.stdout}\n{result.stderr}")
    return match.group(1) if match else None


def pinned_openspec_command(root: Path = ROOT) -> list[str]:
    """Return the argv prefix that runs the OpenSpec CLI version CI pins.

    The ``openspec`` on PATH is used when it reports the pin. Otherwise the
    pinned package runs through ``npx --yes``, as the CI ``lint`` job does.
    Raises PinnedCliUnavailableError, naming the repair, when neither reports the pin.
    """
    pin = openspec_pin(root)
    local = shutil.which("openspec")
    found = "no openspec on PATH"
    if local is not None:
        reported = _reported_version([local, "--version"])
        if reported == pin:
            return [local]
        found = f"{local} reports {reported or 'no version'}"
    package = f"{OPENSPEC_PACKAGE}@{pin}"
    npx = shutil.which("npx")
    if npx is None:
        raise PinnedCliUnavailableError(
            f"{CI_WORKFLOW} pins OpenSpec {pin}; {found} and npx is not on PATH. "
            f"Repair: install Node.js 18+ (for npx) or run: npm install -g {package}"
        )
    reported = _reported_version([npx, "--yes", package, "--version"], timeout=NPX_TIMEOUT_SECONDS)
    if reported != pin:
        raise PinnedCliUnavailableError(
            f"{CI_WORKFLOW} pins OpenSpec {pin}; {found} and `npx --yes {package} --version` "
            f"reported {reported or 'nothing'} (offline or npm registry unreachable?). "
            f"Repair: npm install -g {package}"
        )
    return [npx, "--yes", package]


def check_vgate(vgate: Path) -> Finding:
    """vgate must exist and be executable; it is operator-owned and not in this repo."""
    if not vgate.is_file():
        return Finding(
            "vgate",
            False,
            f"{vgate} is missing; pushes would skip the operator push gate",
            f"restore the operator-owned gate script to {vgate} (this repository does not "
            f"ship it), then: chmod +x {shlex.quote(str(vgate))}",
        )
    if not os.access(vgate, os.X_OK):
        return Finding(
            "vgate", False, f"{vgate} is not executable", f"chmod +x {shlex.quote(str(vgate))}"
        )
    return Finding("vgate", True, f"{vgate} is executable")


def _hooks_dir(root: Path) -> Path | None:
    result = _run(["git", "-C", str(root), "rev-parse", "--git-path", "hooks"])
    if result is None or result.returncode != 0 or not result.stdout.strip():
        return None
    hooks = Path(result.stdout.strip())
    return hooks if hooks.is_absolute() else (root / hooks).resolve()


def _vgate_spellings(vgate: Path) -> set[str]:
    spellings = {str(vgate)}
    try:
        relative = vgate.relative_to(Path.home())
    except ValueError:
        return spellings
    return spellings | {f"{home}/{relative}" for home in ("$HOME", "${HOME}", "~")}


def check_pre_push(root: Path, vgate: Path) -> Finding:
    """The pre-push hook must exist, be executable and run vgate."""
    hooks = _hooks_dir(root)
    if hooks is None:
        return Finding(
            "pre-push hook",
            False,
            f"{root} is not a git checkout (git rev-parse --git-path hooks failed)",
            "run this check from a verdict-core checkout: --root <checkout>",
        )
    hook = hooks / "pre-push"
    quoted = shlex.quote(str(hook))
    line = f'exec {shlex.quote(str(vgate))} --hook "$@"'
    install = (
        f"printf '%s\\n' '#!/usr/bin/env bash' {shlex.quote(line)} > {quoted} && chmod +x {quoted}"
    )
    if not hook.is_file():
        return Finding(
            "pre-push hook", False, f"{hook} is missing; pushes skip vgate silently", install
        )
    try:
        text = hook.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return Finding(
            "pre-push hook", False, f"{hook} is unreadable: {exc}", f"chmod u+r {quoted}"
        )
    if not any(spelling in text for spelling in _vgate_spellings(vgate)):
        return Finding(
            "pre-push hook",
            False,
            f"{hook} does not run {vgate}",
            f"{install}  (replaces the current hook)",
        )
    if not os.access(hook, os.X_OK):
        return Finding(
            "pre-push hook",
            False,
            f"{hook} is not executable; git ignores it and pushes skip vgate silently",
            f"chmod +x {quoted}",
        )
    return Finding("pre-push hook", True, f"{hook} runs {vgate}")


def check_openspec(root: Path) -> Finding:
    """The openspec on PATH must report the version ci.yml pins."""
    try:
        pin = openspec_pin(root)
    except (OSError, ValueError) as exc:
        return Finding("openspec", False, str(exc), f"fix the OpenSpec pin in {CI_WORKFLOW}")
    install = f"npm install -g {OPENSPEC_PACKAGE}@{pin}"
    local = shutil.which("openspec")
    if local is None:
        return Finding("openspec", False, f"not on PATH; {CI_WORKFLOW} pins {pin}", install)
    reported = _reported_version([local, "--version"])
    if reported != pin:
        return Finding(
            "openspec",
            False,
            f"{local} reports {reported or 'no version'}; {CI_WORKFLOW} pins {pin}",
            f"{install}, then put it ahead of {local} on PATH (or remove {local})",
        )
    return Finding("openspec", True, f"{local} reports {pin} (pinned in {CI_WORKFLOW})")


def check_build(root: Path, python: str) -> Finding:
    """The proof contract's ``build`` gate needs ``python -m build`` to run."""
    result = _run([python, "-B", "-m", "build", "--version"], cwd=root)
    if result is None or result.returncode != 0:
        reason = "could not run" if result is None else (result.stderr or result.stdout).strip()
        return Finding(
            "build",
            False,
            f"{python} -m build failed: {reason.splitlines()[-1] if reason else 'no output'}",
            f"{SYNC_COMMAND}  (build is in the dev extra), then rerun this check with the "
            "project interpreter (.venv/bin/python or uv run python)",
        )
    return Finding("build", True, f"{python} -m build: {result.stdout.strip().splitlines()[0]}")


def check_ocr(root: Path) -> Finding:
    """The ocr on PATH must report the release certify-rehearsal.yml pins."""
    try:
        version, digest = ocr_pin(root)
    except (OSError, ValueError) as exc:
        return Finding(
            "ocr", False, str(exc), f"fix OCR_VERSION/OCR_SHA256 in {REHEARSAL_WORKFLOW}"
        )
    local = shutil.which("ocr")
    target = Path(local) if local else Path.home() / ".local" / "bin" / "ocr"
    staged = shlex.quote(f"{target}.download")
    repair = (
        f"curl --fail --location --output {staged} {OCR_ASSET_URL.format(version=version)}"
        f" && printf '%s  %s\\n' {digest} {staged} | sha256sum --check --strict"
        f" && chmod +x {staged} && mv {staged} {shlex.quote(str(target))}"
    )
    if local is None:
        return Finding("ocr", False, f"not on PATH; {REHEARSAL_WORKFLOW} pins {version}", repair)
    reported = _reported_version([local, "--version"])
    if reported is None or f"v{reported}" != version:
        shown = f"v{reported}" if reported else "no version"
        return Finding(
            "ocr", False, f"{local} reports {shown}; {REHEARSAL_WORKFLOW} pins {version}", repair
        )
    return Finding("ocr", True, f"{local} reports {version} (pinned in {REHEARSAL_WORKFLOW})")


def run_checks(root: Path, vgate: Path, python: str) -> list[Finding]:
    """Run every readiness check; none of them writes anything."""
    return [
        check_vgate(vgate),
        check_pre_push(root, vgate),
        check_openspec(root),
        check_build(root, python),
        check_ocr(root),
    ]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--root", type=Path, default=ROOT, help="verdict-core checkout")
    parser.add_argument(
        "--vgate", type=Path, default=None, help="push gate script (default: ~/bin/vgate)"
    )
    parser.add_argument(
        "--python", default=sys.executable, help="project interpreter for the build check"
    )
    args = parser.parse_args(argv)
    vgate = args.vgate if args.vgate is not None else Path.home() / "bin" / "vgate"
    findings = run_checks(args.root.resolve(), vgate, args.python)
    for finding in findings:
        print(f"{'ok' if finding.ok else 'FAIL':<5} {finding.name:<14} {finding.detail}")
        if not finding.ok:
            print(f"{'':<20} repair: {finding.repair}")
    problems = sum(not finding.ok for finding in findings)
    print(f"dev readiness: {problems} problem(s)" if problems else "dev readiness: ok")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
