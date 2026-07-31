#!/usr/bin/env python3
"""Create and verify deterministic, content-addressed evidence bundles."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import tarfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any

EVIDENCE_DIR = Path(__file__).parent.parent / "evidence"
BUNDLE_DIR = Path(__file__).parent.parent / "evidence_bundle"
MANIFEST_NAME = "evidence_manifest.json"
_ALLOWED_SUFFIXES = frozenset({".json", ".md", ".txt", ".log", ".xml"})


def _manifest_bytes(evidence: dict[str, Any]) -> bytes:
    """Serialize a manifest canonically so its digest is reproducible."""

    return (
        json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _safe_relative_path(value: str) -> str:
    """Return a normalized archive path or reject traversal/ambiguity."""

    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"unsafe evidence path: {value!r}")
    path = PurePosixPath(value)
    normalized = path.as_posix()
    if (
        path.is_absolute()
        or normalized != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError(f"unsafe evidence path: {value!r}")
    if normalized == MANIFEST_NAME:
        raise ValueError("the manifest cannot list itself as an artifact")
    return normalized


def _artifact_paths(evidence_dir: Path) -> Iterable[Path]:
    """Yield supported regular files in stable order, rejecting unsafe entries."""

    if not evidence_dir.is_dir():
        raise ValueError(f"evidence directory does not exist: {evidence_dir}")
    for path in sorted(evidence_dir.rglob("*"), key=lambda item: item.as_posix()):
        if path.name == MANIFEST_NAME and path.parent == evidence_dir:
            continue
        if path.is_symlink():
            raise ValueError(f"symlinks are not allowed in evidence: {path}")
        if path.is_dir():
            continue
        if not path.is_file() or path.suffix.lower() not in _ALLOWED_SUFFIXES:
            raise ValueError(f"unsupported or non-regular evidence file: {path}")
        _safe_relative_path(path.relative_to(evidence_dir).as_posix())
        yield path


def collect_evidence(evidence_dir: Path = EVIDENCE_DIR) -> dict[str, Any]:
    """Collect sorted artifact sizes and SHA-256 digests from ``evidence_dir``."""

    artifacts: list[dict[str, Any]] = []
    for path in _artifact_paths(evidence_dir):
        content = path.read_bytes()
        relative = _safe_relative_path(path.relative_to(evidence_dir).as_posix())
        artifacts.append(
            {"path": relative, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
        )
    artifacts.sort(key=lambda artifact: artifact["path"])
    return {"schema_version": 1, "artifacts": artifacts}


def _validated_manifest(manifest: Any) -> dict[str, Any]:
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("unsupported or malformed evidence manifest")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValueError("evidence manifest artifacts must be a list")

    validated: list[dict[str, Any]] = []
    paths: set[str] = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise ValueError("evidence manifest contains a non-object artifact")
        path = _safe_relative_path(artifact.get("path", ""))
        if path in paths:
            raise ValueError(f"duplicate evidence path: {path}")
        digest = artifact.get("sha256")
        size = artifact.get("size")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
        ):
            raise ValueError(f"invalid evidence record for {path}")
        paths.add(path)
        validated.append({"path": path, "size": size, "sha256": digest})

    validated.sort(key=lambda artifact: artifact["path"])
    return {"schema_version": 1, "artifacts": validated}


def _load_manifest(evidence_dir: Path) -> tuple[dict[str, Any], bytes]:
    manifest_path = evidence_dir / MANIFEST_NAME
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read evidence manifest: {manifest_path}") from exc
    validated = _validated_manifest(manifest)
    if manifest_bytes != _manifest_bytes(validated):
        raise ValueError("evidence manifest is not canonical")
    return validated, manifest_bytes


def _expected_archive_names(manifest: dict[str, Any]) -> set[str]:
    return {f"evidence/{MANIFEST_NAME}"} | {
        f"evidence/{artifact['path']}" for artifact in manifest["artifacts"]
    }


def _verify_archive(bundle_path: Path, manifest: dict[str, Any], manifest_bytes: bytes) -> None:
    """Verify archive membership and content without extracting untrusted paths."""

    expected = _expected_archive_names(manifest)
    try:
        with tarfile.open(bundle_path, "r:gz") as archive:
            members = archive.getmembers()
            names = [member.name for member in members]
            if len(names) != len(set(names)):
                raise ValueError("evidence bundle contains duplicate members")
            if set(names) != expected:
                raise ValueError("evidence bundle contains missing or unexpected members")
            for member in members:
                if not member.isfile() or not member.name.startswith("evidence/"):
                    raise ValueError(f"unsafe evidence bundle member: {member.name!r}")
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError(f"cannot read evidence bundle member: {member.name}")
                content = stream.read()
                if member.name == f"evidence/{MANIFEST_NAME}":
                    expected_content = manifest_bytes
                else:
                    relative = member.name.removeprefix("evidence/")
                    record = next(
                        artifact
                        for artifact in manifest["artifacts"]
                        if artifact["path"] == relative
                    )
                    expected_content = None
                    if (
                        len(content) != record["size"]
                        or hashlib.sha256(content).hexdigest() != record["sha256"]
                    ):
                        raise ValueError(f"evidence bundle digest mismatch: {relative}")
                if expected_content is not None and content != expected_content:
                    raise ValueError("evidence bundle manifest does not match")
    except (OSError, tarfile.TarError) as exc:
        raise ValueError(f"cannot read evidence bundle: {bundle_path}") from exc


def verify_bundle(
    evidence_dir: Path = EVIDENCE_DIR, bundle_path: Path | None = None
) -> dict[str, Any]:
    """Verify on-disk evidence, optionally including its compressed archive."""

    manifest, manifest_bytes = _load_manifest(evidence_dir)
    expected_paths = {artifact["path"] for artifact in manifest["artifacts"]}
    actual_paths = {
        path.relative_to(evidence_dir).as_posix() for path in _artifact_paths(evidence_dir)
    }
    if actual_paths != expected_paths:
        missing = sorted(expected_paths - actual_paths)
        unexpected = sorted(actual_paths - expected_paths)
        raise ValueError(
            f"evidence files do not match manifest (missing={missing}, unexpected={unexpected})"
        )

    for artifact in manifest["artifacts"]:
        path = evidence_dir / artifact["path"]
        content = path.read_bytes()
        if (
            len(content) != artifact["size"]
            or hashlib.sha256(content).hexdigest() != artifact["sha256"]
        ):
            raise ValueError(f"evidence digest mismatch: {artifact['path']}")

    if bundle_path is not None:
        _verify_archive(bundle_path, manifest, manifest_bytes)
    return manifest


def create_bundle(
    evidence: dict[str, Any], evidence_dir: Path = EVIDENCE_DIR, bundle_dir: Path = BUNDLE_DIR
) -> Path:
    """Write a deterministic manifest and gzip archive, named by manifest digest."""

    manifest = _validated_manifest(evidence)
    manifest_bytes = _manifest_bytes(manifest)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / MANIFEST_NAME).write_bytes(manifest_bytes)
    bundle_dir.mkdir(parents=True, exist_ok=True)
    manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    bundle_path = bundle_dir / f"verdict-evidence-{manifest_digest}.tar.gz"

    with (
        bundle_path.open("wb") as raw_file,
        gzip.GzipFile(
            filename="", fileobj=raw_file, mode="wb", compresslevel=9, mtime=0
        ) as compressed,
        tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive,
    ):
        files = [(MANIFEST_NAME, manifest_bytes)]
        files.extend(
            (artifact["path"], (evidence_dir / artifact["path"]).read_bytes())
            for artifact in manifest["artifacts"]
        )
        for relative, content in files:
            info = tarfile.TarInfo(f"evidence/{relative}")
            info.size = len(content)
            info.mode = 0o644
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            archive.addfile(info, io.BytesIO(content))
    verify_bundle(evidence_dir, bundle_path)
    return bundle_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path, default=EVIDENCE_DIR)
    parser.add_argument("--output-dir", type=Path, default=BUNDLE_DIR)
    parser.add_argument("--verify", action="store_true", help="verify existing evidence")
    parser.add_argument("--bundle", type=Path, help="archive to verify")
    args = parser.parse_args(argv)

    try:
        if args.verify:
            manifest = verify_bundle(args.evidence_dir, args.bundle)
            print(f"verified {len(manifest['artifacts'])} evidence artifacts")
            return 0
        evidence = collect_evidence(args.evidence_dir)
        bundle_path = create_bundle(evidence, args.evidence_dir, args.output_dir)
        print(f"created deterministic evidence bundle: {bundle_path}")
        print(f"verified {len(evidence['artifacts'])} evidence artifacts")
        return 0
    except (OSError, ValueError, StopIteration) as exc:
        parser.error(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
