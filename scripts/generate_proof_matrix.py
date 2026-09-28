#!/usr/bin/env python3
"""Generate docs/PROOF_MATRIX.md from docs/proof/proof_matrix.v1.json (BOD-203).

The generated markdown is deterministic: re-running this script on an unchanged
JSON source produces byte-identical output. CI enforces freshness by comparing
the committed doc against a regenerated copy.
"""

from __future__ import annotations

import difflib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MATRIX_JSON = ROOT / "docs" / "proof" / "proof_matrix.v1.json"
OUTPUT_MD = ROOT / "docs" / "PROOF_MATRIX.md"

HEADER = """<!-- generated — do not edit by hand; run: python scripts/generate_proof_matrix.py -->
# Proof Matrix

> Auto-generated from [`docs/proof/proof_matrix.v1.json`](proof/proof_matrix.v1.json).
> Regenerate with `python scripts/generate_proof_matrix.py`.

| ID | Area | Claim | Source | Test / Locator | Evidence Artifact | Verify Command | Status |
|----|------|-------|--------|----------------|-------------------|----------------|--------|
"""


def _escape(text: str) -> str:
    """Escape pipe characters for markdown table cells."""
    return text.replace("|", "\\|").replace("\n", " ")


def render(matrix_path: Path = MATRIX_JSON) -> str:
    """Render the markdown table and return the full document text (no I/O)."""
    data = json.loads(matrix_path.read_text(encoding="utf-8"))
    lines = [HEADER]
    for row in data["rows"]:
        row_id = row["id"]
        area = _escape(row["area"])
        claim = _escape(row["public_wording"])
        status = row["status"]
        verification = _escape(row["verification"])

        for i, ev in enumerate(row["evidence"]):
            path = ev["path"]
            locator = _escape(ev["locator"])
            kind = ev["kind"]

            if i == 0:
                lines.append(
                    f"| {row_id} | {area} | {claim} "
                    f"| `{path}` | {locator} | {kind} "
                    f"| `{verification}` | {status} |\n"
                )
            else:
                lines.append(f"| | | | `{path}` | {locator} | {kind} | | |\n")

    return "".join(lines)


def generate(matrix_path: Path = MATRIX_JSON, output_path: Path = OUTPUT_MD) -> str:
    """Render the markdown and write it to *output_path*."""
    doc = render(matrix_path)
    output_path.write_text(doc, encoding="utf-8")
    return doc


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 if the generated doc differs from the committed file.",
    )
    parser.add_argument("--matrix", type=Path, default=MATRIX_JSON)
    parser.add_argument("--output", type=Path, default=OUTPUT_MD)
    args = parser.parse_args(argv)

    if args.check:
        if not args.output.exists():
            print(f"FAIL: {args.output} does not exist", file=sys.stderr)
            return 1
        committed = args.output.read_text(encoding="utf-8")
        regenerated = render(args.matrix)
        if committed != regenerated:
            diff = difflib.unified_diff(
                committed.splitlines(keepends=True),
                regenerated.splitlines(keepends=True),
                fromfile="committed",
                tofile="regenerated",
                n=2,
            )
            sys.stderr.writelines(diff)
            print(
                f"FAIL: {args.output} is stale; "
                f"regenerate with: python scripts/generate_proof_matrix.py",
                file=sys.stderr,
            )
            return 1
        print(f"OK: {args.output} is up to date")
    else:
        doc = generate(args.matrix, args.output)
        print(f"wrote {args.output} ({len(doc)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
