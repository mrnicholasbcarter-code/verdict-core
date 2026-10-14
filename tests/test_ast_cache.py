"""Tests for BOD-350 route-latency fix: shared AST parse cache.

Context Intelligence parsed each matched file up to 5 times per route
(``_extract_definitions``, ``_extract_references``, and
``CodeGraphEngine.parse_file`` each called ``ast.parse`` independently).
``verdict.ast_cache`` must make this one parse per file per process,
invalidated by mtime/size, without dropping file-level metadata (line
count) when a file has a syntax error.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

from verdict.ast_cache import _clear_cache, parse_cached, read_source_cached
from verdict.code_graph import CodeGraphEngine
from verdict.context_sources import _extract_definitions, _extract_references


def setup_function() -> None:
    _clear_cache()


def test_parse_cached_parses_once_across_repeated_calls(tmp_path: Path, monkeypatch) -> None:
    f = tmp_path / "mod.py"
    f.write_text("def foo():\n    pass\n")

    calls: list[str] = []
    real_parse = ast.parse

    def spy_parse(source, *args, **kwargs):
        calls.append(str(source)[:10])
        return real_parse(source, *args, **kwargs)

    monkeypatch.setattr(ast, "parse", spy_parse)

    first = parse_cached(f)
    second = parse_cached(f)
    assert first is not None and second is not None
    assert first[1] is second[1]  # same cached tree object
    assert len(calls) == 1


def test_parse_cached_invalidates_on_content_change_same_mtime_tick(tmp_path: Path) -> None:
    """The cache key uses mtime_ns (nanosecond), not whole-second mtime, so a
    same-size edit within one coarse mtime tick is still detected."""
    f = tmp_path / "mod.py"
    f.write_text("def foo():\n    pass\n")
    first = parse_cached(f)
    assert first is not None

    # Same byte length, different content -- only mtime_ns changes. Bump the
    # mtime explicitly by a sub-second amount so this is detected even on a
    # filesystem with coarse (whole-second) mtime resolution.
    new_text = "def bar():\n    pass\n"
    assert len(new_text) == len(f.read_text())
    f.write_text(new_text)
    current_ns = f.stat().st_mtime_ns
    os.utime(f, ns=(current_ns + 1_000_000, current_ns + 1_000_000))
    second = parse_cached(f)
    assert second is not None
    assert second[0] != first[0]


def test_parse_cached_returns_none_on_syntax_error(tmp_path: Path) -> None:
    f = tmp_path / "broken.py"
    f.write_text("def foo(:\n")
    assert parse_cached(f) is None


def test_read_source_cached_still_available_for_syntax_error_file(tmp_path: Path) -> None:
    """A syntax-error file still has readable source (for line counts etc.);
    only the parsed tree is unavailable."""
    f = tmp_path / "broken.py"
    f.write_text("def foo(:\nx = 1\n")
    assert parse_cached(f) is None
    source = read_source_cached(f)
    assert source is not None
    assert source.splitlines() == ["def foo(:", "x = 1"]


def test_code_graph_engine_keeps_line_count_on_syntax_error(tmp_path: Path) -> None:
    """Before caching, a SyntaxError file still produced a File node with the
    real line count; the cache must not regress this to line_count=0."""
    f = tmp_path / "broken.py"
    f.write_text("def foo(:\nx = 1\ny = 2\n")
    engine = CodeGraphEngine()
    engine.parse_file(f, tmp_path)
    node = engine.nodes[f"file:{f.name}"]
    assert node.line_count == 3


def test_extract_definitions_and_references_and_code_graph_share_one_parse(
    tmp_path: Path, monkeypatch
) -> None:
    f = tmp_path / "mod.py"
    f.write_text("def target_fn():\n    pass\n\ndef caller():\n    target_fn()\n")

    calls: list[str] = []
    real_parse = ast.parse

    def spy_parse(source, *args, **kwargs):
        calls.append(str(source)[:10])
        return real_parse(source, *args, **kwargs)

    monkeypatch.setattr(ast, "parse", spy_parse)

    _extract_definitions(f, tmp_path, ["target_fn"])
    _extract_references(f, tmp_path, ["target_fn"])
    engine = CodeGraphEngine()
    engine.parse_file(f, tmp_path)

    assert len(calls) == 1, f"expected one shared ast.parse call, got {len(calls)}"
