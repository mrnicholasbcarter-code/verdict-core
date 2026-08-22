#!/usr/bin/env python3
"""Fail closed unless a release tag and all package versions agree."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import tomllib

SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def _json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SystemExit(f"manifest is not an object: {path}")
    return value


def verify(root: Path, tag: str) -> str:
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    contracts = _json(root / "contracts/package.json")
    client = _json(root / "verdict/client-sdk/package.json")
    versions = {
        "python": project["project"]["version"],
        "contracts": contracts["version"],
        "client": client["version"],
    }
    if len(set(versions.values())) != 1:
        raise SystemExit(f"release versions are not synchronized: {versions}")

    version = versions["python"]
    if not isinstance(version, str) or not SEMVER.fullmatch(version):
        raise SystemExit(f"release version is not stable semantic versioning: {version}")
    if tag != f"v{version}":
        raise SystemExit(f"tag {tag} does not match package version {version}")

    runtime_source = (root / "verdict/__init__.py").read_text(encoding="utf-8")
    runtime_match = re.search(r'^__version__ = ["\']([^"\']+)["\']$', runtime_source, re.MULTILINE)
    runtime_version = runtime_match.group(1) if runtime_match else None
    if runtime_version != version:
        raise SystemExit(f"runtime version must be {version}; found {runtime_version}")

    peers = client.get("peerDependencies")
    expected = f"^{version}"
    actual = peers.get("@bodanglin/verdict-contracts") if isinstance(peers, dict) else None
    if actual != expected:
        raise SystemExit(f"client peer dependency must be {expected}; found {actual}")
    return version


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--tag", required=True)
    args = parser.parse_args()
    try:
        version = verify(args.root.resolve(), args.tag)
    except (KeyError, OSError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        raise SystemExit(f"cannot verify release versions: {exc}") from exc
    print(f"verified synchronized release version: {version}")


if __name__ == "__main__":
    main()
