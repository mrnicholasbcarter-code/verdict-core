"""Compatibility wrapper for the installed credential-free quickstart fixture."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from verdict.flagship_demo import build_demo_result, run_accepted_and_denied_demo, run_demo


def main() -> None:
    """Print the deterministic demo result as JSON for source checkouts."""

    print(json.dumps(run_accepted_and_denied_demo(), indent=2, sort_keys=True))


__all__ = ["build_demo_result", "main", "run_accepted_and_denied_demo", "run_demo"]


if __name__ == "__main__":
    main()
