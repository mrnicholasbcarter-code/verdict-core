#!/usr/bin/env python3
"""Smoke test for Codiv provider integration (BOD-198).

This script is NOT run in CI. It requires:
- CODIV_API_KEY environment variable
- Live network access
- OmniRoute configuration (for mediated test)

Exit codes:
- 0: Smoke tests passed
- 1: Smoke tests failed
- 2: CODIV_API_KEY missing (expected when key not configured)
- 3: NOT IMPLEMENTED (test path exists but not yet implemented)
"""

import os
import sys


def main() -> int:
    """Run Codiv smoke tests."""

    # Check for CODIV_API_KEY
    if "CODIV_API_KEY" not in os.environ:
        print("CODIV_API_KEY missing", file=sys.stderr)
        print("This is expected if Codiv is not configured.", file=sys.stderr)
        print("To run smoke tests, set CODIV_API_KEY and configure OmniRoute.", file=sys.stderr)
        return 2

    print("=" * 60)
    print("Codiv Smoke Test (BOD-198)")
    print("=" * 60)
    print()
    print("NOT IMPLEMENTED: Smoke tests require live Codiv and OmniRoute configuration.")
    print("Exit 3: test path exists but implementation deferred to operator setup.")
    print()
    print("To implement:")
    print("  1. Direct: POST to CODIV_BASE_URL/v1/chat/completions with CODIV_API_KEY")
    print("  2. OmniRoute: POST to OMNIROUTE_BASE_URL/v1/chat/completions with model codiv/<model>")
    print("  3. Verify provider identity in response metadata")
    print("=" * 60)

    return 3


if __name__ == "__main__":
    sys.exit(main())
