"""CLI entry point for dependency-documentation discovery and ingestion.

Discover the external packages the Verdict ecosystem imports or interacts
with, fetch missing documentation via ``context7``, and ingest provenance-
attributed summaries into the local MemoryPlane under the
``dependency-docs`` namespace.

Run modes:

* ``--discover-only`` — print the discovered package list (no npx calls).
* ``--cap N`` — limit how many packages are fetched via context7.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from verdict.dependency_ingest import (
    discover_dependencies,
    discover_dependencies_full,
    ingest_dependency_docs,
    report_rows,
)
from verdict.memory_plane import MemoryPlane


def _context7_fetcher() -> object:
    """Return a bounded fetcher that resolves one package via context7."""

    def fetch(package: str) -> str:
        env = dict(os.environ)
        env["NO_COLOR"] = "1"
        library = subprocess.run(
            ["npx", "ctx7@latest", "library", package],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
            check=False,
        )
        library_id = _pick_library_id(library.stdout)
        if library_id is None:
            raise RuntimeError(f"context7 library lookup failed for {package}")
        docs = subprocess.run(
            [
                "npx",
                "ctx7@latest",
                "docs",
                library_id,
                f"core usage, API, and configuration for {package}",
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=180,
            check=False,
        )
        if docs.returncode != 0:
            raise RuntimeError(f"context7 docs fetch failed for {package}")
        return _trim(docs.stdout)

    return fetch


def _pick_library_id(stdout: str) -> str | None:
    for line in stdout.splitlines():
        match = line.strip()
        if match.startswith("/") and "/" in match[1:]:
            return match.split()[0]
    return None


def _trim(text: str, limit: int = 200_000) -> str:
    text = text.strip()
    return text[:limit] if len(text) > limit else text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--memory-path", type=Path, default=Path.home() / ".verdict" / "memory.db")
    parser.add_argument("--cap", type=int, default=12)
    parser.add_argument("--discover-only", action="store_true")
    args = parser.parse_args()

    source_root = args.repo_root / "verdict"
    if args.discover_only:
        for name in discover_dependencies(
            source_root,
            pyproject=args.repo_root / "pyproject.toml",
            package_json=args.repo_root / "contracts" / "package.json",
        ):
            print(name)
        return 0

    refs = discover_dependencies_full(
        source_root,
        pyproject=args.repo_root / "pyproject.toml",
        package_json=args.repo_root / "contracts" / "package.json",
    )
    with MemoryPlane(args.memory_path) as plane:
        results = ingest_dependency_docs(
            plane, refs, args.repo_root, fetcher=_context7_fetcher(), cap=args.cap
        )
    sys.stdout.write(report_rows(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
