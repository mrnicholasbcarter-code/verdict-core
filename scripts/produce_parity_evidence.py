#!/usr/bin/env python3
"""Produce contract parity evidence for G4.2 and G4.3.

G4.2: contract_parity_matrix.md — field-by-field comparison of Python vs TypeScript.
      Exit 1 with RESULT: FAIL if any row is not OK.

G4.3: parity_fixture_results.json — shared fixtures run through BOTH Python and TS.
      Exit 1 with RESULT: FAIL if any fixture result mismatches or TS is not run.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import MISSING, fields
from pathlib import Path
from typing import Any

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from verdict.contracts import (
    AvailabilitySnapshot,
    ContractValidationError,
    ExecutionEnvelope,
    RoutingDecisionContract,
    RuntimeCandidate,
    TaskSpec,
    contract_from_dict,
)

CONTRACTS = [
    ("TaskSpec", TaskSpec),
    ("RoutingDecision", RoutingDecisionContract),
    ("AvailabilitySnapshot", AvailabilitySnapshot),
    ("RuntimeCandidate", RuntimeCandidate),
    ("ExecutionEnvelope", ExecutionEnvelope),
]

# Map contract name -> TS contractSchemas key
TS_CONTRACT_NAMES = {
    "TaskSpec": "TaskSpec",
    "RoutingDecision": "RoutingDecision",
    "AvailabilitySnapshot": "AvailabilitySnapshot",
    "RuntimeCandidate": "RuntimeCandidate",
    "ExecutionEnvelope": "ExecutionEnvelope",
}

# Map parity fixture file prefix -> Python contract name
FIXTURE_CONTRACT_MAP = {
    "routing_decision": "RoutingDecisionContract",
    "envelope": "ExecutionEnvelope",
}

# Expected accept/reject for each fixture based on filename
FIXTURE_EXPECT = {
    "routing_decision_valid.json": "accept",
    "routing_decision_minimal.json": "accept",
    "routing_decision_defaults.json": "accept",
    "routing_decision_unknown_field.json": "reject",
    "envelope_explicit.json": "accept",
}


def extract_python_fields(cls: type) -> dict[str, dict[str, Any]]:
    """Extract field names and input-required status from a Python dataclass."""
    result: dict[str, dict[str, Any]] = {}
    for f in fields(cls):
        # A field is input-required if it has no default value AND no default_factory
        required = f.default is MISSING and f.default_factory is MISSING  # type: ignore[misc]
        result[f.name] = {"required": required}
    return result


def get_ts_fields(contracts_dist: Path) -> dict[str, dict[str, dict[str, Any]]]:
    """Run node against the built contracts package and extract field schemas.

    Uses contractSchemas[name]._def.shape() to get the real Zod field defs.
    A field is input-optional if its top-level typeName is ZodDefault or ZodOptional.
    """
    node_script = f"""
const {{ contractSchemas }} = require('{contracts_dist}/index.js');
const NAMES = {json.dumps(list(TS_CONTRACT_NAMES.values()))};
const result = {{}};
for (const cname of NAMES) {{
  const schema = contractSchemas[cname];
  if (!schema || !schema._def || !schema._def.shape) continue;
  const shape = schema._def.shape();
  const schemaFields = {{}};
  for (const [fname, fschema] of Object.entries(shape)) {{
    const def = fschema._def;
    // ZodDefault and ZodOptional both mean the field may be omitted on input
    const inputOptional = def.typeName === 'ZodDefault' || def.typeName === 'ZodOptional';
    schemaFields[fname] = {{ required: !inputOptional }};
  }}
  result[cname] = schemaFields;
}}
console.log(JSON.stringify(result));
"""
    tmp = Path("/tmp/_ts_field_inspect.js")
    tmp.write_text(node_script)
    r = subprocess.run(["node", str(tmp)], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"node field inspection failed: {r.stderr[:500]}")
    return json.loads(r.stdout)  # type: ignore[no-any-return]


def run_fixture_through_ts(contracts_dist: Path, fixture_path: Path, ts_name: str) -> str:
    """Run a single fixture JSON through the TS parseContract() and return 'accept' or 'reject'."""
    node_script = f"""
const {{ parseContract }} = require('{contracts_dist}/index.js');
const data = JSON.parse(require('fs').readFileSync('{fixture_path}', 'utf8'));
try {{
  parseContract('{ts_name}', data);
  console.log('accept');
}} catch (e) {{
  console.log('reject:' + e.message.slice(0, 120));
}}
"""
    tmp = Path("/tmp/_ts_fixture_run.js")
    tmp.write_text(node_script)
    r = subprocess.run(["node", str(tmp)], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return f"error:{r.stderr[:120]}"
    return r.stdout.strip()


def run_fixture_through_python(fixture_path: Path, py_contract: str) -> str:
    """Run a fixture through the Python contract_from_dict and return 'accept' or 'reject:...'."""
    data = json.loads(fixture_path.read_text())
    try:
        contract_from_dict(py_contract, data)
        return "accept"
    except ContractValidationError as e:
        return f"reject:{str(e)[:120]}"
    except Exception as e:
        return f"error:{str(e)[:120]}"


def generate_parity_matrix(evidence_dir: Path, contracts_dist: Path) -> list[str]:
    """Generate contract_parity_matrix.md. Returns list of mismatch descriptions."""
    ts_schemas = get_ts_fields(contracts_dist)

    lines = [
        "# Contract Parity Matrix",
        "",
        "Python vs TypeScript contract field comparison for G4.2.",
        "",
        "Generated from:",
        "- Python: `verdict/contracts.py` (dataclass fields)",
        "- TypeScript: `contracts/dist/index.js` (contractSchemas Zod shapes, built from `contracts/src/index.ts`)",
        "",
    ]

    mismatches: list[str] = []

    for name, py_cls in CONTRACTS:
        lines.append(f"## {name}")
        lines.append("")
        py_schema = extract_python_fields(py_cls)
        ts_schema = ts_schemas.get(TS_CONTRACT_NAMES[name], {})

        all_fields = sorted(set(py_schema.keys()) | set(ts_schema.keys()))

        lines.append("| Field | Python req | TypeScript req | Status |")
        lines.append("|-------|-----------|----------------|--------|")

        for fname in all_fields:
            py_info = py_schema.get(fname)
            ts_info = ts_schema.get(fname)

            py_req = ("yes" if py_info["required"] else "no") if py_info else "MISSING"
            ts_req = ("yes" if ts_info["required"] else "no") if ts_info else "MISSING"

            if py_info is None:
                status = "TS-only"
                mismatches.append(f"{name}.{fname}: TS-only")
            elif ts_info is None:
                status = "PY-only"
                mismatches.append(f"{name}.{fname}: PY-only")
            elif py_info["required"] != ts_info["required"]:
                status = "MISMATCH"
                mismatches.append(
                    f"{name}.{fname}: py.required={py_info['required']} ts.required={ts_info['required']}"
                )
            else:
                status = "OK"

            lines.append(f"| `{fname}` | {py_req} | {ts_req} | {status} |")

        lines.append("")

    output_file = evidence_dir / "contract_parity_matrix.md"
    output_file.write_text("\n".join(lines))
    print(f"Wrote {output_file}")
    return mismatches


def run_fixture_contract_name(fixture_fname: str) -> tuple[str, str] | None:
    """Return (py_contract, ts_contract) for a parity fixture file, or None to skip."""
    for prefix, py_name in FIXTURE_CONTRACT_MAP.items():
        if fixture_fname.startswith(prefix):
            ts_name = {
                "RoutingDecisionContract": "RoutingDecision",
                "ExecutionEnvelope": "ExecutionEnvelope",
            }[py_name]
            return py_name, ts_name
    return None


def generate_fixture_results(evidence_dir: Path, contracts_dist: Path) -> list[str]:
    """Run shared parity fixtures through Python and TS. Returns mismatch descriptions."""
    fixture_dir = Path("test_fixtures/parity")
    fixtures = sorted(fixture_dir.glob("*.json"))

    results = []
    mismatches: list[str] = []

    for fixture_path in fixtures:
        fname = fixture_path.name
        contract_pair = run_fixture_contract_name(fname)
        if contract_pair is None:
            print(f"  SKIP {fname}: no known contract mapping")
            continue

        py_contract, ts_contract = contract_pair
        expected = FIXTURE_EXPECT.get(fname)

        py_result = run_fixture_through_python(fixture_path, py_contract)
        ts_result = run_fixture_through_ts(contracts_dist, fixture_path, ts_contract)

        py_verdict = "accept" if py_result == "accept" else "reject"
        ts_verdict = "accept" if ts_result.startswith("accept") else "reject"

        match = py_verdict == ts_verdict
        expected_ok = (expected is None) or (py_verdict == expected)

        status = "OK" if match and expected_ok else "MISMATCH"
        if status == "MISMATCH":
            mismatches.append(f"{fname}: py={py_verdict} ts={ts_verdict} expected={expected}")

        results.append(
            {
                "fixture": fname,
                "contract_py": py_contract,
                "contract_ts": ts_contract,
                "py_result": py_result,
                "ts_result": ts_result,
                "py_verdict": py_verdict,
                "ts_verdict": ts_verdict,
                "expected": expected,
                "status": status,
            }
        )
        print(f"  {status} {fname}: py={py_verdict} ts={ts_verdict}")

    output_file = evidence_dir / "parity_fixture_results.json"
    output_file.write_text(json.dumps(results, indent=2, sort_keys=True))
    print(f"Wrote {output_file}")
    return mismatches


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Produce contract parity evidence")
    parser.add_argument(
        "--evidence-dir", type=Path, required=True, help="Directory to write evidence artifacts"
    )
    args = parser.parse_args()

    args.evidence_dir.mkdir(parents=True, exist_ok=True)

    contracts_dist = Path("contracts/dist").resolve()
    if not contracts_dist.exists():
        print(
            "RESULT: FAIL (contracts/dist not found; run: cd contracts && npm ci && npm run build)",
            file=sys.stderr,
        )
        return 1

    try:
        matrix_mismatches = generate_parity_matrix(args.evidence_dir, contracts_dist)
        fixture_mismatches = generate_fixture_results(args.evidence_dir, contracts_dist)

        all_mismatches = matrix_mismatches + fixture_mismatches

        if all_mismatches:
            print("", file=sys.stderr)
            print("Parity mismatches found:", file=sys.stderr)
            for m in all_mismatches:
                print(f"  {m}", file=sys.stderr)
            print("RESULT: FAIL")
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
