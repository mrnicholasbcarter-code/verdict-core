#!/usr/bin/env python3
"""Produce contract parity evidence for G4.2 and G4.3.

G4.2: contract_parity_matrix.md - field-by-field comparison of Python vs TypeScript
G4.3: parity_fixture_results.json - shared fixtures run through Python verifier
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import fields
from pathlib import Path
from typing import Any, get_args, get_origin, get_type_hints

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from verdict.contracts import (
    AvailabilitySnapshot,
    ExecutionEnvelope,
    RoutingDecisionContract,
    RuntimeCandidate,
    TaskSpec,
)


def python_type_name(hint: Any) -> str:
    """Convert Python type hint to readable string."""
    origin = get_origin(hint)
    if origin is None:
        if hasattr(hint, "__name__"):
            return hint.__name__
        return str(hint)

    args = get_args(hint)
    if origin is list:
        if args:
            return f"list[{python_type_name(args[0])}]"
        return "list"
    if origin is dict:
        if len(args) == 2:
            return f"dict[{python_type_name(args[0])}, {python_type_name(args[1])}]"
        return "dict"
    if origin is tuple:
        if args:
            return f"tuple[{', '.join(python_type_name(a) for a in args)}]"
        return "tuple"
    # Handle Union (including Optional)
    if origin is type(None) or (hasattr(origin, "__name__") and "Union" in str(origin)):
        if args:
            return " | ".join(python_type_name(a) for a in args)
        return "None"

    return str(hint)


def extract_python_schema(cls: type) -> dict[str, Any]:
    """Extract field schema from Python dataclass."""
    from dataclasses import MISSING

    type_hints = get_type_hints(cls)
    schema_fields = {}
    for f in fields(cls):
        # A field is required if it has no default value and no default_factory
        required = f.default is MISSING and f.default_factory is MISSING
        type_str = python_type_name(type_hints.get(f.name, f.type))
        schema_fields[f.name] = {
            "type": type_str,
            "required": required,
            "has_default": f.default is not MISSING or f.default_factory is not MISSING,
        }
    return schema_fields


def extract_typescript_schema(contract_name: str, ts_file: Path) -> dict[str, Any] | None:
    """Extract field schema from TypeScript Zod schema."""
    if not ts_file.exists():
        return None

    content = ts_file.read_text()

    # Find the schema definition
    schema_var = {
        "TaskSpec": "taskSpecSchema",
        "RoutingDecision": "routingDecisionSchema",
        "AvailabilitySnapshot": "availabilitySnapshotSchema",
        "RuntimeCandidate": "runtimeCandidateSchema",
        "ExecutionEnvelope": "executionEnvelopeSchema",
    }.get(contract_name)

    if not schema_var:
        return None

    # This is a simplified parser - in production we'd use proper TypeScript parsing
    # For now, extract the basic structure
    import re

    pattern = rf"{schema_var}\s*=\s*z\.object\((.*?)\)\.strict\(\)"
    match = re.search(pattern, content, re.DOTALL)
    if not match:
        return None

    obj_content = match.group(1)

    # Parse field definitions (simplified)
    field_pattern = r"(\w+):\s*([^,]+?)(?:,|\})"
    fields_dict = {}
    for field_match in re.finditer(field_pattern, obj_content):
        field_name = field_match.group(1)
        field_def = field_match.group(2).strip()

        # Determine if required or has default
        has_default = ".default(" in field_def or ".optional()" in field_def
        required = not has_default

        # Extract type (simplified)
        type_str = field_def
        if ".default(" in type_str:
            type_str = type_str.split(".default(")[0]
        if ".optional()" in type_str:
            type_str = type_str.split(".optional()")[0]

        fields_dict[field_name] = {
            "type": type_str,
            "required": required,
            "has_default": has_default,
        }

    return fields_dict


def generate_parity_matrix(evidence_dir: Path) -> None:
    """Generate contract_parity_matrix.md comparing Python vs TypeScript."""
    contracts = [
        ("TaskSpec", TaskSpec),
        ("RoutingDecision", RoutingDecisionContract),
        ("AvailabilitySnapshot", AvailabilitySnapshot),
        ("RuntimeCandidate", RuntimeCandidate),
        ("ExecutionEnvelope", ExecutionEnvelope),
    ]

    ts_file = Path("contracts/src/index.ts")

    lines = [
        "# Contract Parity Matrix",
        "",
        "Python vs TypeScript contract field comparison for G4.2.",
        "",
        "Generated from:",
        "- Python: `verdict/contracts.py`",
        "- TypeScript: `contracts/src/index.ts`",
        "- JSON Schema: `verdict/schemas/contracts.v1.json`",
        "",
    ]

    for name, py_cls in contracts:
        lines.append(f"## {name}")
        lines.append("")

        py_schema = extract_python_schema(py_cls)
        ts_schema = extract_typescript_schema(name, ts_file) if ts_file.exists() else None
        if ts_schema is None:
            ts_schema = {}

        # All fields from both schemas
        all_fields = sorted(set(py_schema.keys()) | set(ts_schema.keys()))

        lines.append(
            "| Field | Python Type | Python Required | TypeScript Type | TypeScript Required | Status |"
        )
        lines.append(
            "|-------|-------------|-----------------|-----------------|---------------------|--------|"
        )

        for field_name in all_fields:
            py_info = py_schema.get(field_name, {})
            ts_info = ts_schema.get(field_name, {})

            py_type = py_info.get("type", "MISSING")
            py_req = "✓" if py_info.get("required") else "✗"
            ts_type = ts_info.get("type", "MISSING")
            ts_req = "✓" if ts_info.get("required") else "✗"

            # Determine status
            if not py_info:
                status = "TS-only"
            elif not ts_info:
                status = "PY-only"
            elif py_req != ts_req:
                status = "MISMATCH"
            else:
                status = "OK"

            lines.append(
                f"| `{field_name}` | {py_type} | {py_req} | {ts_type} | {ts_req} | {status} |"
            )

        lines.append("")

    output_file = evidence_dir / "contract_parity_matrix.md"
    output_file.write_text("\n".join(lines))
    print(f"Wrote {output_file}")


def run_python_fixtures(evidence_dir: Path, ts_results_file: Path | None) -> None:
    """Run shared fixtures through Python verifier and record results."""
    # Use the flagship demo as the primary fixture
    from verdict.flagship_demo import run_accepted_and_denied_demo

    demo_results = run_accepted_and_denied_demo()

    results = {
        "python": {
            "accepted": {"verdict": demo_results["accepted"]["verdict"], "decision_count": 1},
            "denied": {"verdict": demo_results["denied"]["verdict"], "decision_count": 1},
        },
        "typescript": {
            "status": "not_run",
            "note": "TypeScript contract tests run separately in contracts/ package",
        },
    }

    # If TypeScript results were provided, include them
    if ts_results_file and ts_results_file.exists():
        ts_data = json.loads(ts_results_file.read_text())
        results["typescript"] = ts_data

    output_file = evidence_dir / "parity_fixture_results.json"
    output_file.write_text(json.dumps(results, indent=2, sort_keys=True))
    print(f"Wrote {output_file}")


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Produce contract parity evidence")
    parser.add_argument(
        "--evidence-dir", type=Path, required=True, help="Directory to write evidence artifacts"
    )
    parser.add_argument(
        "--ts-results", type=Path, help="Optional TypeScript test results file to include"
    )
    args = parser.parse_args()

    args.evidence_dir.mkdir(parents=True, exist_ok=True)

    try:
        generate_parity_matrix(args.evidence_dir)
        run_python_fixtures(args.evidence_dir, args.ts_results)
        print("RESULT: PASS")
        return 0
    except Exception as e:
        import traceback

        traceback.print_exc()
        print(f"RESULT: FAIL ({e})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
