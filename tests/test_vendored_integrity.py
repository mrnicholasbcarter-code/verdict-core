"""Test vendored file integrity."""

from __future__ import annotations

import hashlib
from pathlib import Path


def test_vendored_validator_byte_identity():
    """The vendored validator matches the documented SHA256."""
    vendored_file = (
        Path(__file__).parent.parent / "verdict" / "openspec_vendor" / "validate_openspec_change.py"
    )
    expected_sha256 = "174ec1b45f2f24be96db2fd17e8d6dfa63edfbbbf2b90b4f5a8dc6841751dec9"

    actual_sha256 = hashlib.sha256(vendored_file.read_bytes()).hexdigest()

    assert actual_sha256 == expected_sha256, (
        f"Vendored file SHA256 mismatch: expected {expected_sha256}, got {actual_sha256}. "
        "The file may have been edited. Re-vendor from verdict-ecosystem@fec5556 if intentional."
    )
