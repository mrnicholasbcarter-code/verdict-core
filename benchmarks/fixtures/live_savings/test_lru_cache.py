"""Grading test for LRU cache task."""

from solution import LRUCache


def test_basic_put_get():
    c = LRUCache(2)
    c.put(1, 10)
    c.put(2, 20)
    assert c.get(1) == 10
    assert c.get(2) == 20


def test_miss():
    c = LRUCache(2)
    assert c.get(99) == -1


def test_eviction():
    c = LRUCache(2)
    c.put(1, 1)
    c.put(2, 2)
    c.put(3, 3)  # evicts key 1
    assert c.get(1) == -1
    assert c.get(2) == 2
    assert c.get(3) == 3


def test_use_refreshes():
    c = LRUCache(2)
    c.put(1, 1)
    c.put(2, 2)
    c.get(1)  # refresh key 1
    c.put(3, 3)  # evicts key 2, not 1
    assert c.get(1) == 1
    assert c.get(2) == -1


def test_update_existing():
    c = LRUCache(2)
    c.put(1, 1)
    c.put(1, 10)
    assert c.get(1) == 10
