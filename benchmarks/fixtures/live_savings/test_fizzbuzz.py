"""Grading test for fizzbuzz task."""

from solution import fizzbuzz


def test_basic():
    assert fizzbuzz(1) == ["1"]
    assert fizzbuzz(3) == ["1", "2", "Fizz"]


def test_fizzbuzz_15():
    r = fizzbuzz(15)
    assert r[2] == "Fizz"  # 3
    assert r[4] == "Buzz"  # 5
    assert r[14] == "FizzBuzz"  # 15
    assert r[0] == "1"
    assert r[6] == "7"


def test_length():
    assert len(fizzbuzz(20)) == 20
