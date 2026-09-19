#!/usr/bin/env python3
"""Lightweight merge-proof acceptance smoke for BOD-89.

Full G1-G7 release acceptance remains `.github/workflows/acceptance-gates.yml`.
This smoke verifies the proof contract and core package importability so merge
proof does not silently skip acceptance-shaped checks.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    contract = ROOT / "proof" / "contract.yaml"
    if not contract.is_file():
        print(f"missing proof contract: {contract}", file=sys.stderr)
        return 1
    # Import core package — fail closed if install is broken.
    importlib.import_module("verdict")
    from scripts.proof.contract import load_contract

    loaded = load_contract(contract)
    if loaded.runner != "native":
        print(f"unexpected runner: {loaded.runner}", file=sys.stderr)
        return 1
    required = {"format", "lint", "type", "unit_full", "security_semgrep", "secrets", "build"}
    present = {gate.id for gate in loaded.gates}
    missing = sorted(required - present)
    if missing:
        print(f"contract missing required gates: {missing}", file=sys.stderr)
        return 1
    print("acceptance_smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
