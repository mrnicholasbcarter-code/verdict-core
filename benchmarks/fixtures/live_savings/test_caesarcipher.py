"""Grading test for caesar cipher task."""

from solution import caesar_decrypt, caesar_encrypt


def test_encrypt_basic():
    assert caesar_encrypt("abc", 1) == "bcd"
    assert caesar_encrypt("xyz", 3) == "abc"


def test_preserve_case():
    assert caesar_encrypt("Hello", 1) == "Ifmmp"


def test_non_alpha():
    assert caesar_encrypt("a b!c", 1) == "b c!d"


def test_roundtrip():
    msg = "The Quick Brown Fox!"
    for shift in [1, 5, 13, 25]:
        assert caesar_decrypt(caesar_encrypt(msg, shift), shift) == msg
