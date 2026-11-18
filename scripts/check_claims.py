#!/usr/bin/env python3
"""
Check that every public claim in README.md has an inventory entry in docs/CLAIMS.md
and that every inventory entry's evidence path exists.
"""

import re
import sys
from pathlib import Path


def extract_readme_claims(readme_path: Path) -> set[str]:
    """Extract all claim IDs from README.md markers like <!-- claim:C-001 -->"""
    content = readme_path.read_text()
    # Match <!-- claim:C-XXX --> pattern (can have multiple per line)
    pattern = r"<!--\s*claim:(C-\d+)\s*-->"
    matches = re.findall(pattern, content)
    return set(matches)


def extract_claims_md_claims(claims_path: Path) -> dict[str, dict]:
    """Parse docs/CLAIMS.md table and return dict of claim_id -> {evidence_paths, label}"""
    content = claims_path.read_text()
    claims = {}

    # Parse the markdown table rows
    # Pattern: | C-XXX | ... | ... | ... | evidence paths ... |
    lines = content.split("\n")
    in_table = False
    for line in lines:
        if line.startswith("| C-") and "|" in line:
            in_table = True
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 6:
                claim_id = parts[1].strip()
                label = parts[4].strip()
                evidence_col = parts[5].strip()

                # Extract evidence paths from the evidence column
                # Evidence column contains comma-separated paths like:
                # "verdict/eligibility.py, tests/test_eligibility_gate.py"
                evidence_paths = []
                for ep in evidence_col.split(","):
                    ep = ep.strip()
                    if (
                        ep
                        and not ep.startswith("docs/")
                        and not ep.startswith("verdict/")
                        and not ep.startswith("tests/")
                        and not ep.startswith("schemas/")
                        and not ep.startswith("diagrams/")
                        and not ep.startswith("contracts/")
                        and not ep.startswith("benchmarks/")
                        and not ep.startswith(".github/")
                        and not ep.startswith("pyproject.toml")
                    ):
                        # Skip non-path entries
                        continue
                    if ep:
                        evidence_paths.append(ep)

                # Also check for docs/proof/ paths
                for ep in evidence_col.split(","):
                    ep = ep.strip()
                    if ep.startswith("docs/proof/"):
                        evidence_paths.append(ep)

                claims[claim_id] = {"label": label, "evidence_paths": evidence_paths}
        elif in_table and not line.startswith("|"):
            break

    return claims


def check_evidence_exists(evidence_paths: list[str], repo_root: Path) -> list[str]:
    """Check if evidence paths exist. Return list of missing paths."""
    missing = []
    for ep in evidence_paths:
        # Handle multiple paths in one entry (comma-separated already split)
        path = repo_root / ep
        if not path.exists():
            missing.append(ep)
    return missing


def main():
    repo_root = Path(__file__).parent.parent
    readme_path = repo_root / "README.md"
    claims_path = repo_root / "docs" / "CLAIMS.md"

    if not readme_path.exists():
        print(f"ERROR: {readme_path} not found")
        return 1

    if not claims_path.exists():
        print(f"ERROR: {claims_path} not found")
        return 1

    # Extract claims from both sources
    readme_claims = extract_readme_claims(readme_path)
    claims_md_claims = extract_claims_md_claims(claims_path)

    print(f"Found {len(readme_claims)} claim markers in README.md")
    print(f"Found {len(claims_md_claims)} claim entries in docs/CLAIMS.md")

    # Check 1: Every README claim marker has a CLAIMS.md entry
    missing_in_claims = readme_claims - set(claims_md_claims.keys())
    if missing_in_claims:
        print(
            f"\nERROR: {len(missing_in_claims)} claim(s) in README.md have no entry in docs/CLAIMS.md:"
        )
        for cid in sorted(missing_in_claims):
            print(f"  - {cid}")
        return 1
    else:
        print("\n✓ All README claim markers have entries in docs/CLAIMS.md")

    # Check 2: Every CLAIMS.md entry has a README marker (optional but good practice)
    extra_in_claims = set(claims_md_claims.keys()) - readme_claims
    if extra_in_claims:
        print(
            f"\nWARNING: {len(extra_in_claims)} claim(s) in docs/CLAIMS.md have no marker in README.md:"
        )
        for cid in sorted(extra_in_claims):
            print(f"  - {cid}")

    # Check 3: Every evidence path exists
    print("\nChecking evidence paths...")
    all_missing = []
    for claim_id, data in claims_md_claims.items():
        missing = check_evidence_exists(data["evidence_paths"], repo_root)
        if missing:
            all_missing.extend([(claim_id, m) for m in missing])

    if all_missing:
        print(f"\nERROR: {len(all_missing)} evidence path(s) do not exist:")
        for claim_id, path in all_missing:
            print(f"  - {claim_id}: {path}")
        return 1
    else:
        print("✓ All evidence paths exist")

    # Check 4: No README claim is unlabeled (every claim in CLAIMS.md has a label)
    unlabeled = [
        cid for cid, data in claims_md_claims.items() if not data["label"] or data["label"] == ""
    ]
    if unlabeled:
        print(f"\nERROR: {len(unlabeled)} claim(s) have no label:")
        for cid in unlabeled:
            print(f"  - {cid}")
        return 1
    else:
        print("✓ All claims have labels")

    # Check 5: Proposed/disputed claims are not presented as shipped in README
    # This is a manual check - we verify the label in CLAIMS.md
    proposed_or_disputed = [
        cid
        for cid, data in claims_md_claims.items()
        if data["label"] in ("proposed", "disputed", "superseded")
    ]
    if proposed_or_disputed:
        print(
            f"\nINFO: {len(proposed_or_disputed)} claim(s) are labeled proposed/disputed/superseded:"
        )
        for cid in proposed_or_disputed:
            data = claims_md_claims[cid]
            print(f"  - {cid}: {data['label']}")

    # Summary by label
    label_counts = {}
    for data in claims_md_claims.values():
        label = data["label"]
        label_counts[label] = label_counts.get(label, 0) + 1

    print("\n=== Claim Count by Label ===")
    for label in sorted(label_counts.keys()):
        print(f"  {label}: {label_counts[label]}")

    print(f"\nTotal claims: {len(claims_md_claims)}")
    print("All checks passed!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
