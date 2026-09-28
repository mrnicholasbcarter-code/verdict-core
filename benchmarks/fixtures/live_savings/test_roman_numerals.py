"""Grading test for roman numerals task."""

from solution import from_roman, to_roman


def test_basic():
    assert to_roman(1) == "I"
    assert to_roman(4) == "IV"
    assert to_roman(9) == "IX"
    assert to_roman(40) == "XL"
    assert to_roman(1994) == "MCMXCIV"
    assert to_roman(3999) == "MMMCMXCIX"


def test_from_roman():
    assert from_roman("I") == 1
    assert from_roman("IV") == 4
    assert from_roman("MCMXCIV") == 1994


def test_roundtrip():
    for n in [1, 42, 100, 999, 2025, 3999]:
        assert from_roman(to_roman(n)) == n
