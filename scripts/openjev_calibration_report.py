#!/usr/bin/env python3
"""Render an OpenJev calibration report from labeled JSONL records (BOD-203).

Measurement only: this never changes routing, admission or planner behaviour.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from verdict.decision_signals.calibration import evaluate, load_records, render_markdown


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="JSONL calibration records")
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument("--min-confidence", type=float, default=0.6)
    parser.add_argument("--security-threshold", type=float, default=0.5)
    parser.add_argument("--json", action="store_true", help="Print JSON instead of markdown")
    args = parser.parse_args(argv)

    if not args.input.exists():
        print(f"Error: input file not found: {args.input}", file=sys.stderr)
        return 1
    try:
        records = load_records(args.input)
    except ValueError as exc:
        print(f"Error loading records: {exc}", file=sys.stderr)
        return 1
    report = evaluate(
        records,
        threshold=args.threshold,
        min_confidence=args.min_confidence,
        security_threshold=args.security_threshold,
    )
    print(json.dumps(report.to_dict(), indent=2) if args.json else render_markdown(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
