#!/usr/bin/env python3
"""
Check critical module branch coverage floors.

Reads coverage.xml and .coverage-critical.toml and fails if any critical module
is below its floor threshold. This prevents regression in safety-critical modules.

Usage:
    pytest --cov=verdict --cov-branch --cov-report=xml
    scripts/check_critical_coverage.py

    # With custom paths:
    scripts/check_critical_coverage.py --coverage-xml path/to/coverage.xml --config path/to/config.toml

Exit codes:
    0: All modules meet or exceed their floors
    1: One or more modules below floor, or configured module not measured
    2: Missing inputs, parse error, or usage error
"""

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def parse_toml_config(path: Path) -> dict[str, int]:
    """Parse .coverage-critical.toml and return {module: floor} dict."""
    try:
        import tomllib  # Python 3.11+
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[import-not-found,no-redef]
        except ImportError:
            # Fall back to manual parser for Python 3.10 without tomli
            return _parse_toml_manual(path)

    with open(path, "rb") as f:
        config = tomllib.load(f)

    floors = {}
    for module, settings in config.get("critical_modules", {}).items():
        floors[module] = settings["branch_coverage_floor"]

    return floors


def _parse_toml_manual(path: Path) -> dict[str, int]:
    """Manual TOML parser for Python 3.10 without tomli."""
    floors = {}
    current_module = None

    with open(path) as f:
        for line in f:
            line = line.strip()
            if line.startswith('[critical_modules."'):
                # Extract module name
                start = line.index('"') + 1
                end = line.rindex('"')
                current_module = line[start:end]
            elif line.startswith("branch_coverage_floor = ") and current_module:
                floor = int(line.split("=")[1].strip())
                floors[current_module] = floor
                current_module = None

    return floors


def parse_coverage_xml(path: Path) -> dict[str, float]:
    """Parse coverage.xml and return {module: branch_pct} dict."""
    tree = ET.parse(path)
    root = tree.getroot()

    coverage = {}

    for package in root.findall(".//package"):
        for class_elem in package.findall(".//class"):
            filename = class_elem.get("filename")
            # Correct: verdict/ + filename (filename already includes package path)
            full_path = f"verdict/{filename}"

            # Get branch coverage
            lines = class_elem.find("lines")
            if lines is not None:
                branch_hits = 0
                branch_total = 0
                for line in lines.findall("line"):
                    if line.get("branch") == "true":
                        conditions = line.get("condition-coverage", "")
                        if conditions:
                            import re

                            match = re.search(r"(\d+)/(\d+)", conditions)
                            if match:
                                branch_hits += int(match.group(1))
                                branch_total += int(match.group(2))

                if full_path not in coverage:
                    coverage[full_path] = {"hits": 0, "total": 0}

                coverage[full_path]["hits"] += branch_hits
                coverage[full_path]["total"] += branch_total

    # Calculate percentages
    result = {}
    for path, data in coverage.items():
        if data["total"] > 0:
            result[path] = (data["hits"] / data["total"]) * 100
        else:
            # Module with no branches: 100% by definition
            result[path] = 100.0

    return result


def main() -> int:
    """Check critical module coverage floors."""
    parser = argparse.ArgumentParser(description="Check critical module branch coverage floors")
    parser.add_argument(
        "--coverage-xml",
        type=Path,
        help="Path to coverage.xml (default: coverage.xml in repo root)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="Path to config file (default: .coverage-critical.toml in repo root)",
    )
    args = parser.parse_args()

    # Determine paths
    if args.coverage_xml:
        coverage_path = args.coverage_xml
        repo_root = Path.cwd()
    else:
        repo_root = Path(__file__).parent.parent
        coverage_path = repo_root / "coverage.xml"

    if args.config:
        config_path = args.config
    else:
        repo_root = Path(__file__).parent.parent
        config_path = repo_root / ".coverage-critical.toml"

    # Validate inputs exist
    if not config_path.exists():
        print(f"ERROR: {config_path} not found", file=sys.stderr)
        return 2

    if not coverage_path.exists():
        print(f"ERROR: {coverage_path} not found", file=sys.stderr)
        print("Run: pytest --cov=verdict --cov-branch --cov-report=xml", file=sys.stderr)
        return 2

    try:
        floors = parse_toml_config(config_path)
        coverage = parse_coverage_xml(coverage_path)
    except Exception as e:
        print(f"ERROR parsing inputs: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 2

    # Check that all configured modules exist as files (only if using default config)
    if not args.config:
        missing_files = []
        for module in floors:
            module_path = repo_root / module
            if not module_path.exists():
                missing_files.append(module)

        if missing_files:
            print("ERROR: Configured modules do not exist as files:", file=sys.stderr)
            for module in missing_files:
                print(f"  {module}", file=sys.stderr)
            return 2

    failures = []
    missing_coverage = []

    for module, floor in sorted(floors.items()):
        if module not in coverage:
            # Module configured but not measured in coverage.xml
            missing_coverage.append(module)
            print(f"✗ {module:60s} NOT MEASURED (floor: {floor:3d}%)")
        else:
            measured = coverage[module]
            status = "✓" if measured >= floor else "✗"

            print(f"{status} {module:60s} {measured:6.2f}% (floor: {floor:3d}%)")

            if measured < floor:
                failures.append((module, measured, floor))

    if missing_coverage:
        print(
            f"\nERROR: {len(missing_coverage)} configured module(s) not measured in coverage.xml:",
            file=sys.stderr,
        )
        for module in missing_coverage:
            print(f"  {module}", file=sys.stderr)
        return 1

    if failures:
        print(f"\nERROR: {len(failures)} module(s) below floor:", file=sys.stderr)
        for module, measured, floor in failures:
            shortfall = floor - measured
            print(
                f"  {module}: {measured:.2f}% < {floor}% (shortfall: {shortfall:.2f}%)",
                file=sys.stderr,
            )
        return 1

    print(f"\nAll {len(floors)} critical modules meet or exceed their coverage floors.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
