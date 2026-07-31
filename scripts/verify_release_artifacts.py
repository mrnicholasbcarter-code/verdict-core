#!/usr/bin/env python3
"""Build and install release artifacts in isolated environments.

This script is intentionally stdlib-only so CI can run it immediately after
`uv build`. It rejects missing, duplicate, or unimportable wheel/sdist outputs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import venv
from pathlib import Path
from typing import Any


def run(command: list[str], *, cwd: Path | None = None) -> None:
    print("+", " ".join(str(part) for part in command))
    subprocess.run(command, cwd=cwd, check=True)


def executable_path(environment: Path, name: str) -> Path:
    """Return a venv executable path on both POSIX and Windows."""

    directory = "Scripts" if sys.platform == "win32" else "bin"
    suffix = ".exe" if sys.platform == "win32" else ""
    return environment / directory / f"{name}{suffix}"


def artifact_pair(dist: Path) -> tuple[Path, Path]:
    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise SystemExit(
            f"expected exactly one wheel and one sdist in {dist}; "
            f"found {len(wheels)} wheel(s) and {len(sdists)} sdist(s)"
        )
    return wheels[0], sdists[0]


def sha256_file(path: Path) -> str:
    """Return the SHA-256 digest for a local artifact."""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_manifest(manifest_path: Path, *, artifact_root: Path | None = None) -> dict[str, Any]:
    """Verify local artifact digests from a canonical JSON manifest.

    Manifest paths are relative to the manifest directory unless
    ``artifact_root`` is provided. No network or keyring access is performed.
    """

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"cannot read artifact manifest {manifest_path}: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise SystemExit("artifact manifest must contain schema_version 1")
    records = manifest.get("artifacts")
    if not isinstance(records, list) or not records:
        raise SystemExit("artifact manifest must contain a non-empty artifacts list")

    root = (artifact_root or manifest_path.parent).resolve()
    results: list[dict[str, str]] = []
    seen: set[Path] = set()
    for record in records:
        if not isinstance(record, dict):
            raise SystemExit("artifact manifest contains a non-object record")
        relative = record.get("path")
        expected = record.get("sha256")
        if (
            not isinstance(relative, str)
            or not relative
            or Path(relative).is_absolute()
            or "\\" in relative
            or any(part in {"", ".", ".."} for part in Path(relative).parts)
            or not isinstance(expected, str)
            or len(expected) != 64
            or any(character not in "0123456789abcdef" for character in expected)
        ):
            raise SystemExit("artifact manifest contains an invalid path or SHA-256 digest")
        path = (root / relative).resolve()
        if path in seen or root not in path.parents:
            raise SystemExit(f"artifact manifest contains duplicate or unsafe path: {relative}")
        seen.add(path)
        if not path.is_file():
            raise SystemExit(f"artifact is missing: {relative}")
        actual = sha256_file(path)
        if actual != expected:
            raise SystemExit(f"artifact digest mismatch: {relative}")
        results.append({"path": relative, "sha256": actual})
    return {"schema_version": 1, "artifacts": results}


def install_and_smoke_test(artifact: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="verdict-release-") as directory:
        environment = Path(directory) / "venv"
        venv.EnvBuilder(with_pip=True).create(environment)
        python = executable_path(environment, "python")
        verdict = executable_path(environment, "verdict")
        run([str(python), "-m", "pip", "install", "--disable-pip-version-check", str(artifact)])
        run(
            [
                str(python),
                "-c",
                "import verdict; from verdict import Gate; print(verdict.__version__, Gate.__name__)",
            ]
        )
        run([str(verdict), "--help"])
        run([str(python), "-m", "verdict", "--help"])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    parser.add_argument(
        "--manifest",
        type=Path,
        help="verify local artifact digests from a JSON manifest before install smoke tests",
    )
    parser.add_argument("--manifest-root", type=Path, help="root directory for paths in --manifest")
    parser.add_argument(
        "--manifest-only",
        action="store_true",
        help="verify --manifest without requiring wheel and sdist files",
    )
    args = parser.parse_args()
    if args.manifest:
        verify_manifest(args.manifest, artifact_root=args.manifest_root)
        print(f"verified artifact manifest: {args.manifest}")
        if args.manifest_only:
            return
    wheel, sdist = artifact_pair(args.dist)
    for artifact in (wheel, sdist):
        install_and_smoke_test(artifact.resolve())
    print(f"verified release artifacts: {wheel.name}, {sdist.name}")


if __name__ == "__main__":
    main()
