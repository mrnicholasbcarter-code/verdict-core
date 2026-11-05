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

ROOT = Path(__file__).resolve().parents[1]
PIN = "1.13.2"
OCR_PIN = "v1.12.9"
OCR_SHA = "ab" * 32
CI_YML = f"""\
jobs:
  lint:
    steps:
      - name: openspec validate --all --strict (pinned {PIN})
        run: npx --yes @fission-ai/openspec@{PIN} validate --all --strict --no-interactive
"""
REHEARSAL_YML = f"""\
jobs:
  rehearse:
    env:
      OCR_VERSION: {OCR_PIN}
      OCR_SHA256: {OCR_SHA}
"""


def _write_exe(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


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

        self.root.mkdir()
        _git(self.root, "init", "-q", "-b", "main")
        workflows = self.root / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "ci.yml").write_text(CI_YML, encoding="utf-8")
        (workflows / "certify-rehearsal.yml").write_text(REHEARSAL_YML, encoding="utf-8")

        self.vgate = _write_exe(self.home / "bin" / "vgate", "exit 0\n")
        self.hook = _write_exe(
            self.root / ".git" / "hooks" / "pre-push", f'exec {self.vgate} --hook "$@"\n'
        )
        self.openspec = _write_exe(self.bin / "openspec", f"echo {PIN}\n")
        self.ocr = _write_exe(
            self.bin / "ocr", f"echo 'open-code-review {OCR_PIN} (bccbc15) linux/amd64'\n"
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
    assert OCR_SHA in out
    assert str(host.home / ".local" / "bin" / "ocr") in out


def test_ocr_version_drift_fails_and_repairs_in_place(
    host: Host, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_exe(host.ocr, "echo 'open-code-review v1.13.0 (x) linux/amd64'\n")
    code, out = host.run(capsys)
    assert code == 1
    assert "v1.13.0" in out and OCR_PIN in out
    assert f"mv {host.ocr}.download {host.ocr}" in out


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
    version, digest = readiness.ocr_pin(ROOT)
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


def test_every_executable_openspec_pin_matches_ci() -> None:
    """Workflows, scripts and product code run the version the lint job pins."""
    pin = readiness.openspec_pin(ROOT)
    pattern = re.compile(r"@fission-ai/openspec@(\d+\.\d+\.\d+)")
    sources = [
        *(ROOT / ".github" / "workflows").glob("*.yml"),
        *(ROOT / "scripts").rglob("*.py"),
        *(ROOT / "scripts").rglob("*.sh"),
        *(path for path in (ROOT / "verdict").rglob("*.py") if "openspec_vendor" not in path.parts),
    ]
    drift = {
        str(path.relative_to(ROOT)): found
        for path in sources
        for found in pattern.findall(path.read_text(encoding="utf-8"))
        if found != pin
    }
    assert drift == {}, f"OpenSpec pins differ from ci.yml ({pin}): {drift}"


# --- pinned_openspec_command: what the strict-validation test runs ------------


def _fake_npx(host: Host, reports: str) -> Path:
    return _write_exe(
        host.bin / "npx",
        f'[ "$*" = "--yes @fission-ai/openspec@{PIN} --version" ] && echo {reports} && exit 0\n'
        "exit 3\n",
    )


def test_pinned_command_uses_path_cli_when_it_matches(host: Host) -> None:
    assert readiness.pinned_openspec_command(host.root) == [str(host.openspec)]


def test_pinned_command_falls_back_to_npx_on_version_drift(host: Host) -> None:
    _write_exe(host.openspec, "echo 1.16.0\n")
    npx = _fake_npx(host, PIN)
    command = readiness.pinned_openspec_command(host.root)
    assert command == [str(npx), "--yes", f"@fission-ai/openspec@{PIN}"]


def test_pinned_command_fails_clearly_without_npx(host: Host) -> None:
    _write_exe(host.openspec, "echo 1.16.0\n")
    with pytest.raises(readiness.PinnedCliUnavailableError) as caught:
        readiness.pinned_openspec_command(host.root)
    message = str(caught.value)
    assert "1.16.0" in message and PIN in message and "npx" in message


def test_pinned_command_fails_when_npx_cannot_run_the_pin(host: Host) -> None:
    _write_exe(host.openspec, "echo 1.16.0\n")
    _fake_npx(host, "9.9.9")
    with pytest.raises(readiness.PinnedCliUnavailableError, match=PIN):
        readiness.pinned_openspec_command(host.root)


def test_version_probes_run_with_telemetry_off(host: Host, tmp_path: Path) -> None:
    seen = tmp_path / "env.txt"
    _write_exe(host.openspec, f'echo "$OPENSPEC_TELEMETRY" > {seen}; echo {PIN}\n')
    readiness.pinned_openspec_command(host.root)
    assert seen.read_text(encoding="utf-8").strip() == "0"
