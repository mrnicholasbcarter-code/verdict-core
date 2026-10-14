"""Shared, process-local AST parse cache keyed by (path, mtime_ns, size).

Context Intelligence's code providers (``NativeCodeProvider`` for
definitions/references and ``NativeGraphProvider`` via ``CodeGraphEngine``)
each parsed every matched file independently, so one route could re-parse the
same ~700 files up to 5 times. This cache holds one parsed tree per file per
process, invalidated by mtime/size, so each file is parsed at most once per
route.

Source text is cached separately from the parsed tree: a file with a syntax
error still has readable source (line counts, raw text), so a parse failure
must not also drop the cached read.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SOURCE_CACHE: dict[tuple[str, int, int], str | None] = {}
_TREE_CACHE: dict[tuple[str, int, int], ast.AST | None] = {}
_MAX_ENTRIES = 4096


def _key(path: Path) -> tuple[str, int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (str(path), stat.st_mtime_ns, stat.st_size)


def read_source_cached(path: Path) -> str | None:
    """Return the file's text, reading at most once per file state.

    Returns ``None`` only on a read error (missing file, permissions).
    """
    key = _key(path)
    if key is None:
        return None
    if key in _SOURCE_CACHE:
        return _SOURCE_CACHE[key]
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        source = None
    if len(_SOURCE_CACHE) >= _MAX_ENTRIES:
        _SOURCE_CACHE.clear()
    _SOURCE_CACHE[key] = source
    return source


def parse_cached(path: Path) -> tuple[str, ast.AST] | None:
    """Return ``(source, tree)`` for ``path``, parsing at most once per file state.

    Returns ``None`` when the file cannot be read or has a syntax error
    (callers treat the file as unparsable, matching prior per-call
    ``try/except`` behavior for symbol extraction).
    """
    key = _key(path)
    if key is None:
        return None
    source = read_source_cached(path)
    if source is None:
        return None
    if key in _TREE_CACHE:
        tree = _TREE_CACHE[key]
        return (source, tree) if tree is not None else None
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        tree = None
    if len(_TREE_CACHE) >= _MAX_ENTRIES:
        _TREE_CACHE.clear()
    _TREE_CACHE[key] = tree
    return (source, tree) if tree is not None else None


def _clear_cache() -> None:
    """Drop all cached entries. Tests use this between cases."""
    _SOURCE_CACHE.clear()
    _TREE_CACHE.clear()
