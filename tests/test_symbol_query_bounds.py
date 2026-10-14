"""Tests for symbol extraction bounds: plain query words must not trigger code scans."""

from __future__ import annotations

from pathlib import Path

import pytest

from verdict.context_sources import (
    NativeCodeProvider,
    NativeGraphProvider,
    _is_code_identifier,
    _symbols_from_hints,
)


def test_is_code_identifier_shapes() -> None:
    # Plain English words / stopwords must NOT be code identifiers
    assert not _is_code_identifier("Reply")
    assert not _is_code_identifier("with")
    assert not _is_code_identifier("just")
    assert not _is_code_identifier("ok")
    assert not _is_code_identifier("the")
    assert not _is_code_identifier("and")
    assert not _is_code_identifier("for")
    assert not _is_code_identifier("hello")
    assert not _is_code_identifier("world")

    # Code identifiers: snake_case, screaming snake, private
    assert _is_code_identifier("parse_file")
    assert _is_code_identifier("target_fn")
    assert _is_code_identifier("MAX_TOKENS")
    assert _is_code_identifier("_private_func")
    assert _is_code_identifier("get_user_by_id")

    # Code identifiers: camelCase
    assert _is_code_identifier("camelCase")
    assert _is_code_identifier("formatString")
    assert _is_code_identifier("getUserData")

    # Code identifiers: PascalCase / Acronyms
    assert _is_code_identifier("NativeGraphProvider")
    assert _is_code_identifier("HealthCache")
    assert _is_code_identifier("XMLParser")
    assert _is_code_identifier("HTTPResponse")

    # Numbers with letters
    assert _is_code_identifier("utf8")
    assert _is_code_identifier("sha256")
    assert _is_code_identifier("var1")


def test_symbols_from_hints_plain_query_words_produce_no_symbols() -> None:
    # "Reply with just: ok" should not extract any symbols
    symbols = _symbols_from_hints(None, "Reply with just: ok")
    assert symbols == ()

    # Stopwords should not extract symbols
    symbols = _symbols_from_hints(None, "the and for with from")
    assert symbols == ()


def test_symbols_from_hints_identifies_code_symbols_in_query() -> None:
    symbols = _symbols_from_hints(None, "where is parse_file defined?")
    assert symbols == ("parse_file",)

    symbols = _symbols_from_hints(None, "explain how NativeGraphProvider and formatResponse work")
    assert "NativeGraphProvider" in symbols
    assert "formatResponse" in symbols
    assert "explain" not in symbols
    assert "work" not in symbols


def test_symbols_from_hints_explicit_hints_preserved() -> None:
    # Explicit hints bypass query inspection entirely
    symbols = _symbols_from_hints({"symbols": ["Reply", "target"]}, "Reply with just: ok")
    assert symbols == ("Reply", "target")

    symbols = _symbols_from_hints({"target_symbols": ["CustomSymbol"]}, "unrelated query")
    assert symbols == ("CustomSymbol",)


def test_plain_query_yields_no_file_scan_in_providers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Create dummy repo with .py files
    (tmp_path / "mod.py").write_text("def reply_func(): pass\n")
    (tmp_path / "other.py").write_text("def just_do_it(): pass\n")

    parsed_files: list[str] = []
    import ast

    real_parse = ast.parse

    def spy_parse(source, *args, **kwargs):
        parsed_files.append(str(source)[:20])
        return real_parse(source, *args, **kwargs)

    monkeypatch.setattr(ast, "parse", spy_parse)

    code_res = NativeCodeProvider().provide(
        capability_id="code.definitions", query="Reply with just: ok", repo_root=tmp_path
    )
    assert code_res.units == ()
    assert code_res.gap_reason in (
        "no_symbol_seed",
        "No target symbols specified in query or hints",
    )
    assert parsed_files == []

    graph_res = NativeGraphProvider().provide(
        capability_id="code.graph", query="Reply with just: ok", repo_root=tmp_path
    )
    assert graph_res.units == ()
    assert graph_res.gap_reason in (
        "no_symbol_seed",
        "No target symbols specified in query or hints",
    )
    assert parsed_files == []
