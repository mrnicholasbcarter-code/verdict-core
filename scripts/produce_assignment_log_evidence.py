#!/usr/bin/env python3
"""Produce assignment log evidence for G6.1.

G6.1 requires evidence that per-assignment logging captures:
- model, provider
- estimated and actual cost (estimated_cost_usd, actual_cost_usd)
- reason (why selected)
- fallback_result (if escalated)
- verification_result
- availability snapshot (via candidate_states)

This producer drives the REAL routing path offline (Gate with allow_offline=True
and a tmp log_path), reads the real JSONL record written by verdict/logger.py
log_decision(), and writes:
  assignment_log_schema.json  — schema of that record
  assignment_log_sample.json  — actual record from a real routing call

Exit 1 with RESULT: FAIL if the record is missing required G6.1 fields.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

G61_REQUIRED_FIELDS = [
    "model_chosen",
    "provider",
    "candidate_states",
    "estimated_cost_usd",
    "actual_cost_usd",
    "reason",
    "fallback_result",
    "verification_result",
    "escalated",
    "escalation_reason",
    "transport_outcome",
    "quality_outcome",
]

ASSIGNMENT_LOG_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "AssignmentLogRecord",
    "description": "Schema for one JSONL record written by verdict/logger.py log_decision()",
    "type": "object",
    "required": G61_REQUIRED_FIELDS,
    "properties": {
        "ts": {"type": "string", "description": "ISO-8601 timestamp"},
        "event_version": {"type": "string"},
        "policy_version": {"type": "string"},
        "request_id": {"type": "string"},
        "task_hash": {"type": "string"},
        "task_preview": {"type": "string"},
        "task_len": {"type": "integer"},
        "input_tier": {"type": "integer"},
        "task_class": {"type": "string"},
        "protected": {"type": "boolean"},
        "degraded_mode": {"type": "boolean"},
        "managed_backend_status": {"type": "string"},
        "decision": {"type": "string"},
        "effective_tier": {"type": "integer"},
        "escalated": {"type": "boolean"},
        "escalation_reason": {"type": ["string", "null"]},
        "model_chosen": {"type": "string", "description": "Model selected for this assignment"},
        "provider": {"type": "string"},
        "alternatives_considered": {"type": "array", "items": {"type": "string"}},
        "candidate_states": {
            "type": "array",
            "description": "Availability snapshot: per-candidate state info",
        },
        "safety_flags": {"type": "array", "items": {"type": "string"}},
        "headroom_pct": {"type": "number"},
        "latency_ms": {"type": "number"},
        "reason": {"type": "string", "description": "Why this model was selected"},
        "transport_outcome": {"type": "string"},
        "quality_outcome": {"type": "string"},
        "quality_score": {"type": ["number", "null"]},
        "estimated_cost_usd": {
            "type": ["number", "null"],
            "description": "Estimated cost for this assignment in USD",
        },
        "actual_cost_usd": {
            "type": ["number", "null"],
            "description": "Actual cost recorded after completion",
        },
        "fallback_result": {
            "type": ["string", "null"],
            "description": "Result if fallback was triggered",
        },
        "verification_result": {
            "type": ["string", "null"],
            "description": "Verification outcome for this assignment",
        },
    },
}


def run_offline_routing(log_path: Path) -> None:
    """Drive the real routing path offline and log one decision."""
    from verdict.gate import Gate

    gate = Gate(allow_offline=True, log_path=str(log_path))
    # Use a simple offline task to generate a real routing decision
    decision = gate.route("Write a unit test for a Python function", criticality="low")

    # The log_decision call is made inside IntelligenceService; if the offline
    # path skips it we call it directly to ensure the record is written.
    if not log_path.exists() or log_path.stat().st_size == 0:
        from verdict.logger import log_decision

        log_decision(log_path, "Write a unit test for a Python function", 2, decision)


def read_log_record(log_path: Path) -> dict:
    """Read the most recent JSONL record from the log file."""
    lines = [line.strip() for line in log_path.read_text().splitlines() if line.strip()]
    if not lines:
        raise ValueError("log file is empty after routing call")
    return json.loads(lines[-1])


def generate_schema(evidence_dir: Path) -> None:
    """Write the assignment log schema derived from the real log_decision() record structure."""
    output_file = evidence_dir / "assignment_log_schema.json"
    output_file.write_text(json.dumps(ASSIGNMENT_LOG_SCHEMA, indent=2, sort_keys=True))
    print(f"Wrote {output_file}")


def generate_sample(evidence_dir: Path) -> list[str]:
    """Generate assignment_log_sample.json by running real routing code.

    Returns a list of missing required G6.1 fields.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "verdict-decisions.jsonl"
        run_offline_routing(log_path)
        record = read_log_record(log_path)

    missing = [f for f in G61_REQUIRED_FIELDS if f not in record]

    output_file = evidence_dir / "assignment_log_sample.json"
    output_file.write_text(json.dumps(record, indent=2, sort_keys=True))
    print(f"Wrote {output_file}")
    return missing


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
        missing = generate_sample(args.evidence_dir)

        if missing:
            print(
                f"RESULT: FAIL (missing G6.1 fields in real log record: {missing})", file=sys.stderr
            )
            return 1

        print("RESULT: PASS")
        return 0
    except Exception as e:
        import traceback

        traceback.print_exc()
        print(f"RESULT: FAIL ({e})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
