"""The pinned open-code-review (``ocr``) release: pin, inspection and verified install.

``OCR_VERSION`` (the release tag) and ``OCR_SHA256`` (the SHA-256 of its
linux/amd64 asset) below are the single source of truth. They ship inside the
package, so a non-editable install reads them with no repository checkout.
``verdict setup``, ``verdict doctor``, ``scripts/check_dev_readiness.py`` and
``.github/workflows/certify-rehearsal.yml`` all read them from this module;
nothing else restates them, and ``tests/test_ocr_release.py`` fails if anything
does.

A binary is the pinned release only when its SHA-256 matches: a version string
is not identity. ``install_release`` hashes the downloaded bytes before it writes
anything, so a mismatch never installs.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

OCR_VERSION = "v1.12.9"
OCR_SHA256 = "9105c7081b8362a1cb0167f1ddfd1437e147a0e3c9a5af89c03c8859a5b0f0e8"
#: Where the pin lives, as operator-facing messages name it.
PIN_SOURCE = "verdict.ocr_release"
OCR_ASSET_URL = (
    "https://github.com/alibaba/open-code-review/releases/download/"
    "{version}/opencodereview-linux-amd64"
)
#: Larger bodies are refused instead of buffered; the asset is far smaller.
MAX_ASSET_BYTES = 256 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 120.0
VERSION_TIMEOUT_SECONDS = 30.0
_VERSION = re.compile(r"\bv?(\d+\.\d+\.\d+)\b")

Fetch = Callable[[str], bytes]
ReportedVersion = Callable[[Path], str | None]


def package_root() -> Path:
    """The directory above this package: the checkout only for an editable install."""
    return Path(__file__).resolve().parent.parent


def ocr_pin() -> tuple[str, str]:
    """Return the pinned ``ocr`` release tag and the SHA-256 of its linux/amd64 asset."""
    return OCR_VERSION, OCR_SHA256


def install_target(home: Path) -> Path:
    """Where ``verdict setup`` installs the pinned release."""
    return home / ".local" / "bin" / "ocr"


def sha256_file(path: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def probe_version(path: Path) -> str | None:
    """The first ``X.Y.Z`` that ``<path> --version`` prints, with an empty environment."""
    try:
        result = subprocess.run(
            [str(path), "--version"],
            capture_output=True,
            text=True,
            env={},
            timeout=VERSION_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    match = _VERSION.search(f"{result.stdout}\n{result.stderr}")
    return match.group(1) if match else None


@dataclass(frozen=True)
class OcrInspection:
    """What one ``ocr`` binary is, measured against the pin."""

    state: str  # ok | missing | wrong_version | digest_mismatch
    path: Path | None
    version: str
    digest: str
    reported: str | None = None
    actual_digest: str | None = None

    @property
    def ok(self) -> bool:
        return self.state == "ok"

    def describe(self) -> str:
        if self.state == "missing":
            return f"not on PATH; {PIN_SOURCE} pins {self.version}"
        if self.state == "wrong_version":
            shown = f"v{self.reported}" if self.reported else "no version"
            return f"{self.path} reports {shown}; {PIN_SOURCE} pins {self.version}"
        if self.state == "digest_mismatch":
            return (
                f"{self.path} reports {self.version} but has sha256 "
                f"{self.actual_digest or 'unreadable'}; {PIN_SOURCE} pins {self.digest} "
                "(the linux/amd64 release asset)"
            )
        return f"{self.path} is {self.version}, sha256 matches {PIN_SOURCE}"


def inspect_ocr(
    path: Path | None,
    *,
    version: str,
    digest: str,
    reported_version: ReportedVersion = probe_version,
) -> OcrInspection:
    """Classify ``path`` against the pin.

    The digest is checked first, so the pinned binary is never executed. Any other
    binary is asked for its version only to tell a different release from a rebuild
    of the pinned tag.
    """
    if path is None:
        return OcrInspection("missing", None, version, digest)
    actual = sha256_file(path)
    if actual == digest:
        return OcrInspection("ok", path, version, digest, version.lstrip("v"), actual)
    reported = reported_version(path)
    if reported is None or f"v{reported}" != version:
        return OcrInspection("wrong_version", path, version, digest, reported, actual)
    return OcrInspection("digest_mismatch", path, version, digest, reported, actual)


def fetch_release_asset(
    url: str, *, timeout: float = DOWNLOAD_TIMEOUT_SECONDS, max_bytes: int = MAX_ASSET_BYTES
) -> bytes:
    """Download one release asset over HTTPS, bounded in time and size."""
    if not url.startswith("https://"):
        raise ValueError(f"release assets are fetched over https only: {url!r}")
    request = urllib.request.Request(url, headers={"Accept": "application/octet-stream"})
    # Scheme checked above; GitHub answers with a redirect to its asset host.
    with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
        if not str(response.geturl()).startswith("https://"):
            raise ValueError("release download was redirected off https")
        body = bytes(response.read(max_bytes + 1))
    if len(body) > max_bytes:
        raise ValueError(f"release asset exceeds {max_bytes} bytes")
    return body


def install_release(
    *, version: str, digest: str, target: Path, fetch: Fetch = fetch_release_asset
) -> dict[str, Any]:
    """Download the pinned asset, verify its SHA-256, then install it at ``target``.

    Nothing is written unless the downloaded bytes match ``digest``. A replaced
    binary is copied to ``<target>.verdict.bak`` first.
    """
    url = OCR_ASSET_URL.format(version=version)
    base: dict[str, Any] = {"url": url, "path": str(target), "version": version}
    try:
        body = fetch(url)
    except Exception as exc:  # transport, TLS, HTTP or size: never installs
        return {**base, "status": "failed", "note": f"download failed: {type(exc).__name__}"}
    actual = hashlib.sha256(body).hexdigest()
    if actual != digest:
        return {
            **base,
            "status": "failed",
            "sha256": actual,
            "expected_sha256": digest,
            "note": (
                f"downloaded sha256 {actual} does not match the {PIN_SOURCE} pin "
                f"{digest}; nothing installed"
            ),
        }
    target.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if target.exists() and sha256_file(target) != digest:
        backup = target.with_name(target.name + ".verdict.bak")
        shutil.copy2(target, backup)
    fd, staged = tempfile.mkstemp(dir=target.parent, prefix=".ocr-download-")
    try:
        with os.fdopen(fd, "wb") as handle:
            # Owner-only: the operator runs this binary from their own PATH;
            # nobody else on the host needs to read or execute it.
            os.fchmod(handle.fileno(), 0o700)
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staged, target)
    finally:
        Path(staged).unlink(missing_ok=True)
    if sha256_file(target) != digest:
        return {**base, "status": "failed", "note": f"{target} does not hash to the pin"}
    return {
        **base,
        "status": "installed",
        "sha256": digest,
        "backup": str(backup) if backup else None,
        "note": f"installed {version} at {target}; sha256 verified against {PIN_SOURCE}",
    }
