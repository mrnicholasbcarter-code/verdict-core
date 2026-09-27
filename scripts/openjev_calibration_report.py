#!/usr/bin/env python3
"""CLI for OpenJev calibration report generation (BOD-203).

Usage:
    openjev_calibration_report.py <input.jsonl> [options]

Options:
    --threshold FLOAT       Frontier decision threshold (default: 0.5)
    --min-confidence FLOAT  Minimum confidence for usable signal (default: 0.6)
    --security-threshold FLOAT  Security sensitivity threshold (default: 0.5)
    --json                  Output as JSON instead of markdown
    -h, --help              Show this message
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from docopt import docopt

from verdict.decision_signals.calibration import evaluate, load_records, render_markdown


def main() -> int:
    """Run the calibration report CLI."""
    args = docopt(__doc__)

    input_path = Path(args["<input.jsonl>"])
    if not input_path.exists():
        print(f"Error: Input file not found: {input_path}", file=sys.stderr)
        return 1

    try:
        records = load_records(input_path)
    except ValueError as e:
        print(f"Error loading records: {e}", file=sys.stderr)
        return 1

    threshold = float(args["--threshold"]) if args["--threshold"] else 0.5
    min_confidence = float(args["--min-confidence"]) if args["--min-confidence"] else 0.6
    security_threshold = (
        float(args["--security-threshold"]) if args["--security-threshold"] else 0.5
    )

    report = evaluate(
        records,
        threshold=threshold,
        min_confidence=min_confidence,
        security_threshold=security_threshold,
    )

    if args["--json"]:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(render_markdown(report))

    return 0


if __name__ == "__main__":
    sys.exit(main())
