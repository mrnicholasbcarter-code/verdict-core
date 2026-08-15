"""Compatibility wrapper for the credential-free flagship quickstart fixture.

This wrapper runs from a clean source checkout without a pre-install: it adds
the repository root to ``sys.path`` so the in-repo ``verdict`` package is
importable, then delegates to ``verdict.flagship_demo``. The demo is a
deterministic, credential-free fixture simulation (in-memory catalog and
runtime observations); it is not a live provider call or production-readiness
proof. See ``docs/archive/DEMO.md`` for the simulation contract.

Run from the repository root::

    python scripts/flagship_demo.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Allow `python scripts/flagship_demo.py` to run from a clean checkout with no
# pre-install: insert the repository root (parent of this file's directory) on
# sys.path so the local `verdict` package resolves before any installed copy.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from verdict.flagship_demo import build_demo_result, run_demo  # noqa: E402


def main() -> None:
    """Print the deterministic demo result as JSON for source checkouts."""

    print(json.dumps(run_demo(), indent=2, sort_keys=True))


__all__ = ["build_demo_result", "main", "run_demo"]


if __name__ == "__main__":
    main()
