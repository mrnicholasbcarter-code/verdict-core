#!/usr/bin/env python3
"""Read-only check that this host runs the tools CI and the push gate expect (BOD-219).

Reports, and never changes:

* ``vgate``, the operator push gate, exists and is executable;
* the ``pre-push`` hook (found with ``git rev-parse --git-path hooks``, so a
  linked worktree reports the shared hook) exists, is executable and has a
  non-comment line that executes vgate;
* ``$OPENSPEC_BIN``, else the ``openspec`` on PATH, reports the version the CI
  ``lint`` job pins in ``.github/workflows/ci.yml``;
* the PyPA ``build`` frontend that the proof contract's ``build`` gate runs
  (``python -m build --wheel``) works in the project interpreter;
* ``ocr`` (open-code-review) on PATH reports the version, and has the SHA-256,
  that ``.github/workflows/certify-rehearsal.yml`` pins.

The workflows are the single source of truth for both pins: this script and
``tests/test_check_openspec_tasks.py`` read them, and nothing else restates them.
Nothing is downloaded, and probes run with credential variables removed from
their environment. Every problem prints the exact repair. Exit 0 when ready,
1 on any problem.

Usage:
    .venv/bin/python scripts/check_dev_readiness.py [--root PATH] [--vgate PATH]
                                                    [--python PATH]
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = ".github/workflows/ci.yml"
REHEARSAL_WORKFLOW = ".github/workflows/certify-rehearsal.yml"
OPENSPEC_PACKAGE = "@fission-ai/openspec"
OPENSPEC_BIN_ENV = "OPENSPEC_BIN"
OCR_ASSET_URL = (
    "https://github.com/alibaba/open-code-review/releases/download/"
    "{version}/opencodereview-linux-amd64"
)
SYNC_COMMAND = "uv sync --frozen --extra dev --extra server --extra dashboard"
PROBE_TIMEOUT_SECONDS = 60
_VERSION = re.compile(r"\bv?(\d+\.\d+\.\d+)\b")
# Probes never see credentials: names with these parts or prefixes are dropped.
_SECRET_NAME_PARTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
_SECRET_NAME_PREFIXES = ("OPENAI_", "ANTHROPIC_", "OMNIROUTE_")


class PinnedCliUnavailableError(RuntimeError):
    """The local OpenSpec CLI does not report the version CI pins."""

    def __init__(self, detail: str, repair: str) -> None:
        super().__init__(f"{detail}. Repair: {repair}")
        self.detail = detail
        self.repair = repair


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


def _is_secret_name(name: str) -> bool:
    upper = name.upper()
    return any(part in upper for part in _SECRET_NAME_PARTS) or upper.startswith(
        _SECRET_NAME_PREFIXES
    )


def probe_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return the probe environment: credential variables dropped, OpenSpec telemetry off."""
    source = os.environ if environ is None else environ
    env = {name: value for name, value in source.items() if not _is_secret_name(name)}
    env["OPENSPEC_TELEMETRY"] = "0"
    return env


def _run(
    argv: Sequence[str], *, cwd: Path | None = None, timeout: float = PROBE_TIMEOUT_SECONDS
) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            list(argv),
            cwd=cwd,
            capture_output=True,
            text=True,
            env=probe_env(),
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _reported_version(argv: Sequence[str]) -> str | None:
    """Return the first ``X.Y.Z`` that ``argv`` prints, or None if it fails."""
    result = _run(argv)
    if result is None or result.returncode != 0:
        return None
    match = _VERSION.search(f"{result.stdout}\n{result.stderr}")
    return match.group(1) if match else None


def _local_openspec() -> tuple[str | None, str]:
    """Return the OpenSpec binary to use and how it was chosen.

    ``$OPENSPEC_BIN`` wins and is never replaced by the PATH binary.
    """
    override = os.environ.get(OPENSPEC_BIN_ENV)
    if override:
        return override, f"{OPENSPEC_BIN_ENV}={override}"
    found = shutil.which("openspec")
    return found, f"{found} (first openspec on PATH)"


def pinned_openspec_command(root: Path = ROOT) -> list[str] | None:
    """Return the argv prefix of a local OpenSpec CLI that reports the CI pin.

    Uses ``$OPENSPEC_BIN``, else the ``openspec`` on PATH, and returns None when
    neither exists. Raises PinnedCliUnavailableError, naming the repair, when the
    chosen binary reports another version. Never downloads anything.
    """
    pin = openspec_pin(root)
    binary, source = _local_openspec()
    if binary is None:
        return None
    reported = _reported_version([binary, "--version"])
    if reported != pin:
        raise PinnedCliUnavailableError(
            f"{source} reports {reported or 'no version'}; {CI_WORKFLOW} pins OpenSpec {pin}",
            f"npm install -g {OPENSPEC_PACKAGE}@{pin} and put it first on PATH, or set "
            f"{OPENSPEC_BIN_ENV}=<path to an openspec that reports {pin}>",
        )
    return [binary]


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


def _executes_vgate(text: str, vgate: Path) -> bool:
    """True when a non-comment hook line runs vgate as its command.

    Accepts ``exec <vgate> ...`` and ``<vgate> ...`` with the absolute path or a
    ``$HOME``/``${HOME}``/``~`` spelling. A comment, or vgate as an argument
    (``echo <vgate>``), does not count.
    """
    spellings = _vgate_spellings(vgate)
    for line in text.splitlines():
        try:
            words = shlex.split(line, comments=True)
        except ValueError:
            continue
        if words[:1] == ["exec"]:
            words = words[1:]
        if words and words[0] in spellings:
            return True
    return False


def check_pre_push(root: Path, vgate: Path) -> Finding:
    """The pre-push hook must exist, be executable and execute vgate."""
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
    if not _executes_vgate(text, vgate):
        return Finding(
            "pre-push hook",
            False,
            f"{hook} does not run {vgate} (no non-comment line executes it)",
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
    """``$OPENSPEC_BIN``, else the openspec on PATH, must report the version ci.yml pins."""
    try:
        pin = openspec_pin(root)
        command = pinned_openspec_command(root)
    except PinnedCliUnavailableError as exc:
        return Finding("openspec", False, exc.detail, exc.repair)
    except (OSError, ValueError) as exc:
        return Finding("openspec", False, str(exc), f"fix the OpenSpec pin in {CI_WORKFLOW}")
    if command is None:
        return Finding(
            "openspec",
            False,
            f"not on PATH and {OPENSPEC_BIN_ENV} is unset; {CI_WORKFLOW} pins {pin}",
            f"npm install -g {OPENSPEC_PACKAGE}@{pin}",
        )
    return Finding("openspec", True, f"{command[0]} reports {pin} (pinned in {CI_WORKFLOW})")


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


def _sha256(path: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def check_ocr(root: Path) -> Finding:
    """The ocr on PATH must be the release certify-rehearsal.yml pins: version and SHA-256."""
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
    actual = _sha256(Path(local))
    if actual != digest:
        return Finding(
            "ocr",
            False,
            f"{local} reports {version} but has sha256 {actual or 'unreadable'}; "
            f"{REHEARSAL_WORKFLOW} pins {digest} (the linux/amd64 release asset)",
            repair,
        )
    return Finding("ocr", True, f"{local} is {version}, sha256 matches {REHEARSAL_WORKFLOW}")


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
