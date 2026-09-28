"""Grading test for balanced parentheses task."""

from solution import is_balanced


def test_balanced():
    assert is_balanced("()") is True
    assert is_balanced("({[]})") is True
    assert is_balanced("") is True


def test_unbalanced():
    assert is_balanced("([)]") is False
    assert is_balanced("(") is False
    assert is_balanced(")") is False


def test_with_other_chars():
    assert is_balanced("hello(world)") is True
    assert is_balanced("a{b[c]d}e") is True


def test_nested():
    assert is_balanced("((([[{{}}]])))") is True
    assert is_balanced("((()") is False
