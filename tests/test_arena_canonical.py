"""Tests for verdict/arena/canonical.py — parse_name, slug, Drop/Parsed."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from verdict.arena.canonical import (
    DROP_HARNESS_VARIANT,
    DROP_ORG_UNMAPPED,
    DROP_UNKNOWN_QUALIFIER,
    DROP_UNPARSEABLE_NAME,
    Drop,
    Parsed,
    parse_name,
    slug,
)
from verdict.arena.crosswalk import load_crosswalk
from verdict.arena.match import build_store_index

WORKTREE = Path(__file__).parent.parent
CW_PATH = WORKTREE / "verdict" / "data" / "arena-crosswalk.json"


@pytest.fixture(scope="module")
def org_providers() -> dict[str, list[str]]:
    return load_crosswalk(CW_PATH).org_providers


# ---- slug tests ----------------------------------------------------------------


def test_slug_lowercases() -> None:
    assert slug("GPT-4o") == "gpt-4o"


def test_slug_replaces_spaces_with_dash() -> None:
    assert slug("Claude Opus 5") == "claude-opus-5"


def test_slug_replaces_dots() -> None:
    assert slug("claude.opus.5") == "claude-opus-5"


def test_slug_replaces_underscores() -> None:
    assert slug("claude_opus_5") == "claude-opus-5"


def test_slug_replaces_colons() -> None:
    assert slug("claude:opus") == "claude-opus"


def test_slug_collapses_multiple_dashes() -> None:
    # multiple separators in a row collapse to one dash
    assert slug("GPT  4o") == "gpt-4o"


def test_slug_strips_leading_trailing_dashes() -> None:
    assert slug("-claude-") == "claude"


def test_slug_mixed_separators() -> None:
    assert slug("GPT-4o_turbo.preview") == "gpt-4o-turbo-preview"


# ---- parse_name round-trip table -----------------------------------------------


@pytest.mark.parametrize(
    "model_name, organization, expected",
    [
        ("GPT-4o", "openai", Parsed(org="openai", key="gpt-4o", effort="default", qualifiers=())),
        (
            "Claude Opus 5 (Max)",
            "anthropic",
            Parsed(org="anthropic", key="claude-opus-5", effort="max", qualifiers=()),
        ),
        (
            "Gemini 4 Carbon (High)",
            "google",
            Parsed(org="google", key="gemini-4-carbon", effort="high", qualifiers=()),
        ),
        (
            "DeepSeek-V4-Pro",
            "deepseek",
            Parsed(org="deepseek", key="deepseek-v4-pro", effort="default", qualifiers=()),
        ),
        (
            # "Thinking" is in MODE_VOCAB — it is appended to the slug key,
            # not placed in qualifiers.  qualifiers only captures _RECORDED_ONLY tokens.
            "Qwen3-Max (Thinking)",
            "alibaba",
            Parsed(org="alibaba", key="qwen3-max-thinking", effort="default", qualifiers=()),
        ),
    ],
)
def test_parse_name_round_trip(
    model_name: str, organization: str, expected: Parsed, org_providers: dict[str, list[str]]
) -> None:
    result = parse_name(model_name, organization, org_providers)
    assert result == expected, f"parse_name({model_name!r}, {organization!r}) → {result!r}"


# ---- Near-miss Drop cases -------------------------------------------------------


def test_parse_name_empty_name_drops(org_providers: dict[str, list[str]]) -> None:
    result = parse_name("", "openai", org_providers)
    assert isinstance(result, Drop)
    assert result.reason == DROP_UNPARSEABLE_NAME


def test_parse_name_harness_variant_drops(org_providers: dict[str, list[str]]) -> None:
    # "Codex Harness" appears as a paren token → harness_variant drop
    result = parse_name("GPT-5.1 (Codex Harness)", "openai", org_providers)
    assert isinstance(result, Drop)
    assert result.reason == DROP_HARNESS_VARIANT


def test_parse_name_unknown_org_drops(org_providers: dict[str, list[str]]) -> None:
    result = parse_name("SomeModel-3", "unknownvendor99", org_providers)
    assert isinstance(result, Drop)
    assert result.reason == DROP_ORG_UNMAPPED


def test_parse_name_unknown_qualifier_drops(org_providers: dict[str, list[str]]) -> None:
    result = parse_name("GPT-4o (Unknown-token)", "openai", org_providers)
    assert isinstance(result, Drop)
    assert result.reason == DROP_UNKNOWN_QUALIFIER


# ---- One-char perturbation -------------------------------------------------------


def test_parse_name_no_store_match_on_one_char_perturbation(
    org_providers: dict[str, list[str]],
) -> None:
    """A one-char suffix makes the key differ → no match in a single-entry index."""
    base_ids = frozenset({"openai/gpt-4o"})
    index = build_store_index(base_ids)

    result_good = parse_name("GPT-4o", "openai", org_providers)
    assert isinstance(result_good, Parsed)
    assert index.get((result_good.org, result_good.key)) == ("openai/gpt-4o",)

    result_bad = parse_name("GPT-4ox", "openai", org_providers)
    if isinstance(result_bad, Parsed):
        # Parsed but the perturbed key must not be found in the index
        found = index.get((result_bad.org, result_bad.key))
        assert found is None, "Perturbed key must not fuzzy-match into the index"
    # Drop is also an acceptable outcome — either way no fuzzy match occurred


# ---- AST no-fuzzy scan ----------------------------------------------------------


@pytest.mark.parametrize(
    "source_file",
    [WORKTREE / "verdict" / "arena" / "canonical.py", WORKTREE / "verdict" / "arena" / "match.py"],
    ids=["canonical", "match"],
)
def test_no_fuzzy_imports(source_file: Path) -> None:
    """canonical.py and match.py must not import any fuzzy-matching library."""
    forbidden = {
        "difflib",
        "fuzzywuzzy",
        "rapidfuzz",
        "thefuzz",
        "Levenshtein",
        "jellyfish",
        "similarity",
        "fuzzy",
    }
    tree = ast.parse(source_file.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                assert top not in forbidden, f"{source_file.name}: forbidden import {alias.name!r}"
        elif isinstance(node, ast.ImportFrom) and node.module:
            top = node.module.split(".")[0]
            assert top not in forbidden, (
                f"{source_file.name}: forbidden import from {node.module!r}"
            )
