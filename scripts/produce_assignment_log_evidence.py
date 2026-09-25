#!/usr/bin/env python3
"""Produce assignment log evidence for G6.1.

G6.1 requires per-assignment logging with:
- model, provider
- availability snapshot
- estimated/actual cost
- reason (why selected)
- fallback (if escalated)
- verification result
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from verdict.assignment_logger import ASSIGNMENT_LOG_SCHEMA, create_sample_log


def generate_schema(evidence_dir: Path) -> None:
    """Generate assignment_log_schema.json."""
    output_file = evidence_dir / "assignment_log_schema.json"
    output_file.write_text(json.dumps(ASSIGNMENT_LOG_SCHEMA, indent=2, sort_keys=True))
    print(f"Wrote {output_file}")


def generate_sample(evidence_dir: Path) -> None:
    """Generate assignment_log_sample.json by running routing code."""
    sample = create_sample_log()

    # Validate sample against schema
    _validate_sample(sample)

    output_file = evidence_dir / "assignment_log_sample.json"
    output_file.write_text(json.dumps(sample, indent=2, sort_keys=True))
    print(f"Wrote {output_file}")


def _validate_sample(sample: dict) -> None:
    """Validate that sample conforms to schema."""
    required_fields = [
        "assignment_id",
        "timestamp",
        "model",
        "provider",
        "availability_snapshot",
        "estimated_cost_usd",
        "reason",
    ]

    missing = [f for f in required_fields if f not in sample]
    if missing:
        raise ValueError(f"Sample missing required fields: {missing}")

    # Type checks
    if not isinstance(sample["model"], str):
        raise ValueError("model must be string")
    if not isinstance(sample["provider"], str):
        raise ValueError("provider must be string")
    if not isinstance(sample["availability_snapshot"], dict):
        raise ValueError("availability_snapshot must be object")
    if not isinstance(sample["estimated_cost_usd"], (int, float)):
        raise ValueError("estimated_cost_usd must be number")
    if not isinstance(sample["reason"], str):
        raise ValueError("reason must be string")


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Produce assignment log evidence")
    parser.add_argument(
        "--evidence-dir", type=Path, required=True, help="Directory to write evidence artifacts"
    )
    args = parser.parse_args()

    args.evidence_dir.mkdir(parents=True, exist_ok=True)

    try:
        generate_schema(args.evidence_dir)
        generate_sample(args.evidence_dir)
        print("RESULT: PASS")
        return 0
    except Exception as e:
        print(f"RESULT: FAIL ({e})", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
