"""Grading test for binary search task."""

from solution import binary_search


def test_found():
    assert binary_search([1, 3, 5, 7, 9], 5) == 2
    assert binary_search([1, 3, 5, 7, 9], 1) == 0
    assert binary_search([1, 3, 5, 7, 9], 9) == 4


def test_not_found():
    assert binary_search([1, 3, 5, 7, 9], 4) == -1
    assert binary_search([], 1) == -1


def test_single():
    assert binary_search([42], 42) == 0
    assert binary_search([42], 1) == -1
