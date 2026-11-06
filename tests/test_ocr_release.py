"""verdict/ocr_release.py: the one pinned ocr release helper (BOD-219 part 2).

``verdict setup``, ``verdict doctor`` and ``scripts/check_dev_readiness.py`` read
the pin from ``.github/workflows/certify-rehearsal.yml`` through this module.
Every test uses a temporary directory and an injected fetcher: nothing here
downloads, and nothing reads the operator's real ``~/.local/bin``.
"""

from __future__ import annotations

import hashlib
import stat
from pathlib import Path

import pytest

from scripts import check_dev_readiness as readiness
from verdict import ocr_release

ROOT = Path(__file__).resolve().parents[1]
PIN = "v1.12.9"
ASSET = b"\x7fELF fake ocr release asset\n"
ASSET_SHA = hashlib.sha256(ASSET).hexdigest()


def _workflow_root(tmp_path: Path, digest: str = ASSET_SHA) -> Path:
    workflows = tmp_path / "repo" / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "certify-rehearsal.yml").write_text(
        f"jobs:\n  rehearse:\n    env:\n      OCR_VERSION: {PIN}\n      OCR_SHA256: {digest}\n",
        encoding="utf-8",
    )
    return tmp_path / "repo"


def _exe(path: Path, body: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    path.chmod(0o755)
    return path


def test_pin_is_read_from_the_real_rehearsal_workflow() -> None:
    version, digest = ocr_release.ocr_pin(ROOT)
    text = (ROOT / ocr_release.REHEARSAL_WORKFLOW).read_text(encoding="utf-8")
    assert f"OCR_VERSION: {version}" in text
    assert f"OCR_SHA256: {digest}" in text
    assert ocr_release.ocr_pin() == (version, digest)


def test_pin_requires_both_values(tmp_path: Path) -> None:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "certify-rehearsal.yml").write_text("env:\n  OCR_VERSION: v1.0.0\n")
    with pytest.raises(ValueError, match="OCR_VERSION and OCR_SHA256"):
        ocr_release.ocr_pin(tmp_path)


def test_readiness_script_shares_the_helper() -> None:
    assert readiness.ocr_pin is ocr_release.ocr_pin
    assert readiness.OCR_ASSET_URL == ocr_release.OCR_ASSET_URL
    assert readiness.REHEARSAL_WORKFLOW == ocr_release.REHEARSAL_WORKFLOW


def test_inspect_reports_missing_wrong_version_and_digest_mismatch(tmp_path: Path) -> None:
    calls: list[Path] = []

    def version_of(path: Path) -> str | None:
        calls.append(path)
        return "1.13.0" if path.name == "drifted" else "1.12.9"

    missing = ocr_release.inspect_ocr(
        None, version=PIN, digest=ASSET_SHA, reported_version=version_of
    )
    assert missing.state == "missing"
    assert missing.describe() == f"not on PATH; {ocr_release.REHEARSAL_WORKFLOW} pins {PIN}"

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
    assert found.describe().endswith(f"sha256 matches {ocr_release.REHEARSAL_WORKFLOW}")


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
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
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


def test_workflow_root_helper_round_trips(tmp_path: Path) -> None:
    root = _workflow_root(tmp_path)
    assert ocr_release.ocr_pin(root) == (PIN, ASSET_SHA)
