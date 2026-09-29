"""Test receipt_digest_info returns digest algo, prefix, and max size."""

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sol = importlib.import_module("solution")


def test_algorithm():
    info = sol.receipt_digest_info()
    assert info["algorithm"] == "BLAKE2s-128"


def test_prefix():
    info = sol.receipt_digest_info()
    assert info["prefix"] == "ud1:"


def test_max_file_size():
    info = sol.receipt_digest_info()
    assert info["max_packed_bytes"] == 8192


def test_truncated_flag():
    info = sol.receipt_digest_info()
    assert info["truncated_flag"] == "truncated_by_cap"
