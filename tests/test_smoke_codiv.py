"""Tests for smoke_codiv.py script (BOD-198)."""

import subprocess
import sys
import unittest


class TestSmokeCodiScript(unittest.TestCase):
    """Test smoke_codiv.py exit codes."""

    def test_without_key_exits_2(self):
        """Without CODIV_API_KEY, script exits 2."""
        result = subprocess.run(
            [sys.executable, "scripts/smoke_codiv.py"],
            capture_output=True,
            env={"PATH": "/usr/bin:/bin"},  # Clean env without CODIV_API_KEY
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn(b"CODIV_API_KEY missing", result.stderr)

    def test_with_key_exits_3_not_implemented(self):
        """With CODIV_API_KEY, script exits 3 (not implemented)."""
        result = subprocess.run(
            [sys.executable, "scripts/smoke_codiv.py"],
            capture_output=True,
            env={"CODIV_API_KEY": "test-key-placeholder", "PATH": "/usr/bin:/bin"},
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn(b"NOT IMPLEMENTED", result.stdout)


if __name__ == "__main__":
    unittest.main()
