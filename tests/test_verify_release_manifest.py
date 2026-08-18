"""Tests for offline release artifact manifest verification."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.verify_release_artifacts import verify_manifest


def write_manifest(root: Path, path: str, content: bytes) -> Path:
    artifact = root / path
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(content)
    manifest = root / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "artifacts": [{"path": path, "sha256": hashlib.sha256(content).hexdigest()}],
            }
        ),
        encoding="utf-8",
    )
    return manifest


def test_verify_manifest_accepts_matching_local_digest(tmp_path: Path) -> None:
    manifest = write_manifest(tmp_path, "dist/package.whl", b"artifact")

    result = verify_manifest(manifest)

    assert result["artifacts"][0]["path"] == "dist/package.whl"


def test_manifest_only_cli_does_not_require_distribution_pair(tmp_path: Path) -> None:
    manifest = write_manifest(tmp_path, "package.whl", b"artifact")
    result = subprocess.run(
        [
            sys.executable,
            "scripts/verify_release_artifacts.py",
            "--manifest",
            str(manifest),
            "--manifest-only",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "verified artifact manifest" in result.stdout


def test_verify_manifest_rejects_tampered_or_missing_artifact(tmp_path: Path) -> None:
    manifest = write_manifest(tmp_path, "package.whl", b"artifact")
    (tmp_path / "package.whl").write_bytes(b"tampered")
    with pytest.raises(SystemExit, match="digest mismatch"):
        verify_manifest(manifest)

    (tmp_path / "package.whl").unlink()
    with pytest.raises(SystemExit, match="missing"):
        verify_manifest(manifest)


@pytest.mark.parametrize("path", ["../outside.whl", "/tmp/outside.whl", "nested/../package.whl"])
def test_verify_manifest_rejects_unsafe_paths(tmp_path: Path, path: str) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"schema_version": 1, "artifacts": [{"path": path, "sha256": "0" * 64}]}),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit, match="invalid path"):
        verify_manifest(manifest)
