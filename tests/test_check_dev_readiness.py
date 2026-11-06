"""scripts/check_dev_readiness.py reports host drift from CI and the push gate (BOD-219).

Every test builds a throwaway git repository, an isolated HOME and a PATH that
holds only fake binaries, so nothing here reads the operator's real tools.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from scripts import check_dev_readiness as readiness
from verdict import ocr_release

ROOT = Path(__file__).resolve().parents[1]
PIN = "1.13.2"
OCR_PIN = "v1.12.9"
CI_YML = f"""\
jobs:
  lint:
    steps:
      - name: openspec validate --all --strict (pinned {PIN})
        run: npx --yes @fission-ai/openspec@{PIN} validate --all --strict --no-interactive
"""


def _write_exe(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


class Host:
    """A ready host: every check passes until a test breaks one part."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        git = shutil.which("git")
        assert git is not None, "git is required for these tests"
        self.home = tmp_path / "home"
        self.bin = tmp_path / "bin"
        self.root = tmp_path / "repo"
        self.bin.mkdir()
        (self.bin / "git").symlink_to(git)
        monkeypatch.setenv("HOME", str(self.home))
        monkeypatch.setenv("PATH", str(self.bin))
        monkeypatch.delenv(readiness.OPENSPEC_BIN_ENV, raising=False)

        self.vgate = _write_exe(self.home / "bin" / "vgate", "exit 0\n")
        self.openspec = _write_exe(self.bin / "openspec", f"echo {PIN}\n")
        self.ocr = _write_exe(
            self.bin / "ocr", f"echo 'open-code-review {OCR_PIN} (bccbc15) linux/amd64'\n"
        )
        self.ocr_sha = _sha256(self.ocr)
        # The fake ocr binary is "the pinned release": the pin is the package's seam.
        monkeypatch.setattr(readiness, "ocr_pin", lambda: (OCR_PIN, self.ocr_sha))

        self.root.mkdir()
        _git(self.root, "init", "-q", "-b", "main")
        workflows = self.root / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "ci.yml").write_text(CI_YML, encoding="utf-8")
        self.hook = _write_exe(
            self.root / ".git" / "hooks" / "pre-push", f'exec {self.vgate} --hook "$@"\n'
        )
        self.python = _write_exe(
            tmp_path / "py" / "python",
            'case "$*" in *"-m build --version"*) echo "build 1.6.1 (fake)";; *) exit 9;; esac\n',
        )

    def run(self, capsys: pytest.CaptureFixture[str], root: Path | None = None) -> tuple[int, str]:
        code = readiness.main(
            [
                "--root",
                str(root or self.root),
                "--vgate",
                str(self.vgate),
                "--python",
                str(self.python),
            ]
        )
        return code, capsys.readouterr().out


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Host:
    return Host(tmp_path, monkeypatch)


def _failures(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.startswith("FAIL")]


def test_ready_host_passes_every_check(host: Host, capsys: pytest.CaptureFixture[str]) -> None:
    code, out = host.run(capsys)
    assert code == 0, out
    assert _failures(out) == []
    for name in ("vgate", "pre-push hook", "openspec", "build", "ocr"):
        assert re.search(rf"^ok\s+{name}\b", out, re.MULTILINE), (name, out)


def test_vgate_defaults_to_home_bin(host: Host, capsys: pytest.CaptureFixture[str]) -> None:
    code = readiness.main(["--root", str(host.root), "--python", str(host.python)])
    out = capsys.readouterr().out
    assert code == 0, out
    assert str(host.vgate) in out


def test_missing_vgate_fails_with_repair(host: Host, capsys: pytest.CaptureFixture[str]) -> None:
    host.vgate.unlink()
    code, out = host.run(capsys)
    assert code == 1
    assert [line.split()[1] for line in _failures(out)] == ["vgate"]
    assert f"chmod +x {host.vgate}" in out
    assert "operator-owned" in out


def test_non_executable_vgate_fails(host: Host, capsys: pytest.CaptureFixture[str]) -> None:
    host.vgate.chmod(0o644)
    code, out = host.run(capsys)
    assert code == 1
    assert "not executable" in out
    assert f"repair: chmod +x {host.vgate}" in out


def test_missing_pre_push_hook_fails_with_exact_install_command(
    host: Host, capsys: pytest.CaptureFixture[str]
) -> None:
    host.hook.unlink()
    code, out = host.run(capsys)
    assert code == 1
    assert "pre-push hook" in _failures(out)[0]
    assert f"> {host.hook} && chmod +x {host.hook}" in out
    assert f'exec {host.vgate} --hook "$@"' in out


def test_non_executable_pre_push_hook_fails(host: Host, capsys: pytest.CaptureFixture[str]) -> None:
    host.hook.chmod(0o644)
    code, out = host.run(capsys)
    assert code == 1
    assert f"repair: chmod +x {host.hook}" in out


def test_pre_push_hook_that_skips_vgate_fails(
    host: Host, capsys: pytest.CaptureFixture[str]
) -> None:
    host.hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    code, out = host.run(capsys)
    assert code == 1
    assert f"does not run {host.vgate}" in out


@pytest.mark.parametrize(
    "line",
    ['# exec {vgate} --hook "$@"', "exit 0  # gate: {vgate}", "echo {vgate}", "  #   {vgate}"],
    ids=["comment-line", "trailing-comment", "argument-not-command", "indented-comment"],
)
def test_pre_push_hook_that_only_mentions_vgate_fails(
    host: Host, capsys: pytest.CaptureFixture[str], line: str
) -> None:
    host.hook.write_text(f"#!/bin/sh\n{line.format(vgate=host.vgate)}\n", encoding="utf-8")
    code, out = host.run(capsys)
    assert code == 1
    assert f"does not run {host.vgate}" in out


@pytest.mark.parametrize(
    "line",
    [
        'exec {vgate} --hook "$@"',
        '{vgate} --hook "$@"',
        'exec "$HOME/bin/vgate" --hook "$@"',
        'exec "${{HOME}}/bin/vgate" --hook "$@"',
        'exec ~/bin/vgate --hook "$@"',
        '{vgate} --hook "$@" || exit 1  # push gate',
    ],
    ids=["exec-absolute", "absolute", "exec-HOME", "exec-HOME-braced", "exec-tilde", "or-exit"],
)
def test_pre_push_hook_that_executes_vgate_passes(
    host: Host, capsys: pytest.CaptureFixture[str], line: str
) -> None:
    body = f"# Verdict push gate\n{line.format(vgate=host.vgate)}\n"
    host.hook.write_text(f"#!/usr/bin/env bash\n{body}", encoding="utf-8")
    code, out = host.run(capsys)
    assert code == 0, out
    assert f"invokes {host.vgate} (static check)" in out


def test_pre_push_check_is_static_and_passes_a_dead_branch(
    host: Host, capsys: pytest.CaptureFixture[str]
) -> None:
    """Documented limitation: the check reads lines, it does not run the hook.

    A hook whose vgate line can never run still passes, labelled as a static
    check. vgate itself is the enforcement point; this is a readiness hint.
    """
    host.hook.write_text(
        f'#!/usr/bin/env bash\nif false; then\n  exec {host.vgate} --hook "$@"\nfi\n',
        encoding="utf-8",
    )
    code, out = host.run(capsys)
    assert code == 0, out
    assert re.search(r"^ok\s+pre-push hook .*\(static check\)$", out, re.MULTILINE), out


def test_help_states_the_static_hook_limitation(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        readiness.main(["--help"])
    assert caught.value.code == 0
    text = " ".join(capsys.readouterr().out.split())
    assert "static" in text and "cannot prove the hook reaches vgate" in text
    assert "environment variables only" in text


def test_hook_found_through_a_linked_worktree(
    host: Host, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Worktrees share the common git dir's hooks, as the operator's do."""
    _git(host.root, "commit", "-q", "--allow-empty", "-m", "x")
    worktree = tmp_path / "wt"
    _git(host.root, "worktree", "add", "-q", str(worktree))
    (worktree / ".github").mkdir()
    shutil.copytree(host.root / ".github" / "workflows", worktree / ".github" / "workflows")
    code, out = host.run(capsys, root=worktree)
    assert code == 0, out
    assert str(host.hook) in out


def test_openspec_version_drift_fails_with_pin(
    host: Host, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_exe(host.openspec, "echo 1.16.0\n")
    code, out = host.run(capsys)
    assert code == 1
    assert "1.16.0" in out and PIN in out
    assert f"npm install -g @fission-ai/openspec@{PIN}" in out


def test_missing_openspec_fails(host: Host, capsys: pytest.CaptureFixture[str]) -> None:
    host.openspec.unlink()
    code, out = host.run(capsys)
    assert code == 1
    assert "openspec" in _failures(out)[0]
    assert f"npm install -g @fission-ai/openspec@{PIN}" in out


def test_missing_build_fails_with_sync_hint(host: Host, capsys: pytest.CaptureFixture[str]) -> None:
    _write_exe(host.python, "echo 'No module named build' >&2; exit 1\n")
    code, out = host.run(capsys)
    assert code == 1
    assert "build" in _failures(out)[0]
    assert "No module named build" in out
    assert "uv sync --frozen --extra dev --extra server --extra dashboard" in out


def test_missing_ocr_fails_with_pinned_download(
    host: Host, capsys: pytest.CaptureFixture[str]
) -> None:
    host.ocr.unlink()
    code, out = host.run(capsys)
    assert code == 1
    assert "ocr" in _failures(out)[0]
    assert f"releases/download/{OCR_PIN}/opencodereview-linux-amd64" in out
    assert host.ocr_sha in out
    assert str(host.home / ".local" / "bin" / "ocr") in out


def test_ocr_version_drift_fails_and_repairs_in_place(
    host: Host, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_exe(host.ocr, "echo 'open-code-review v1.13.0 (x) linux/amd64'\n")
    code, out = host.run(capsys)
    assert code == 1
    assert "v1.13.0" in out and OCR_PIN in out
    assert f"mv {host.ocr}.download {host.ocr}" in out


def test_ocr_with_pinned_version_but_other_digest_fails(
    host: Host, capsys: pytest.CaptureFixture[str]
) -> None:
    """The version string is not identity: the binary must be the pinned release."""
    _write_exe(host.ocr, f"echo 'open-code-review {OCR_PIN} (bccbc15) linux/amd64'  # rebuilt\n")
    actual = _sha256(host.ocr)
    assert actual != host.ocr_sha
    code, out = host.run(capsys)
    assert code == 1
    assert [line.split()[1] for line in _failures(out)] == ["ocr"]
    assert f"sha256 {actual}" in out and host.ocr_sha in out
    assert f"mv {host.ocr}.download {host.ocr}" in out


def test_ocr_check_reads_the_pin_without_the_rehearsal_workflow(
    host: Host, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ocr pin comes from the package, never from ``--root``'s ``.github/``."""
    monkeypatch.setattr(readiness, "ocr_pin", ocr_release.ocr_pin)
    version, digest = ocr_release.ocr_pin()
    (host.root / ".github" / "workflows" / "certify-rehearsal.yml").unlink(missing_ok=True)
    host.ocr.unlink()
    code, out = host.run(capsys)
    assert code == 1
    assert [line.split()[1] for line in _failures(out)] == ["ocr"]
    assert f"releases/download/{version}/opencodereview-linux-amd64" in out
    assert digest in out


def _snapshot(base: Path) -> dict[str, tuple[int, int, str]]:
    state: dict[str, tuple[int, int, str]] = {}
    for path in sorted(base.rglob("*")):
        info = path.lstat()
        digest = ""
        if stat.S_ISREG(info.st_mode):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        state[str(path.relative_to(base))] = (info.st_mode, info.st_mtime_ns, digest)
    return state


def test_check_never_modifies_anything(
    host: Host, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    host.vgate.chmod(0o644)
    host.hook.chmod(0o644)
    host.ocr.unlink()
    _write_exe(host.openspec, "echo 1.16.0\n")
    before = _snapshot(tmp_path)
    code, out = host.run(capsys)
    assert code == 1
    assert len(_failures(out)) == 4
    assert _snapshot(tmp_path) == before


# --- the CI pin is the single source of truth -------------------------------


def test_pins_are_read_from_the_real_workflows() -> None:
    pin = readiness.openspec_pin(ROOT)
    assert re.fullmatch(r"\d+\.\d+\.\d+", pin)
    lint = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert f"npx --yes @fission-ai/openspec@{pin} validate --all --strict" in lint
    version, digest = readiness.ocr_pin()
    assert re.fullmatch(r"v\d+\.\d+\.\d+", version)
    assert re.fullmatch(r"[0-9a-f]{64}", digest)


def test_openspec_pin_must_be_unambiguous(tmp_path: Path) -> None:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        CI_YML + "      - run: npx --yes @fission-ai/openspec@1.16.0 --version\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="exactly one"):
        readiness.openspec_pin(tmp_path)


_PIN_TREES = (".github", "scripts", "verdict")
_SKIPPED_PARTS = {"openspec_vendor", "node_modules", "__pycache__"}


def _pin_drift(root: Path, pin: str) -> dict[str, str]:
    """Map each text file under .github/, scripts/ and verdict/ to a non-CI OpenSpec pin."""
    pattern = re.compile(r"@fission-ai/openspec@(\d+\.\d+\.\d+)")
    drift: dict[str, str] = {}
    for tree in _PIN_TREES:
        for path in (root / tree).rglob("*"):
            if not path.is_file() or _SKIPPED_PARTS & set(path.parts):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for found in pattern.findall(text):
                if found != pin:
                    drift[path.relative_to(root).as_posix()] = found
    return drift


def test_every_openspec_pin_matches_ci() -> None:
    """Every text file under .github/, scripts/ and verdict/ names the CI pin.

    That covers workflow ``*.yml`` and ``*.yaml``, scripts in any language and
    product code. Vendored code, ``node_modules`` and caches are skipped; docs
    and test fixtures are prose, not runners, and are out of scope.
    """
    pin = readiness.openspec_pin(ROOT)
    drift = _pin_drift(ROOT, pin)
    assert drift == {}, f"OpenSpec pins differ from ci.yml ({pin}): {drift}"


def test_pin_scan_covers_yaml_workflows_and_every_script_language(tmp_path: Path) -> None:
    stale = "npx --yes @fission-ai/openspec@1.16.0 validate\n"
    for relative in (
        ".github/workflows/drill.yaml",
        ".github/workflows/lint.yml",
        "scripts/check.mjs",
        "scripts/tool.sh",
        "verdict/lifecycle.py",
        "verdict/openspec_vendor/vendored.py",
        "docs/LIFECYCLE.md",
    ):
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_text(stale, encoding="utf-8")
    assert _pin_drift(tmp_path, PIN) == {
        ".github/workflows/drill.yaml": "1.16.0",
        ".github/workflows/lint.yml": "1.16.0",
        "scripts/check.mjs": "1.16.0",
        "scripts/tool.sh": "1.16.0",
        "verdict/lifecycle.py": "1.16.0",
    }


# --- pinned_openspec_command: what the strict-validation test runs ------------


def _npx_trap(host: Host, tmp_path: Path) -> Path:
    marker = tmp_path / "npx-was-called"
    _write_exe(host.bin / "npx", f"touch {marker}; exit 1\n")
    return marker


def test_pinned_command_uses_path_cli_when_it_matches(host: Host) -> None:
    assert readiness.pinned_openspec_command(host.root) == [str(host.openspec)]


def test_pinned_command_is_none_without_any_cli(host: Host) -> None:
    host.openspec.unlink()
    assert readiness.pinned_openspec_command(host.root) is None


def test_pinned_command_fails_on_version_drift_and_never_downloads(
    host: Host, tmp_path: Path
) -> None:
    _write_exe(host.openspec, "echo 1.16.0\n")
    marker = _npx_trap(host, tmp_path)
    with pytest.raises(readiness.PinnedCliUnavailableError) as caught:
        readiness.pinned_openspec_command(host.root)
    message = str(caught.value)
    assert "1.16.0" in message and PIN in message
    assert f"npm install -g @fission-ai/openspec@{PIN}" in message
    assert f"{readiness.OPENSPEC_BIN_ENV}=" in message
    assert not marker.exists(), "the pinned-CLI lookup must never run npx"


def test_openspec_bin_override_wins_over_path(
    host: Host, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_exe(host.openspec, "echo 1.16.0\n")
    pinned = _write_exe(tmp_path / "pinned" / "openspec", f"echo {PIN}\n")
    monkeypatch.setenv(readiness.OPENSPEC_BIN_ENV, str(pinned))
    assert readiness.pinned_openspec_command(host.root) == [str(pinned)]


@pytest.mark.parametrize("reports", ["1.16.0", None], ids=["wrong-version", "missing-binary"])
def test_openspec_bin_override_that_is_not_the_pin_fails(
    host: Host, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reports: str | None
) -> None:
    """An explicit override is never silently replaced by the PATH binary."""
    override = tmp_path / "other" / "openspec"
    if reports is not None:
        _write_exe(override, f"echo {reports}\n")
    monkeypatch.setenv(readiness.OPENSPEC_BIN_ENV, str(override))
    with pytest.raises(readiness.PinnedCliUnavailableError) as caught:
        readiness.pinned_openspec_command(host.root)
    assert f"{readiness.OPENSPEC_BIN_ENV}={override}" in str(caught.value)


def test_readiness_check_honours_openspec_bin(
    host: Host, capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host.openspec.unlink()
    pinned = _write_exe(tmp_path / "pinned" / "openspec", f"echo {PIN}\n")
    monkeypatch.setenv(readiness.OPENSPEC_BIN_ENV, str(pinned))
    code, out = host.run(capsys)
    assert code == 0, out
    assert str(pinned) in out


SECRET_NAMES = (
    "VERDICT_OMNIROUTE_API_KEY",
    "GITHUB_TOKEN",
    "npm_config__authToken",
    "AWS_SECRET_ACCESS_KEY",
    "PGPASSWORD",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "OPENAI_BASE_URL",
    "ANTHROPIC_MODEL",
    "OMNIROUTE_URL",
    # npm/netrc settings can point a CLI at credential files or registries.
    "npm_config_registry",
    "NPM_CONFIG_USERCONFIG",
    "NETRC",
)


def test_probes_drop_credentials_from_the_environment(
    host: Host, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in SECRET_NAMES:
        monkeypatch.setenv(name, "must-not-leak")
    monkeypatch.setenv("VERDICT_PLAIN", "1")
    seen = tmp_path / "env.txt"
    _write_exe(host.openspec, f"export -p > {seen}; echo {PIN}\n")
    readiness.pinned_openspec_command(host.root)
    exported = seen.read_text(encoding="utf-8")
    names = set(re.findall(r"^export (\w+)", exported, re.MULTILINE))
    assert names.isdisjoint(SECRET_NAMES), sorted(names & set(SECRET_NAMES))
    assert "must-not-leak" not in exported
    assert {"PATH", "HOME", "VERDICT_PLAIN", "OPENSPEC_TELEMETRY"} <= names
    assert re.search(r"^export OPENSPEC_TELEMETRY=['\"]?0['\"]?$", exported, re.MULTILINE)
