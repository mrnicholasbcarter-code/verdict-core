"""Validate the checked-in v1 schema and canonical contract fixture."""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).parents[1]
SCHEMA_PATH = ROOT / "schemas" / "contracts.v1.json"
PACKAGED_SCHEMA_PATH = ROOT / "verdict" / "schemas" / "contracts.v1.json"
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "contract-v1.json"
CONTEXT_SCHEMA_PATH = ROOT / "schemas" / "context-pack.v1.json"
PACKAGED_CONTEXT_SCHEMA_PATH = ROOT / "verdict" / "schemas" / "context-pack.v1.json"


def main() -> int:
    schema = json.loads(SCHEMA_PATH.read_text())
    packaged_schema = json.loads(PACKAGED_SCHEMA_PATH.read_text())
    fixture = json.loads(FIXTURE_PATH.read_text())
    context_schema = json.loads(CONTEXT_SCHEMA_PATH.read_text())
    packaged_context_schema = json.loads(PACKAGED_CONTEXT_SCHEMA_PATH.read_text())
    if schema != packaged_schema:
        print(
            f"{PACKAGED_SCHEMA_PATH.relative_to(ROOT)} does not match {SCHEMA_PATH.relative_to(ROOT)}"
        )
        return 1
    if context_schema != packaged_context_schema:
        print(
            f"{PACKAGED_CONTEXT_SCHEMA_PATH.relative_to(ROOT)} does not match {CONTEXT_SCHEMA_PATH.relative_to(ROOT)}"
        )
        return 1
    Draft202012Validator.check_schema(schema)
    Draft202012Validator.check_schema(context_schema)
    errors = sorted(
        Draft202012Validator(schema).iter_errors(fixture),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        for error in errors:
            path = ".".join(str(part) for part in error.absolute_path) or "$"
            print(f"{path}: {error.message}")
        return 1
    print(f"Validated {FIXTURE_PATH.relative_to(ROOT)} against {SCHEMA_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
