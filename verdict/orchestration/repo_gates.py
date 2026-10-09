"""Repo gate discovery: pure, deterministic quality-gate discovery.

Discovery reads only configuration files -- ``pyproject.toml`` (ruff/mypy),
``ruff.toml``, ``.ruff.toml`` (ruff) and ``package.json`` (npm scripts) -- and
stats candidate package directories at the repo root and under ``src/`` to
resolve mypy targets. It returns immutable ``RepoGate`` descriptors. No
execution, no network.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

try:  # Python 3.11+ ships tomllib in the stdlib.
    import tomllib  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised only on Python <3.11
    import tomli as tomllib

_NODE_SCRIPT_ORDER: tuple[str, ...] = ("typecheck", "lint", "format:check")


@dataclass(frozen=True)
class RepoGate:
    """A discovered repo quality gate.

    ``name`` is a stable slug, ``argv`` the deterministic argv to invoke it, and ``source``
    records where it was declared (e.g. ``pyproject:tool.ruff``).
    """

    name: str
    argv: tuple[str, ...]
    source: str


def _load_pyproject(repo: Path) -> dict[str, object]:
    """Load ``pyproject.toml`` from *repo*, or ``{}`` when absent; malformed TOML -> ValueError."""
    path = repo / "pyproject.toml"
    if not path.exists():
        return {}
    try:
        loaded = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"malformed pyproject.toml in {repo}: {exc}") from exc
    return loaded if isinstance(loaded, dict) else {}


def _as_dict(value: object) -> dict[str, object]:
    """Return *value* when it is a mapping, otherwise an empty mapping."""
    return value if isinstance(value, dict) else {}


def _ruff_source(root: dict[str, object], repo: Path) -> str | None:
    """Ruff config source precedence: ``ruff.toml`` > ``.ruff.toml`` > ``pyproject:tool.ruff``."""
    if (repo / "ruff.toml").exists():
        return "ruff.toml"
    if (repo / ".ruff.toml").exists():
        return ".ruff.toml"
    if isinstance(_as_dict(root.get("tool")).get("ruff"), dict):
        return "pyproject:tool.ruff"
    return None


def _python_gates(root: dict[str, object], repo: Path) -> list[RepoGate]:
    """Discover ruff (check + format) and mypy gates from *repo*."""
    tool = _as_dict(root.get("tool"))
    gates: list[RepoGate] = []
    ruff_source = _ruff_source(root, repo)
    if ruff_source is not None:
        gates.append(RepoGate("ruff-check", ("ruff", "check", "."), ruff_source))
        gates.append(RepoGate("ruff-format", ("ruff", "format", "--check", "."), ruff_source))
    mypy = tool.get("mypy")
    if isinstance(mypy, dict):
        argv: list[str] = ["mypy"]
        if mypy.get("strict") is True:
            argv.append("--strict")
        argv.extend(_mypy_targets(mypy, root, repo))
        gates.append(RepoGate("mypy", tuple(argv), "pyproject:tool.mypy"))
    return gates


def _split_scalar_or_list(value: object) -> list[str]:
    """Normalize a list of strings or a comma-separated string into stripped parts."""
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return []


def _mypy_targets(mypy: dict[str, object], root: dict[str, object], repo: Path) -> list[str]:
    """Resolve mypy targets without ever emitting a targetless argv.

    Precedence: ``files`` (list or CSV), ``packages`` (list or CSV -> ``-p <pkg>``),
    then ``[project].name`` package dir at the repo root or under ``src/``.
    """
    targets: list[str] = []
    targets.extend(_split_scalar_or_list(mypy.get("files")))
    if not targets:
        for pkg in _split_scalar_or_list(mypy.get("packages")):
            targets.extend(["-p", pkg])
    if not targets:
        name = _as_dict(root.get("project")).get("name")
        if isinstance(name, str) and name:
            for cand in (name, name.replace("-", "_")):
                for base in (repo, repo / "src"):
                    if (base / cand).is_dir():
                        targets.append(str((base / cand).relative_to(repo)))
                        break
                if targets:
                    break
    if not targets:
        raise ValueError(
            "mypy configured but no target resolvable: set [tool.mypy].files or packages"
        )
    return targets


def _node_gates(repo: Path) -> list[RepoGate]:
    """Discover Node script gates in a fixed, deterministic order."""
    path = repo / "package.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"malformed package.json in {repo}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"package.json root is not a JSON object in {repo}: {type(data).__name__}")
    scripts = _as_dict(data.get("scripts"))
    return [
        RepoGate(name=script, argv=("npm", "run", script), source="package.json:scripts")
        for script in _NODE_SCRIPT_ORDER
        if script in scripts
    ]


def discover_repo_gates(repo: Path) -> tuple[RepoGate, ...]:
    """Discover quality gates declared in *repo*.

    Stable order: Python gates (ruff-check, ruff-format, mypy) then Node script gates
    (typecheck, lint, format:check). A repo declaring nothing returns ``()``.
    """
    repo = Path(repo)
    root = _load_pyproject(repo)
    gates = _python_gates(root, repo)
    gates.extend(_node_gates(repo))
    return tuple(gates)


def describe_gates(gates: Iterable[RepoGate]) -> dict[str, object]:
    """Render *gates* into a receipt dict (``{"gates":[...], "declared": bool}``; empty is ``"none declared"``)."""
    items = [{"name": gate.name, "argv": list(gate.argv), "source": gate.source} for gate in gates]
    if not items:
        return {"gates": [], "declared": False, "note": "none declared"}
    return {"gates": items, "declared": True}
