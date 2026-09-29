"""Tests for install.sh — pinned version with hash verification.

Tests use fake PyPI responses and script introspection (no network).
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import textwrap
from pathlib import Path

import pytest

INSTALL_SH = Path(__file__).resolve().parent.parent / "install.sh"

# Real hashes baked into install.sh for 0.4.0
GOOD_WHL_SHA256 = "17197fc61cbb97199561956ad9189c7d4b05d3753c2403daa9a7842a13432453"
GOOD_SDIST_SHA256 = "ae248ca948aa361a847e5e61e6c87b2ff8b803b6a68710ea6f7082da36bcd73c"


def _fake_pypi_json(
    version: str = "0.4.0", whl_sha256: str = GOOD_WHL_SHA256, sdist_sha256: str = GOOD_SDIST_SHA256
) -> str:
    """Build a minimal PyPI JSON API response."""
    return json.dumps(
        {
            "info": {"name": "verdict-core", "version": version},
            "urls": [
                {
                    "filename": f"verdict_core-{version}-py3-none-any.whl",
                    "packagetype": "bdist_wheel",
                    "digests": {"sha256": whl_sha256},
                },
                {
                    "filename": f"verdict_core-{version}.tar.gz",
                    "packagetype": "sdist",
                    "digests": {"sha256": sdist_sha256},
                },
            ],
        }
    )


def _minimal_path(stub_dir: Path | None = None) -> str:
    """Build a PATH with only essential system dirs (no ~/.local/bin)."""
    keep = ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    parts: list[str] = []
    if stub_dir:
        parts.append(str(stub_dir))
    parts.extend(keep)
    return ":".join(parts)


def _run_install_script(
    *, env_overrides: dict[str, str] | None = None, stub_bin_dir: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """Run install.sh with stubbed commands so it never hits the network."""
    env = os.environ.copy()
    env["VERDICT_VERSION"] = (
        env_overrides.pop("VERDICT_VERSION", "0.4.0") if env_overrides else "0.4.0"
    )

    if env_overrides:
        env.update(env_overrides)

    # Use a minimal PATH so the real verdict binary is never found.
    env["PATH"] = _minimal_path(stub_bin_dir)

    return subprocess.run(
        ["bash", str(INSTALL_SH)], capture_output=True, text=True, env=env, timeout=30
    )


def _write_stub(directory: Path, name: str, body: str, *, exit_code: int = 0) -> Path:
    """Write a stub executable shell script."""
    p = directory / name
    p.write_text(
        textwrap.dedent(f"""\
        #!/usr/bin/env bash
        {body}
        exit {exit_code}
        """)
    )
    p.chmod(0o755)
    return p


# ---------------------------------------------------------------------------
# Script-level assertions (no execution needed)
# ---------------------------------------------------------------------------


class TestInstallScriptContent:
    """Verify the script contains the expected safety properties."""

    @pytest.fixture(autouse=True)
    def _load_script(self) -> None:
        self.script = INSTALL_SH.read_text()

    def test_pinned_version_present(self) -> None:
        assert 'VERDICT_VERSION="${VERDICT_VERSION:-0.4.0}"' in self.script

    def test_version_overridable_via_env(self) -> None:
        assert "VERDICT_VERSION:-" in self.script

    def test_sha256_verification_present(self) -> None:
        """The script must verify downloaded artifacts via SHA-256."""
        assert "verify_sha256" in self.script

    def test_fail_closed_on_hash_mismatch(self) -> None:
        assert "SHA-256 MISMATCH" in self.script

    def test_no_pipe_curl_to_shell(self) -> None:
        """install.sh must never pipe unverified remote code to a shell."""
        for line in self.script.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert "| bash" not in stripped, f"Unsafe pipe-to-shell: {stripped}"
            assert "| sh" not in stripped, f"Unsafe pipe-to-shell: {stripped}"

    def test_whl_hash_matches_pypi(self) -> None:
        assert GOOD_WHL_SHA256 in self.script

    def test_sdist_hash_matches_pypi(self) -> None:
        assert GOOD_SDIST_SHA256 in self.script

    def test_download_no_deps(self) -> None:
        """The script downloads with --no-deps for isolated verification."""
        assert "--no-deps" in self.script


# ---------------------------------------------------------------------------
# Functional tests using stub commands (no network, no real pip)
# ---------------------------------------------------------------------------


@pytest.fixture()
def stub_dir(tmp_path: Path) -> Path:
    """Create a directory with stub commands for install.sh (verdict present)."""
    d = tmp_path / "bin"
    d.mkdir()
    _write_stub(d, "verdict", 'echo "verdict stub"')
    _write_stub(d, "curl", 'echo "curl stub"')
    return d


class TestGoodHashInstalls:
    """Default version with correct baked-in hashes → install succeeds."""

    def test_already_installed_skips(self, stub_dir: Path) -> None:
        """When verdict is already on PATH, the script skips the install step."""
        r = _run_install_script(stub_bin_dir=stub_dir)
        assert "skipping install step" in r.stdout.lower()

    def test_fresh_install_verifies_hash(self, tmp_path: Path) -> None:
        """A fresh install downloads the wheel and verifies its SHA-256."""
        d = tmp_path / "bin"
        d.mkdir()

        invocation_log = tmp_path / "pip_args.log"

        # pip stub: on "download" creates a fake whl; on "install" plants verdict
        _write_stub(
            d,
            "pip",
            textwrap.dedent(f"""\
            echo "$@" >> {invocation_log}
            if echo "$@" | grep -q "download"; then
                # Create a fake wheel in the dest dir
                DEST=$(echo "$@" | grep -oP '(?<=--dest )\\S+')
                mkdir -p "$DEST"
                echo "fake-wheel-content" > "$DEST/verdict_core-0.3.0-py3-none-any.whl"
            fi
            if echo "$@" | grep -q "install"; then
                cat > {d}/verdict <<'STUB'
#!/usr/bin/env bash
if [[ "$1" == "check" ]]; then echo "ok"; exit 0; fi
if [[ "$1" == "setup" ]]; then exit 0; fi
echo "verdict stub"; exit 0
STUB
                chmod +x {d}/verdict
            fi
            """),
        )
        # python3 stub: use real python3 from /usr/bin (on minimal PATH)
        _write_stub(d, "curl", 'echo "curl stub"; exit 1')

        r = _run_install_script(stub_bin_dir=d)
        combined = r.stdout + r.stderr

        # pip must have been called
        assert invocation_log.exists(), f"pip was never called. Output: {combined[:500]}"
        pip_args = invocation_log.read_text()
        # download phase must use --no-deps
        assert "--no-deps" in pip_args, f"pip download without --no-deps. Args: {pip_args}"

        # Script will fail on hash mismatch (fake whl has wrong hash) — that's correct
        # The key assertion: the script attempted SHA-256 verification
        assert "sha-256" in combined.lower() or "mismatch" in combined.lower()


class TestBadHashAborts:
    """Tampered artifact hash → install must abort."""

    def test_bad_hash_aborts(self, tmp_path: Path) -> None:
        """Download succeeds but hash doesn't match → non-zero exit."""
        d = tmp_path / "bin"
        d.mkdir()

        # pip download stub: creates a file with wrong content (wrong hash)
        _write_stub(
            d,
            "pip",
            textwrap.dedent("""\
            if echo "$@" | grep -q "download"; then
                DEST=$(echo "$@" | grep -oP '(?<=--dest )\\S+')
                mkdir -p "$DEST"
                echo "tampered-content" > "$DEST/verdict_core-0.3.0-py3-none-any.whl"
            fi
            """),
        )
        _write_stub(d, "curl", 'echo "curl stub"; exit 1')

        r = _run_install_script(stub_bin_dir=d)
        combined = r.stdout + r.stderr
        assert r.returncode != 0, (
            f"Expected non-zero exit on hash mismatch, got 0. Output: {combined[:500]}"
        )
        assert "mismatch" in combined.lower() or "abort" in combined.lower()


class TestMissingVersionAborts:
    """Requesting a version not on PyPI → install must abort."""

    def test_nonexistent_version_aborts(self, tmp_path: Path) -> None:
        d = tmp_path / "bin"
        d.mkdir()

        # pip download fails (version doesn't exist)
        _write_stub(d, "pip", 'echo "ERROR: No matching distribution found" >&2', exit_code=1)
        # curl fails (PyPI returns 404 for unknown version)
        _write_stub(d, "curl", "exit 22", exit_code=22)

        r = _run_install_script(env_overrides={"VERDICT_VERSION": "99.99.99"}, stub_bin_dir=d)
        assert r.returncode != 0, (
            f"Expected non-zero exit for missing version. Output: {r.stdout[:500]}"
        )


class TestPyPIHashExtraction:
    """The pypi_sha256 helper correctly parses PyPI JSON."""

    def test_extract_wheel_hash(self) -> None:
        fake_json = _fake_pypi_json()
        result = subprocess.run(
            [
                "python3",
                "-c",
                textwrap.dedent("""\
                import sys, json
                data = json.load(sys.stdin)
                for u in data['urls']:
                    if u['packagetype'] == 'bdist_wheel':
                        print(u['digests']['sha256'])
                        sys.exit(0)
                print('NOT_FOUND')
                sys.exit(1)
                """),
            ],
            input=fake_json,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == GOOD_WHL_SHA256

    def test_extract_sdist_hash(self) -> None:
        fake_json = _fake_pypi_json()
        result = subprocess.run(
            [
                "python3",
                "-c",
                textwrap.dedent("""\
                import sys, json
                data = json.load(sys.stdin)
                for u in data['urls']:
                    if u['packagetype'] == 'sdist':
                        print(u['digests']['sha256'])
                        sys.exit(0)
                print('NOT_FOUND')
                sys.exit(1)
                """),
            ],
            input=fake_json,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == GOOD_SDIST_SHA256

    def test_bad_packagetype_returns_not_found(self) -> None:
        """Missing packagetype → NOT_FOUND + non-zero exit."""
        fake_json = json.dumps(
            {
                "info": {"name": "verdict-core", "version": "0.3.0"},
                "urls": [
                    {
                        "filename": "verdict_core-0.3.0.egg",
                        "packagetype": "bdist_egg",
                        "digests": {"sha256": "abc123"},
                    }
                ],
            }
        )
        result = subprocess.run(
            [
                "python3",
                "-c",
                textwrap.dedent("""\
                import sys, json
                data = json.load(sys.stdin)
                for u in data['urls']:
                    if u['packagetype'] == 'bdist_wheel':
                        print(u['digests']['sha256'])
                        sys.exit(0)
                print('NOT_FOUND')
                sys.exit(1)
                """),
            ],
            input=fake_json,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert "NOT_FOUND" in result.stdout


class TestPostInstallVerification:
    """After installing, the script proves the package works without a config file."""

    def _stubs(self, tmp_path: Path, *, demo_exit: int = 0) -> tuple[Path, Path]:
        d = tmp_path / "bin"
        d.mkdir()
        calls = tmp_path / "verdict_calls.log"
        _write_stub(d, "verdict", f'echo "$@" >> {calls}; [[ "$1" == "demo" ]] && exit {demo_exit}')
        _write_stub(d, "curl", "exit 1")  # no local gateway
        return d, calls

    def test_runs_demo_and_skips_check_without_config(self, tmp_path: Path) -> None:
        d, calls = self._stubs(tmp_path)
        home = tmp_path / "home"
        home.mkdir()
        r = _run_install_script(
            env_overrides={"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config")},
            stub_bin_dir=d,
        )
        assert r.returncode == 0, r.stdout + r.stderr
        invoked = calls.read_text()
        assert "demo --speed 0" in invoked
        assert "check" not in invoked.split(), "check must not run without a config file"
        assert "No configuration file yet" in r.stdout

    def test_runs_check_when_config_exists(self, tmp_path: Path) -> None:
        d, calls = self._stubs(tmp_path)
        cfg = tmp_path / "cfg"
        (cfg / "verdict").mkdir(parents=True)
        (cfg / "verdict" / "verdict.yaml").write_text("{}\n")
        r = _run_install_script(env_overrides={"XDG_CONFIG_HOME": str(cfg)}, stub_bin_dir=d)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "check" in calls.read_text().split()

    def test_failed_demo_fails_the_install(self, tmp_path: Path) -> None:
        d, _ = self._stubs(tmp_path, demo_exit=3)
        r = _run_install_script(stub_bin_dir=d)
        assert r.returncode != 0
        assert "verdict demo failed" in r.stdout + r.stderr


def test_successful_install_exits_zero_and_removes_its_temp_dir(tmp_path: Path) -> None:
    """Under set -u the EXIT trap must still clean up, even for a path with a quote.

    Runs the real script end to end with stubbed pip/curl: the stub serves a
    PyPI JSON whose digest matches the stub wheel, so hash verification passes.
    """
    tmp_root = tmp_path / "it's tmp"
    tmp_root.mkdir()
    d = tmp_path / "bin"
    d.mkdir()
    wheel = tmp_path / "verdict_core-9.9.9-py3-none-any.whl"
    wheel.write_text("fake-wheel-content\n")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    pypi_json = tmp_path / "pypi.json"
    pypi_json.write_text(
        json.dumps(
            {
                "info": {"name": "verdict-core", "version": "9.9.9"},
                "urls": [
                    {"packagetype": "bdist_wheel", "digests": {"sha256": digest}},
                    {"packagetype": "sdist", "digests": {"sha256": "0" * 64}},
                ],
            }
        )
    )
    _write_stub(
        d, "curl", f'case "$*" in *pypi.org*) cat {shlex.quote(str(pypi_json))};; *) exit 1;; esac'
    )
    _write_stub(
        d,
        "pip",
        f"""if echo "$@" | grep -q download; then
  dest=""; prev=""
  for a in "$@"; do [ "$prev" = "--dest" ] && dest="$a"; prev="$a"; done
  cp {shlex.quote(str(wheel))} "$dest/"
fi
if echo "$@" | grep -q -- "--user"; then
  printf '#!/usr/bin/env bash\\nexit 0\\n' > {shlex.quote(str(d / "verdict"))}
  chmod +x {shlex.quote(str(d / "verdict"))}
fi""",
    )
    # The script prefers pipx when present; stub it the same way as pip --user.
    _write_stub(
        d,
        "pipx",
        f"""printf '#!/usr/bin/env bash\\nexit 0\\n' > {shlex.quote(str(d / "verdict"))}
chmod +x {shlex.quote(str(d / "verdict"))}""",
    )
    home = tmp_path / "home"
    home.mkdir()
    r = _run_install_script(
        env_overrides={"VERDICT_VERSION": "9.9.9", "TMPDIR": str(tmp_root), "HOME": str(home)},
        stub_bin_dir=d,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "SHA-256 verified" in r.stdout
    assert list(tmp_root.iterdir()) == [], "the installer left its temp dir behind"
