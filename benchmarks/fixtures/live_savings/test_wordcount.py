"""Grading test for wordcount task."""

from solution import word_frequencies


def test_basic():
    r = word_frequencies("hello world hello")
    assert r["hello"] == 2
    assert r["world"] == 1


def test_case_insensitive():
    r = word_frequencies("Hello HELLO hello")
    assert r["hello"] == 3


def test_punctuation():
    r = word_frequencies("one, two. one!")
    assert r["one"] == 2
    assert r["two"] == 1


def test_empty():
    assert word_frequencies("") == {}
