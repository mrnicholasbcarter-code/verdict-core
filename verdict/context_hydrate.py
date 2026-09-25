"""Gather real provenance units for the serve cheap-path context pack.

Invent-never: only files that exist become units. Missing configured roots and
unreadable sources become named omissions. MCP is consulted only when a source
is actually configured; an absent MCP config is skipped, not faked.

OmniRoute RTK/compression is out of scope — this module feeds ``ContextPack``.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from verdict.context_pack import ContextUnit, SlotType, unit_prompt_token_cost
from verdict.documentation_preflight import _is_adr_path

CHEAP_PATH_EPOCH = "1970-01-01T00:00:00Z"
DEFAULT_CONTEXT_ROOTS: tuple[str, ...] = ("docs", "docs/architecture", "docs/adr")
PROJECT_DOC_NAMES: tuple[str, ...] = ("README.md", "README", "AGENTS.md", "CONTRIBUTING.md")
DEFAULT_MAX_UNITS_PER_ROOT = 8
DEFAULT_MAX_FILE_BYTES = 8_192
# ADR / architecture / project docs must land before misc docs when the pack
# budget is tight. Lower number = packed sooner.
HYDRATE_CLASS_ADR = 0
HYDRATE_CLASS_ARCHITECTURE = 1
HYDRATE_CLASS_README = 2
HYDRATE_CLASS_PROJECT = 3
HYDRATE_CLASS_OTHER = 4
_PROJECT_DOC_LOWER = {name.lower() for name in PROJECT_DOC_NAMES}
_README_NAMES = frozenset({"readme.md", "readme"})
_DOC_SUFFIXES = {".md", ".markdown", ".txt"}
_SKIP_DIR_PARTS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    ".tox",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
}
_STOP = frozenset(
    {
        "this",
        "that",
        "with",
        "from",
        "your",
        "project",
        "the",
        "and",
        "for",
        "into",
        "only",
        "using",
    }
)
WORKSPACE_ROOT_ENV = "VERDICT_WORKSPACE_ROOT"
CONTEXT_ROOTS_ENV = "VERDICT_CONTEXT_ROOTS"
MCP_CONTEXT_ROOT_ENV = "VERDICT_MCP_CONTEXT_ROOT"


@dataclass(frozen=True)
class HydrateOmission:
    """A configured source that did not become a unit, with why."""

    name: str
    reason: str


@dataclass(frozen=True)
class HydrateGather:
    """Workspace units plus gather-time omissions (compiler omissions are separate).

    ``required_uris`` names the task-relevant high-value sources (ADR /
    architecture files whose path or content matches the task terms). A pack
    that omits any of them is incomplete for this task even if some other ADR
    landed (BOD-110).
    """

    units: tuple[ContextUnit, ...]
    omissions: tuple[HydrateOmission, ...]
    workspace_root: Path
    required_uris: tuple[str, ...] = ()


def resolve_workspace_root(workspace_root: Path | str | None = None) -> Path:
    """Resolve the cheap-path workspace: argument, then env, then cwd."""
    if workspace_root is not None and str(workspace_root).strip():
        return Path(workspace_root).expanduser().resolve()
    configured = os.getenv(WORKSPACE_ROOT_ENV)
    if configured and configured.strip():
        return Path(configured.strip()).expanduser().resolve()
    return Path.cwd().resolve()


def default_context_roots(
    workspace_root: Path, roots: Sequence[str] | None = None
) -> tuple[str, ...]:
    """Return configured relative (or absolute) context roots, else defaults."""
    if roots is not None:
        return tuple(item.strip() for item in roots if str(item).strip())
    env = os.getenv(CONTEXT_ROOTS_ENV)
    if env is not None and env.strip():
        return tuple(part.strip() for part in env.split(",") if part.strip())
    from_config = _roots_from_config(workspace_root)
    if from_config is not None:
        return from_config
    return DEFAULT_CONTEXT_ROOTS


def resolve_mcp_root(mcp_root: Path | str | None = None) -> Path | None:
    """Return a configured MCP snapshot root, or None when MCP is not configured.

    An empty string disables MCP even if the environment names a root. None
    means "consult the environment / project config". This never invents a
    live MCP client.
    """
    if mcp_root is not None:
        text = str(mcp_root).strip()
        return Path(text).expanduser().resolve() if text else None
    env = os.getenv(MCP_CONTEXT_ROOT_ENV)
    if env is not None and env.strip():
        return Path(env.strip()).expanduser().resolve()
    configured = _mcp_root_from_config(resolve_workspace_root())
    return configured


def gather_cheap_path_units(
    task: str,
    *,
    workspace_root: Path | str | None = None,
    roots: Sequence[str] | None = None,
    mcp_root: Path | str | None = None,
    max_units_per_root: int = DEFAULT_MAX_UNITS_PER_ROOT,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> HydrateGather:
    """Collect provenance-attributed units from workspace roots and optional MCP."""
    root = resolve_workspace_root(workspace_root)
    omissions: list[HydrateOmission] = []
    units: list[ContextUnit] = []
    seen_uris: set[str] = set()

    if not root.exists() or not root.is_dir():
        omissions.append(HydrateOmission(name="workspace", reason="source_missing"))
        return HydrateGather((), tuple(omissions), root)

    project_units, project_omissions = _gather_project_docs(
        root, max_file_bytes=max_file_bytes, seen_uris=seen_uris
    )
    units.extend(project_units)
    omissions.extend(project_omissions)

    configured_roots = default_context_roots(root, roots)
    resolved_roots = [_resolve_named_root(root, name) for name in configured_roots]
    for name, path in zip(configured_roots, resolved_roots, strict=True):
        nested = tuple(
            other
            for other in resolved_roots
            if other != path and other is not None and path is not None and _is_under(other, path)
        )
        found, missed = _gather_root(
            workspace=root,
            name=name,
            path=path,
            task=task,
            max_units=max_units_per_root,
            max_file_bytes=max_file_bytes,
            skip_subtrees=nested,
            seen_uris=seen_uris,
        )
        units.extend(found)
        omissions.extend(missed)

    mcp = resolve_mcp_root(mcp_root)
    if mcp is not None:
        found, missed = _gather_mcp(
            mcp, task=task, max_units=max_units_per_root, max_file_bytes=max_file_bytes
        )
        units.extend(found)
        omissions.extend(missed)

    # Required sources are judged over everything the task matched — gathered
    # units *and* files the per-root cap left behind — never only the survivors.
    required = task_required_uris(task, units) + tuple(
        omission.name for omission in omissions if omission.reason == OMISSION_UNIT_CAP_EXCEEDED
    )
    return HydrateGather(tuple(units), tuple(omissions), root, tuple(dict.fromkeys(required)))


def task_required_uris(task: str, units: Sequence[ContextUnit]) -> tuple[str, ...]:
    """Name the gathered high-value units this task specifically depends on.

    A unit is required when it is an ADR or architecture source and its path
    or content matches the task's query terms. Nothing is inferred beyond the
    gathered set: a required source that does not exist on disk is not
    invented here — it simply cannot be required.
    """
    terms = _query_terms(task)
    if not terms:
        return ()
    required: list[str] = []
    for unit in units:
        if unit_hydrate_class(unit) not in (HYDRATE_CLASS_ADR, HYDRATE_CLASS_ARCHITECTURE):
            continue
        haystack = f"{unit.source_uri}\n{unit.content}".lower()
        if any(term in haystack for term in terms):
            required.append(unit.source_uri)
    return tuple(dict.fromkeys(required))


def _resolve_named_root(workspace: Path, name: str) -> Path | None:
    candidate = Path(name).expanduser()
    path = candidate if candidate.is_absolute() else (workspace / candidate)
    try:
        return path.resolve()
    except OSError:
        return None


def _gather_project_docs(
    workspace: Path, *, max_file_bytes: int, seen_uris: set[str]
) -> tuple[list[ContextUnit], list[HydrateOmission]]:
    """README / AGENTS / CONTRIBUTING at the repo root. Absence is not an omission."""
    units: list[ContextUnit] = []
    omissions: list[HydrateOmission] = []
    for filename in PROJECT_DOC_NAMES:
        target = workspace / filename
        if not target.is_file() or target.is_symlink():
            continue
        unit, omission = _unit_from_file(
            workspace,
            target,
            slot_type="evidence",
            max_file_bytes=max_file_bytes,
            seen_uris=seen_uris,
        )
        if omission is not None:
            omissions.append(omission)
        elif unit is not None:
            units.append(unit)
    return units, omissions


def _gather_root(
    *,
    workspace: Path,
    name: str,
    path: Path | None,
    task: str,
    max_units: int,
    max_file_bytes: int,
    skip_subtrees: Sequence[Path],
    seen_uris: set[str],
) -> tuple[list[ContextUnit], list[HydrateOmission]]:
    if path is None or not path.exists():
        return [], [HydrateOmission(name=name, reason="source_missing")]
    if path.is_file():
        unit, omission = _unit_from_file(
            workspace,
            path,
            slot_type=_slot_for(path),
            max_file_bytes=max_file_bytes,
            seen_uris=seen_uris,
        )
        if omission is not None:
            return [], [omission]
        return ([unit] if unit is not None else []), []
    if not path.is_dir():
        return [], [HydrateOmission(name=name, reason="source_missing")]

    if path == workspace:
        return _gather_project_docs(workspace, max_file_bytes=max_file_bytes, seen_uris=seen_uris)

    candidates = _list_doc_files(path, skip_subtrees=skip_subtrees)
    ranked = _rank_files(candidates, task=task)
    units: list[ContextUnit] = []
    omissions: list[HydrateOmission] = []
    for index, file_path in enumerate(ranked):
        if len(units) >= max_units:
            # The cap has been reached. Anything left that this task *requires*
            # (task-matching ADR / architecture) must be named as an omission so
            # the pack cannot read hydrated while silently lacking it (BOD-110).
            omissions.extend(
                _capped_required_omissions(
                    workspace,
                    ranked[index:],
                    task=task,
                    max_file_bytes=max_file_bytes,
                    seen_uris=seen_uris,
                )
            )
            break
        unit, omission = _unit_from_file(
            workspace,
            file_path,
            slot_type=_slot_for(file_path),
            max_file_bytes=max_file_bytes,
            seen_uris=seen_uris,
        )
        if omission is not None:
            omissions.append(omission)
        elif unit is not None:
            units.append(unit)
    return units, omissions


OMISSION_UNIT_CAP_EXCEEDED = "unit_cap_exceeded"


def _capped_required_omissions(
    workspace: Path,
    remaining: Sequence[Path],
    *,
    task: str,
    max_file_bytes: int,
    seen_uris: set[str],
) -> list[HydrateOmission]:
    """Name task-required high-value files the per-root cap left ungathered."""
    terms = _query_terms(task)
    if not terms:
        return []
    omissions: list[HydrateOmission] = []
    for file_path in remaining:
        if hydrate_priority_class(file_path) not in (HYDRATE_CLASS_ADR, HYDRATE_CLASS_ARCHITECTURE):
            continue
        source_uri = _relative_uri(workspace, file_path)
        if source_uri in seen_uris:
            continue
        try:
            content = truncate_utf8(file_path.read_text(encoding="utf-8"), max_file_bytes)
        except (OSError, UnicodeDecodeError):
            content = ""
        haystack = f"{source_uri}\n{content}".lower()
        if any(term in haystack for term in terms):
            omissions.append(HydrateOmission(name=source_uri, reason=OMISSION_UNIT_CAP_EXCEEDED))
    return omissions


def _gather_mcp(
    mcp_root: Path, *, task: str, max_units: int, max_file_bytes: int
) -> tuple[list[ContextUnit], list[HydrateOmission]]:
    if not mcp_root.exists() or not mcp_root.is_dir():
        return [], [HydrateOmission(name="mcp", reason="source_missing")]
    seen: set[str] = set()
    ranked = _rank_files(_list_doc_files(mcp_root, skip_subtrees=()), task=task)
    units: list[ContextUnit] = []
    omissions: list[HydrateOmission] = []
    for file_path in ranked:
        if len(units) >= max_units:
            break
        unit, omission = _unit_from_file(
            mcp_root,
            file_path,
            slot_type="memory",
            max_file_bytes=max_file_bytes,
            seen_uris=seen,
            uri_prefix="mcp:",
        )
        if omission is not None:
            omissions.append(omission)
        elif unit is not None:
            units.append(unit)
    return units, omissions


def _list_doc_files(root: Path, *, skip_subtrees: Sequence[Path]) -> list[Path]:
    found: list[Path] = []
    try:
        iterator = root.rglob("*")
    except OSError:
        return []
    skip = tuple(path.resolve() for path in skip_subtrees)
    for path in iterator:
        try:
            if path.is_symlink() or not path.is_file():
                continue
        except OSError:
            continue
        if any(part in _SKIP_DIR_PARTS for part in path.parts):
            continue
        if path.suffix.lower() not in _DOC_SUFFIXES:
            continue
        resolved = path.resolve()
        if any(_is_under(resolved, subtree) for subtree in skip):
            continue
        found.append(resolved)
    return sorted(found, key=lambda item: item.as_posix())


def _rank_files(paths: Sequence[Path], *, task: str) -> list[Path]:
    terms = _query_terms(task)
    # Query hits first, then high-value small roots (ADR / architecture /
    # project docs) so a tight pack budget still hydrates the thesis files
    # instead of filling on large misc docs.
    return sorted(
        paths,
        key=lambda path: (
            0 if terms and _file_matches(path, terms) else 1,
            hydrate_priority_class(path),
            _file_size(path),
            path.as_posix(),
        ),
    )


def hydrate_priority_class(path: Path) -> int:
    """Return pack priority for a workspace path (ADR highest, misc lowest)."""
    name = path.name.lower()
    parts = {part.lower() for part in path.parts}
    if _is_adr_path(path) or name.startswith("adr-") or name.startswith("adr_"):
        return HYDRATE_CLASS_ADR
    if "architecture" in parts or name == "architecture.md":
        return HYDRATE_CLASS_ARCHITECTURE
    if name in _README_NAMES:
        return HYDRATE_CLASS_README
    if name in _PROJECT_DOC_LOWER:
        return HYDRATE_CLASS_PROJECT
    return HYDRATE_CLASS_OTHER


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def unit_hydrate_class(unit: ContextUnit) -> int:
    """Pack priority for a gathered unit; the task slot sorts separately."""
    if unit.slot_type == "instructions" or unit.source_uri.startswith("urn:verdict:task"):
        return -1
    return hydrate_priority_class(Path(unit.source_uri))


def cheap_path_unit_sort_key(
    units: Sequence[ContextUnit], *, token_budget: int
) -> Callable[[ContextUnit], tuple[int, int, int, int, str, str]]:
    """Compiler order: task, one reserved unit per high-value class, then small-first.

    Reserving the smallest fitting ADR, architecture, and README units prevents a
    pile of large ADRs from starving architecture (and vice versa) under budget.
    """
    task_units = [
        unit
        for unit in units
        if unit.slot_type == "instructions" or unit.source_uri.startswith("urn:verdict:task")
    ]
    leftover = token_budget - sum(unit_prompt_token_cost(unit) for unit in task_units)
    reserved: set[str] = set()
    by_class: dict[int, list[ContextUnit]] = {
        HYDRATE_CLASS_ADR: [],
        HYDRATE_CLASS_ARCHITECTURE: [],
        HYDRATE_CLASS_README: [],
        HYDRATE_CLASS_PROJECT: [],
        HYDRATE_CLASS_OTHER: [],
    }
    for unit in units:
        cls = unit_hydrate_class(unit)
        if cls < 0:
            continue
        by_class[cls].append(unit)
    for cls in (
        HYDRATE_CLASS_ADR,
        HYDRATE_CLASS_ARCHITECTURE,
        HYDRATE_CLASS_README,
        HYDRATE_CLASS_PROJECT,
    ):
        ranked = sorted(
            by_class[cls],
            key=lambda item: (len(item.content.encode("utf-8")), item.source_uri, item.unit_id),
        )
        for unit in ranked:
            cost = unit_prompt_token_cost(unit)
            if 0 < cost <= leftover:
                reserved.add(unit.source_uri)
                leftover -= cost
                break

    def order(unit: ContextUnit) -> tuple[int, int, int, int, str, str]:
        if unit.slot_type == "instructions" or unit.source_uri.startswith("urn:verdict:task"):
            return (0, 0, 0, 0, unit.key, unit.unit_id)
        cls = unit_hydrate_class(unit)
        reserve_rank = 0 if unit.source_uri in reserved else 1
        size = len(unit.content.encode("utf-8"))
        return (1, reserve_rank, cls, size, unit.key, unit.unit_id)

    return order


def _file_matches(path: Path, terms: tuple[str, ...]) -> bool:
    haystack = path.as_posix().lower()
    if any(term in haystack for term in terms):
        return True
    try:
        payload = truncate_utf8(path.read_text(encoding="utf-8"), DEFAULT_MAX_FILE_BYTES).lower()
    except (OSError, UnicodeDecodeError):
        return False
    return any(term in payload for term in terms)


def _query_terms(query: str) -> tuple[str, ...]:
    terms = [part.lower() for part in re.findall(r"[A-Za-z0-9_-]{4,}", query)]
    return tuple(term for term in terms if term not in _STOP)


def _slot_for(path: Path) -> SlotType:
    if _is_adr_path(path) or "architecture" in {part.lower() for part in path.parts}:
        return "policy"
    return "evidence"


def _unit_from_file(
    origin: Path,
    path: Path,
    *,
    slot_type: SlotType,
    max_file_bytes: int,
    seen_uris: set[str],
    uri_prefix: str = "",
) -> tuple[ContextUnit | None, HydrateOmission | None]:
    relative = _relative_uri(origin, path)
    source_uri = f"{uri_prefix}{relative}"
    if source_uri in seen_uris:
        return None, None
    try:
        payload = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, HydrateOmission(name=source_uri, reason="source_missing")
    except UnicodeDecodeError:
        return None, HydrateOmission(name=source_uri, reason="unreadable")
    except OSError:
        return None, HydrateOmission(name=source_uri, reason="unreadable")
    content = truncate_utf8(payload, max_file_bytes)
    seen_uris.add(source_uri)
    digest = _digest_text(content)
    unit = ContextUnit(
        unit_id=f"file:{source_uri}",
        slot_type=slot_type,
        key=source_uri,
        content=content,
        source_uri=source_uri,
        source_digest=digest,
        revision="working-tree",
        observed_at=CHEAP_PATH_EPOCH,
        retrieved_at=CHEAP_PATH_EPOCH,
        trust="local-observation",
        authority="workspace",
        created_at=0.0,
    )
    return unit, None


def truncate_utf8(text: str, max_bytes: int) -> str:
    """Bound ``text`` to ``max_bytes`` of UTF-8, never splitting a code point.

    ``max_file_bytes`` is a byte limit: multi-byte content must not be allowed
    to exceed it by counting characters instead (BOD-110).
    """
    if max_bytes <= 0:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _relative_uri(origin: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(origin.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _digest_text(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode()).hexdigest()}"


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _config_paths(workspace_root: Path) -> tuple[Path, ...]:
    return (workspace_root / ".verdict" / "config.toml", Path.home() / ".verdict" / "config.toml")


def _load_toml(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        import tomllib  # type: ignore[import-not-found,unused-ignore]
    except ImportError:
        return _parse_context_toml_subset(path)
    try:
        with path.open("rb") as handle:
            loaded = tomllib.load(handle)
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _parse_context_toml_subset(path: Path) -> dict[str, Any] | None:
    """Tiny ``[context]`` reader for Python 3.10 (no stdlib tomllib)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    in_context = False
    payload: dict[str, Any] = {}
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            in_context = stripped[1:-1].strip() == "context"
            continue
        if not in_context or "=" not in stripped:
            continue
        key, _, rest = stripped.partition("=")
        payload[key.strip()] = _parse_toml_scalar(rest.strip())
    return {"context": payload} if payload else None


def _parse_toml_scalar(raw: str) -> Any:
    text = raw.strip()
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [item.strip().strip('"').strip("'") for item in inner.split(",") if item.strip()]
    return text.strip('"').strip("'")


def _roots_from_config(workspace_root: Path) -> tuple[str, ...] | None:
    for path in _config_paths(workspace_root):
        loaded = _load_toml(path)
        if not loaded:
            continue
        context = loaded.get("context")
        if not isinstance(context, dict):
            continue
        roots = context.get("roots")
        if isinstance(roots, str):
            items = tuple(part.strip() for part in roots.split(",") if part.strip())
            return items or None
        if isinstance(roots, list):
            items = tuple(str(item).strip() for item in roots if str(item).strip())
            return items or None
    return None


def _mcp_root_from_config(workspace_root: Path) -> Path | None:
    for path in _config_paths(workspace_root):
        loaded = _load_toml(path)
        if not loaded:
            continue
        context = loaded.get("context")
        if not isinstance(context, dict):
            continue
        raw = context.get("mcp_root")
        if isinstance(raw, str) and raw.strip():
            candidate = Path(raw.strip()).expanduser()
            return candidate if candidate.is_absolute() else (workspace_root / candidate).resolve()
    return None


__all__ = [
    "CHEAP_PATH_EPOCH",
    "CONTEXT_ROOTS_ENV",
    "DEFAULT_CONTEXT_ROOTS",
    "HYDRATE_CLASS_ADR",
    "HYDRATE_CLASS_ARCHITECTURE",
    "HYDRATE_CLASS_OTHER",
    "HYDRATE_CLASS_PROJECT",
    "HYDRATE_CLASS_README",
    "MCP_CONTEXT_ROOT_ENV",
    "PROJECT_DOC_NAMES",
    "WORKSPACE_ROOT_ENV",
    "HydrateGather",
    "HydrateOmission",
    "cheap_path_unit_sort_key",
    "default_context_roots",
    "gather_cheap_path_units",
    "hydrate_priority_class",
    "resolve_mcp_root",
    "resolve_workspace_root",
    "task_required_uris",
    "truncate_utf8",
]
