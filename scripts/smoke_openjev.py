#!/usr/bin/env python3
"""OpenJev SHADOW mode smoke test (BOD-199).

Exit codes:
  0: Success (real OpenJev call made)
  2: OPENJEV_API_KEY missing
  3: Unimplemented live steps (placeholder)
"""

import os
import sys


def main() -> int:
    """Run OpenJev smoke test."""
    # Check for API key
    api_key = os.environ.get("OPENJEV_API_KEY", "").strip()
    if not api_key:
        print("ERROR: OPENJEV_API_KEY not set", file=sys.stderr)
        print("Set OPENJEV_API_KEY to run live OpenJev smoke test", file=sys.stderr)
        return 2

    # Check for base URL
    base_url = os.environ.get("OPENJEV_BASE_URL", "").strip()
    if not base_url:
        print("WARNING: OPENJEV_BASE_URL not set, using default", file=sys.stderr)
        base_url = "https://api.openjev.dev"

    print(f"OpenJev base URL: {base_url}")
    print(f"API key configured: {len(api_key)} chars")

    # Unimplemented: actual live call
    print("ERROR: Live OpenJev call not implemented (BOD-199 SHADOW-only)", file=sys.stderr)
    print("BOD-203 owns live calibration and promotion", file=sys.stderr)
    return 3


if __name__ == "__main__":
    sys.exit(main())
