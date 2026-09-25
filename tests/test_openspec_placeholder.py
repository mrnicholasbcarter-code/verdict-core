"""Comprehensive placeholder detection tests."""

from __future__ import annotations

import pytest

from verdict.openspec_lifecycle import _is_placeholder_only


@pytest.mark.parametrize(
    "text,expected",
    [
        # Single placeholders
        ("TBD", True),
        ("TODO", True),
        ("N/A", True),
        ("na", True),
        ("none", True),
        ("-", True),
        ("...", True),
        ("…", True),
        ("tba", True),
        ("pending", True),
        # With trailing punctuation
        ("tbd.", True),
        ("n/a.", True),
        ("todo!", True),
        ("TBD:", True),
        ("pending;", True),
        # With bullets
        ("- TBD", True),
        ("* TODO", True),
        ("• N/A", True),
        ("1. tbd", True),
        ("12. pending", True),
        # Multiple placeholders
        ("- TBD\n- TODO", True),
        ("TBD\n\nTODO", True),
        ("- tbd\n- n/a\n- pending", True),
        # Mixed with empty lines
        ("TBD\n\n\nTODO", True),
        ("  \n- TBD\n  \n- TODO  \n", True),
        # Real content (should be False)
        ("This is real content", False),
        ("N/A is not acceptable here because we need details", False),
        ("The system must handle TBD items", False),
        ("- Real task 1\n- Real task 2", False),
        ("Context: some real explanation", False),
        ("1. First step\n2. Second step", False),
        # Mixed (at least one real line = False)
        ("TBD\nReal content", False),
        ("- TBD\n- Real task", False),
        ("TODO\n\nActual description here", False),
        # Empty
        ("", True),
        ("   ", True),
        ("\n\n", True),
    ],
)
def test_is_placeholder_only_parametrized(text: str, expected: bool) -> None:
    """Test placeholder detection with various inputs."""
    assert _is_placeholder_only(text) == expected, f"Failed for: {text!r}"


def test_placeholder_detection_is_case_insensitive() -> None:
    """Placeholder detection is case-insensitive."""
    assert _is_placeholder_only("TBD")
    assert _is_placeholder_only("tbd")
    assert _is_placeholder_only("Tbd")
    assert _is_placeholder_only("TODO")
    assert _is_placeholder_only("todo")


def test_placeholder_detection_strips_bullets_and_punctuation() -> None:
    """Bullets and trailing punctuation are stripped before checking."""
    assert _is_placeholder_only("- TBD.")
    assert _is_placeholder_only("* N/A!")
    assert _is_placeholder_only("• pending;")
    assert _is_placeholder_only("1. TODO:")


def test_real_sentence_with_placeholder_word_is_not_placeholder() -> None:
    """Real sentences containing placeholder words are not placeholders."""
    assert not _is_placeholder_only("N/A is not an acceptable placeholder. Please provide details.")
    assert not _is_placeholder_only("The TBD items must be filled in later.")
    assert not _is_placeholder_only("TODO: implement this feature correctly")
