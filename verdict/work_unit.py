"""Bounded unit of delegated work.

A :class:`WorkUnit` is the contract between decomposition and execution.  It
names the files a patch is permitted to touch and the command that must exit
zero once the unit is done, so a unit cannot be constructed without a runnable
check.  That makes decomposition mechanically probeable: a decomposition that
cannot state how to verify its own output is a decomposition failure, reported
as one rather than discovered later.

The module is pure data plus validation.  It never runs a command, reads a
file, or calls a provider.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

WORK_UNIT_SCHEMA_VERSION = "1"

_REQUIRED_FIELDS = frozenset(
    {"unit_id", "objective", "owned_files", "verification_command", "context"}
)


class WorkUnitError(ValueError):
    """Raised when a work unit cannot be constructed from its inputs."""


@dataclass(frozen=True)
class WorkUnit:
    """One bounded, independently verifiable slice of an objective.

    Attributes:
        unit_id: Stable identifier, unique within a decomposition.
        objective: What this unit must accomplish, in prose.
        owned_files: The only repository-relative paths a patch may touch.
        verification_command: Argv that must exit zero when the unit is done.
        context: Optional extra detail handed to the executor.
    """

    unit_id: str
    objective: str
    owned_files: tuple[str, ...]
    verification_command: tuple[str, ...]
    context: str = ""
    schema_version: str = WORK_UNIT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in ("unit_id", "objective"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise WorkUnitError(f"{name} must be a non-empty string")
        if not isinstance(self.context, str):
            raise WorkUnitError("context must be a string")
        if self.schema_version != WORK_UNIT_SCHEMA_VERSION:
            raise WorkUnitError("unsupported work unit schema_version")

        owned = _as_str_tuple(self.owned_files, field="owned_files")
        if not owned:
            raise WorkUnitError("owned_files must name at least one path")
        normalized = tuple(normalize_owned_path(path) for path in owned)
        duplicates = sorted({path for path in normalized if normalized.count(path) > 1})
        if duplicates:
            raise WorkUnitError(f"owned_files contains duplicate path(s): {duplicates}")
        object.__setattr__(self, "owned_files", normalized)

        command = _as_str_tuple(self.verification_command, field="verification_command")
        if not command:
            raise WorkUnitError("verification_command must be a non-empty argv")
        object.__setattr__(self, "verification_command", command)

    def owns(self, path: str) -> bool:
        """Return whether ``path`` is inside this unit's boundary."""
        try:
            candidate = normalize_owned_path(path)
        except WorkUnitError:
            return False
        return candidate in self.owned_files

    def out_of_bounds(self, paths: Iterable[str]) -> tuple[str, ...]:
        """Return the subset of ``paths`` this unit does not own, sorted."""
        return tuple(sorted({path for path in paths if not self.owns(path)}))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "unit_id": self.unit_id,
            "objective": self.objective,
            "owned_files": list(self.owned_files),
            "verification_command": list(self.verification_command),
            "context": self.context,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> WorkUnit:
        """Build a unit from a decoded JSON object, rejecting unknown fields."""
        if not isinstance(value, Mapping):
            raise WorkUnitError("work unit must be an object")
        unknown = sorted(set(value) - _REQUIRED_FIELDS - {"schema_version"})
        if unknown:
            raise WorkUnitError(f"work unit has unknown field(s): {unknown}")
        missing = sorted(
            name
            for name in ("unit_id", "objective", "owned_files", "verification_command")
            if name not in value
        )
        if missing:
            raise WorkUnitError(f"work unit missing field(s): {missing}")
        return cls(
            unit_id=value["unit_id"],
            objective=value["objective"],
            owned_files=tuple(_as_str_tuple(value["owned_files"], field="owned_files")),
            verification_command=tuple(
                _as_str_tuple(value["verification_command"], field="verification_command")
            ),
            context=value.get("context", ""),
            schema_version=value.get("schema_version", WORK_UNIT_SCHEMA_VERSION),
        )


def normalize_owned_path(path: Any) -> str:
    """Return ``path`` as a repo-relative POSIX path, or raise.

    Rejects absolute paths, parent traversal, and Windows-style separators so a
    unit's boundary cannot be widened by a path the caller failed to resolve.
    """
    if not isinstance(path, str) or not path.strip():
        raise WorkUnitError("owned_files entries must be non-empty strings")
    raw = path.strip()
    if "\\" in raw:
        raise WorkUnitError(f"owned_files entry must use POSIX separators: {raw!r}")
    if raw.startswith("/") or PurePosixPath(raw).is_absolute():
        raise WorkUnitError(f"owned_files entry must be repo-relative: {raw!r}")
    parts = [part for part in PurePosixPath(raw).parts if part != "."]
    if not parts:
        raise WorkUnitError(f"owned_files entry does not name a file: {raw!r}")
    if any(part == ".." for part in parts):
        raise WorkUnitError(f"owned_files entry must not escape the repo: {raw!r}")
    return str(PurePosixPath(*parts))


def parse_work_units(value: Any) -> tuple[WorkUnit, ...]:
    """Build a tuple of units from a decoded JSON list.

    Every element is validated; the first invalid element raises, because a
    partially valid decomposition is a decomposition failure.
    """
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise WorkUnitError("work units must be a JSON list")
    units = tuple(WorkUnit.from_dict(item) for item in value)
    if not units:
        raise WorkUnitError("decomposition produced no work units")
    seen: set[str] = set()
    for unit in units:
        if unit.unit_id in seen:
            raise WorkUnitError(f"duplicate unit_id: {unit.unit_id!r}")
        seen.add(unit.unit_id)
    return units


def _as_str_tuple(value: Any, *, field: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise WorkUnitError(f"{field} must be a list of strings")
    items = tuple(value)
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise WorkUnitError(f"{field} must contain non-empty strings")
    return tuple(item.strip() for item in items)
