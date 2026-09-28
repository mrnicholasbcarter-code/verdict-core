#!/usr/bin/env python3
"""Create the recorded, credential-free flagship failover run.

This is an OFFLINE-SCENARIO. It uses static inventory and scripted worker/OCR
process boundaries, but drives the real eligibility, recovery, review, event,
and receipt pipeline. It does not report live model performance, costs, or savings.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

# Permit ``python scripts/demo_scenario.py`` from a source checkout without install.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

FLAGSHIP_RUN_ID = "offline-flagship-failover"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs_dir", type=Path, help="directory that will contain the run")
    parser.add_argument(
        "--run-id", default=FLAGSHIP_RUN_ID, help=f"run directory name (default: {FLAGSHIP_RUN_ID})"
    )
    return parser.parse_args()


def main() -> int:
    from verdict.orchestration.demo_scenario import run_flagship_scenario

    args = _parse_args()
    workspace = Path(tempfile.mkdtemp(prefix="verdict-offline-demo-"))
    try:
        result = run_flagship_scenario(args.runs_dir, workspace_root=workspace, run_id=args.run_id)
        print(result.run_dir.resolve())
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
