"""Grading test for flatten task."""

from solution import flatten


def test_simple():
    assert flatten([1, 2, 3]) == [1, 2, 3]


def test_nested():
    assert flatten([1, [2, [3, 4], 5], 6]) == [1, 2, 3, 4, 5, 6]


def test_empty():
    assert flatten([]) == []


def test_deeply_nested():
    assert flatten([[[1]], [[2]], [[[3]]]]) == [1, 2, 3]


def test_mixed_types():
    assert flatten(["a", ["b", ["c"]]]) == ["a", "b", "c"]
