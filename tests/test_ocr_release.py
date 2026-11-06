"""verdict/ocr_release.py: the one pinned ocr release helper (BOD-219 part 2).

The pin lives in this module and ships with the package. ``verdict setup``,
``verdict doctor``, ``scripts/check_dev_readiness.py`` and
``.github/workflows/certify-rehearsal.yml`` read it from here, and the drift
tests below fail if anything else restates it. Every test uses a temporary
directory and an injected fetcher: nothing here downloads, and nothing reads
the operator's real ``~/.local/bin``.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scripts import check_dev_readiness as readiness
from verdict import ocr_release

ROOT = Path(__file__).resolve().parents[1]
PIN = "v1.12.9"
ASSET = b"\x7fELF fake ocr release asset\n"
ASSET_SHA = hashlib.sha256(ASSET).hexdigest()


def _exe(path: Path, body: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    path.chmod(0o755)
    return path


def test_pin_is_the_package_constants() -> None:
    assert ocr_release.ocr_pin() == (ocr_release.OCR_VERSION, ocr_release.OCR_SHA256)
    assert re.fullmatch(r"v\d+\.\d+\.\d+", ocr_release.OCR_VERSION)
    assert re.fullmatch(r"[0-9a-f]{64}", ocr_release.OCR_SHA256)


def test_readiness_script_shares_the_helper() -> None:
    assert readiness.ocr_pin is ocr_release.ocr_pin
    assert readiness.OCR_ASSET_URL == ocr_release.OCR_ASSET_URL


def test_inspect_reports_missing_wrong_version_and_digest_mismatch(tmp_path: Path) -> None:
    calls: list[Path] = []

    def version_of(path: Path) -> str | None:
        calls.append(path)
        return "1.13.0" if path.name == "drifted" else "1.12.9"

    missing = ocr_release.inspect_ocr(
        None, version=PIN, digest=ASSET_SHA, reported_version=version_of
    )
    assert missing.state == "missing"
    assert missing.describe() == f"not on PATH; verdict.ocr_release pins {PIN}"

    drifted = _exe(tmp_path / "drifted", b"other build\n")
    wrong = ocr_release.inspect_ocr(
        drifted, version=PIN, digest=ASSET_SHA, reported_version=version_of
    )
    assert wrong.state == "wrong_version"
    assert "reports v1.13.0" in wrong.describe() and PIN in wrong.describe()

    rebuilt = _exe(tmp_path / "rebuilt", b"rebuilt with the pinned tag\n")
    mismatch = ocr_release.inspect_ocr(
        rebuilt, version=PIN, digest=ASSET_SHA, reported_version=version_of
    )
    assert mismatch.state == "digest_mismatch"
    assert hashlib.sha256(rebuilt.read_bytes()).hexdigest() in mismatch.describe()
    assert ASSET_SHA in mismatch.describe()


def test_pinned_binary_is_identified_by_digest_without_running_it(tmp_path: Path) -> None:
    pinned = _exe(tmp_path / "ocr", ASSET)

    def never(path: Path) -> str | None:
        raise AssertionError(f"a binary with the pinned digest must not be executed: {path}")

    found = ocr_release.inspect_ocr(pinned, version=PIN, digest=ASSET_SHA, reported_version=never)
    assert found.ok and found.state == "ok"
    assert found.describe().endswith("sha256 matches verdict.ocr_release")


def test_install_verifies_the_digest_before_writing(tmp_path: Path) -> None:
    target = tmp_path / "home" / ".local" / "bin" / "ocr"
    urls: list[str] = []

    def fetch(url: str) -> bytes:
        urls.append(url)
        return ASSET

    result = ocr_release.install_release(version=PIN, digest=ASSET_SHA, target=target, fetch=fetch)
    assert result["status"] == "installed"
    assert urls == [ocr_release.OCR_ASSET_URL.format(version=PIN)]
    assert target.read_bytes() == ASSET
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    assert result["sha256"] == ASSET_SHA
    assert [p.name for p in target.parent.iterdir()] == ["ocr"]


def test_install_never_writes_on_a_digest_mismatch(tmp_path: Path) -> None:
    target = tmp_path / "home" / ".local" / "bin" / "ocr"
    previous = _exe(target, b"previous ocr\n")
    before = previous.read_bytes()

    result = ocr_release.install_release(
        version=PIN, digest=ASSET_SHA, target=target, fetch=lambda url: b"tampered asset"
    )
    assert result["status"] == "failed"
    assert "nothing installed" in result["note"]
    assert target.read_bytes() == before
    assert sorted(p.name for p in target.parent.iterdir()) == ["ocr"]


def test_install_backs_up_a_replaced_binary(tmp_path: Path) -> None:
    target = _exe(tmp_path / "bin" / "ocr", b"old release\n")
    result = ocr_release.install_release(
        version=PIN, digest=ASSET_SHA, target=target, fetch=lambda url: ASSET
    )
    assert result["status"] == "installed"
    backup = Path(str(result["backup"]))
    assert backup.read_bytes() == b"old release\n"
    assert target.read_bytes() == ASSET


def test_download_failure_installs_nothing(tmp_path: Path) -> None:
    target = tmp_path / "bin" / "ocr"

    def fetch(url: str) -> bytes:
        raise OSError("network down")

    result = ocr_release.install_release(version=PIN, digest=ASSET_SHA, target=target, fetch=fetch)
    assert result["status"] == "failed"
    assert not target.exists()


def test_fetch_refuses_non_https_urls() -> None:
    with pytest.raises(ValueError, match="https"):
        ocr_release.fetch_release_asset("http://example.invalid/ocr")


# --- an installed package has no checkout -------------------------------------------

_READ_PIN = (
    "import sys; sys.path.insert(0, sys.argv[1]); from verdict import ocr_release; "
    "print(ocr_release.__file__); print(*ocr_release.ocr_pin())"
)


def read_pin_from(site: Path, cwd: Path) -> tuple[Path, tuple[str, ...]]:
    """Import ``verdict.ocr_release`` from ``site`` in an isolated interpreter.

    ``-I`` drops ``PYTHONPATH``, the user site and the working directory, so no
    checkout is importable; ``site`` comes first on ``sys.path``. Returns the
    module's ``__file__`` so callers can check where it came from.
    """
    result = subprocess.run(
        [sys.executable, "-I", "-c", _READ_PIN, str(site)],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    where, pin = result.stdout.splitlines()
    return Path(where), tuple(pin.split())


def test_installed_package_reads_the_pin_without_a_checkout(tmp_path: Path) -> None:
    """Live defect: a non-editable install has no ``.github/`` and could not read the pin."""
    expected = ocr_release.ocr_pin()
    assert re.fullmatch(r"v\d+\.\d+\.\d+", expected[0])
    assert re.fullmatch(r"[0-9a-f]{64}", expected[1])
    site = tmp_path / "site-packages"
    (site / "verdict").mkdir(parents=True)
    (site / "verdict" / "__init__.py").write_text("", encoding="utf-8")
    shutil.copy2(Path(ocr_release.__file__), site / "verdict" / "ocr_release.py")

    where, pin = read_pin_from(site, cwd=tmp_path)
    assert where.is_relative_to(site)
    assert not (site / ".github").exists()
    assert pin == expected


# --- one source, one check: nothing can drift from verdict.ocr_release --------------

REHEARSAL = ROOT / ".github" / "workflows" / "certify-rehearsal.yml"
PIN_STEP = "Read the ocr pin from the package"
INSTALL_STEP = "Install pinned independent reviewer"


@pytest.mark.skipif(not REHEARSAL.is_file(), reason="no .github/: not a repository checkout")
def test_rehearsal_workflow_reads_the_pin_from_the_package(tmp_path: Path) -> None:
    """The workflow restates nothing: one step reads the pin and the install uses it."""
    workflow = yaml.load(REHEARSAL.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    job = workflow["jobs"]["rehearse"]
    steps = job["steps"]
    for scope in (workflow, job, *steps):
        assert not {"OCR_VERSION", "OCR_SHA256"} & set(scope.get("env") or {}), scope
    assert not re.search(r"\b[0-9a-f]{64}\b", REHEARSAL.read_text(encoding="utf-8"))
    names = [step.get("name") for step in steps]
    sync = names.index("Sync locked dependencies")
    read = names.index(PIN_STEP)
    install = names.index(INSTALL_STEP)
    assert sync < read < install
    assert "releases/download/$OCR_VERSION/" in steps[install]["run"]
    assert '"$OCR_SHA256"' in steps[install]["run"]
    assert "sha256sum --check --strict" in steps[install]["run"]

    # Run the step as the runner does: job shell, a synced .venv, PYTHONPATH at the checkout.
    python = tmp_path / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n', encoding="utf-8")
    python.chmod(0o755)
    github_env = tmp_path / "github-env"
    subprocess.run(
        ["bash", "-euo", "pipefail", "-c", steps[read]["run"]],
        cwd=tmp_path,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(ROOT),
            "GITHUB_ENV": str(github_env),
        },
        check=True,
        capture_output=True,
        timeout=60,
    )
    exported = dict(
        line.split("=", 1) for line in github_env.read_text(encoding="utf-8").splitlines()
    )
    assert exported == {
        "OCR_VERSION": ocr_release.OCR_VERSION,
        "OCR_SHA256": ocr_release.OCR_SHA256,
    }


@pytest.mark.skipif(not (ROOT / ".git").exists(), reason="not a git checkout")
def test_no_other_tracked_file_restates_the_pin_digest() -> None:
    """Frozen history (``docs/proof/``, archived OpenSpec changes) is exempt."""
    result = subprocess.run(
        [
            *("git", "-C", str(ROOT), "grep", "-lF", ocr_release.OCR_SHA256, "--", "."),
            *(":!docs/proof", ":!openspec/changes/archive"),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.stdout.split() == ["verdict/ocr_release.py"], result.stdout


# A literal value assigned to OCR_VERSION or OCR_SHA256, as Python or as YAML.
_PIN_DEFINITION = r"""OCR_(VERSION|SHA256)["']?[[:space:]]*[:=][[:space:]]*["']?[0-9a-fv]"""


@pytest.mark.skipif(not (ROOT / ".git").exists(), reason="not a git checkout")
def test_no_other_tracked_file_defines_the_pin() -> None:
    """A second ``OCR_VERSION``/``OCR_SHA256`` with a literal value is a second pin.

    Tests inject their own pin through the ``ocr_pin`` seam and recorded ocr
    output names the version it ran; neither assigns these names, so neither
    matches. Frozen history is exempt.
    """
    result = subprocess.run(
        [
            *("git", "-C", str(ROOT), "grep", "-lE", _PIN_DEFINITION, "--", "."),
            *(":!docs/proof", ":!openspec/changes/archive"),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.stdout.split() == ["verdict/ocr_release.py"], result.stdout
