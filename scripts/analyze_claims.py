#!/usr/bin/env python3
"""
Analyze CLAIMS.md to check which 'shipped' claims have test evidence.
"""

from pathlib import Path

CLAIMS_PATH = Path(__file__).parent.parent / "docs" / "CLAIMS.md"
TESTS_DIR = Path(__file__).parent.parent / "tests"


def parse_claims():
    """Parse CLAIMS.md and return list of (id, label, evidence_paths)"""
    content = CLAIMS_PATH.read_text()
    lines = content.split("\n")
    claims = []
    in_table = False
    for line in lines:
        if line.startswith("| C-") and "|" in line:
            in_table = True
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 6:
                claim_id = parts[1].strip()
                label = parts[4].strip()
                evidence_col = parts[5].strip()
                evidence_paths = [ep.strip() for ep in evidence_col.split(",") if ep.strip()]
                claims.append((claim_id, label, evidence_paths))
        elif in_table and not line.startswith("|"):
            break
    return claims


def has_test_evidence(evidence_paths):
    """Check if any evidence path is under tests/"""
    for ep in evidence_paths:
        if ep.startswith("tests/"):
            return True
        # Also check if it's a test file even without tests/ prefix
        if ep.startswith("test_") and ep.endswith(".py"):
            return True
    return False


def find_test_for_module(module_path):
    """Try to find a test file that exercises the given module"""
    # Convert module path to potential test file
    if module_path.startswith("verdict/"):
        # e.g., verdict/eligibility.py -> test_eligibility.py or test_*eligibility*.py
        module_name = module_path.replace("verdict/", "").replace("/", "_").replace(".py", "")
        test_patterns = [f"test_{module_name}.py", f"test_*{module_name}*.py"]
        for pattern in test_patterns:
            matches = list(TESTS_DIR.glob(pattern))
            if matches:
                return str(matches[0].relative_to(TESTS_DIR.parent))
    return None


def main():
    claims = parse_claims()
    print(f"Total claims: {len(claims)}")

    shipped_without_tests = []
    shipped_with_tests = []
    proposed = []
    observed = []
    verified = []

    for claim_id, label, evidence_paths in claims:
        if label == "shipped":
            has_tests = has_test_evidence(evidence_paths)
            if has_tests:
                shipped_with_tests.append((claim_id, evidence_paths))
            else:
                # Try to find a test
                test_found = None
                for ep in evidence_paths:
                    if ep.startswith("verdict/"):
                        test_found = find_test_for_module(ep)
                        if test_found:
                            break
                if test_found:
                    shipped_with_tests.append((claim_id, [*evidence_paths, test_found]))
                else:
                    shipped_without_tests.append((claim_id, evidence_paths))
        elif label == "proposed":
            proposed.append(claim_id)
        elif label == "observed":
            observed.append(claim_id)
        elif label == "verified":
            verified.append(claim_id)

    print(f"\n=== Shipped with test evidence: {len(shipped_with_tests)} ===")
    for cid, eps in shipped_with_tests:
        test_eps = [ep for ep in eps if ep.startswith("tests/") or ep.startswith("test_")]
        print(f"  {cid}: {test_eps}")

    print(f"\n=== Shipped WITHOUT test evidence: {len(shipped_without_tests)} ===")
    for cid, eps in shipped_without_tests:
        print(f"  {cid}: {eps}")

    print(f"\n=== Proposed: {len(proposed)} ===")
    for cid in proposed:
        print(f"  {cid}")

    print(f"\n=== Observed: {len(observed)} ===")
    for cid in observed:
        print(f"  {cid}")

    print(f"\n=== Verified: {len(verified)} ===")
    for cid in verified:
        print(f"  {cid}")

    return shipped_without_tests


if __name__ == "__main__":
    main()
