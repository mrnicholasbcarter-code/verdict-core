"""Grading test for json flatten task."""

from solution import flatten_json


def test_simple():
    assert flatten_json({"a": 1}) == {"a": 1}


def test_nested():
    r = flatten_json({"a": {"b": 1}, "c": 2})
    assert r == {"a.b": 1, "c": 2}


def test_list():
    r = flatten_json({"a": [1, 2]})
    assert r == {"a.0": 1, "a.1": 2}


def test_deep():
    r = flatten_json({"a": {"b": {"c": 3}}})
    assert r == {"a.b.c": 3}


def test_empty():
    assert flatten_json({}) == {}


def test_mixed():
    r = flatten_json({"x": {"y": [10, {"z": 20}]}})
    assert r["x.y.0"] == 10
    assert r["x.y.1.z"] == 20
