"""Locate checked-in benchmark fixtures from a source checkout or an installed wheel.

Wheels ship ``benchmarks/fixtures`` under ``verdict/data/benchmarks/fixtures``
(see ``[tool.hatch.build.targets.wheel.force-include]``), so the documented
``verdict benchmark`` commands work without a source checkout (BOD-113).
"""

from __future__ import annotations

import os
from pathlib import Path

_PACKAGE_DIR = Path(__file__).resolve().parent
_SOURCE_ROOT = _PACKAGE_DIR.parent
_PACKAGED_ROOT = _PACKAGE_DIR / "data"

FIXTURE_ROOTS: tuple[Path, ...] = (_SOURCE_ROOT, _PACKAGED_ROOT)


def resolve_fixture_path(path: str | os.PathLike[str]) -> Path:
    """Resolve a fixture path given absolute, cwd-relative, or repo-relative form.

    Search order: the path as given, then each fixture root joined with the
    relative path. A path that exists nowhere is returned as given so the
    caller surfaces the usual ``FileNotFoundError`` for that exact location.
    """
    candidate = Path(path)
    if candidate.is_absolute() or candidate.exists():
        return candidate
    for root in FIXTURE_ROOTS:
        resolved = root / candidate
        if resolved.exists():
            return resolved
    return candidate


def default_fixture_path(relative: str) -> Path:
    """Default location of a repo-relative fixture such as ``benchmarks/fixtures/x.json``."""
    return resolve_fixture_path(relative)


def resolve_fixture_workspace(fixture_path: Path, raw: str | os.PathLike[str]) -> Path:
    """Resolve a fixture-declared workspace (e.g. ``benchmarks/fixtures/legit_workspace``).

    Relative workspaces are tried first beside the fixture file itself (so a
    packaged fixture finds its sibling workspace wherever the wheel lives),
    then against the cwd, then each fixture root.
    """
    workspace = Path(raw)
    if workspace.is_absolute():
        return workspace
    candidates = [
        fixture_path.resolve().parent / workspace.name,
        workspace,
        *(root / workspace for root in FIXTURE_ROOTS),
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate.resolve()
    return (FIXTURE_ROOTS[0] / workspace).resolve()


__all__ = [
    "FIXTURE_ROOTS",
    "default_fixture_path",
    "resolve_fixture_path",
    "resolve_fixture_workspace",
]
