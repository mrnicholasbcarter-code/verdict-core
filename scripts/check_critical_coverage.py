#!/usr/bin/env python3
"""
Check critical module branch coverage floors.

Reads coverage.xml and .coverage-critical.toml and fails if any critical module
is below its floor threshold. This prevents regression in safety-critical modules.

Usage:
    pytest --cov=verdict --cov-branch --cov-report=xml
    scripts/check_critical_coverage.py

Exit codes:
    0: All modules meet or exceed their floors
    1: One or more modules below floor
    2: Missing inputs or parse error
"""
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def parse_toml_config(path: Path) -> dict[str, int]:
    """Parse .coverage-critical.toml and return {module: floor} dict."""
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
            elif line.startswith('branch_coverage_floor = ') and current_module:
                floor = int(line.split('=')[1].strip())
                floors[current_module] = floor
                current_module = None
    
    return floors


def parse_coverage_xml(path: Path) -> dict[str, float]:
    """Parse coverage.xml and return {module: branch_pct} dict."""
    tree = ET.parse(path)
    root = tree.getroot()
    
    coverage = {}
    
    for package in root.findall('.//package'):
        package_name = package.get('name')
        for class_elem in package.findall('.//class'):
            filename = class_elem.get('filename')
            
            # Build full path
            if package_name == '.':
                full_path = f'verdict/{filename}'
            else:
                full_path = f'verdict/{package_name}/{filename}'
            
            # Get branch coverage
            lines = class_elem.find('lines')
            if lines is not None:
                branch_hits = 0
                branch_total = 0
                for line in lines.findall('line'):
                    if line.get('branch') == 'true':
                        conditions = line.get('condition-coverage', '')
                        if conditions:
                            import re
                            match = re.search(r'(\d+)/(\d+)', conditions)
                            if match:
                                branch_hits += int(match.group(1))
                                branch_total += int(match.group(2))
                
                if full_path not in coverage:
                    coverage[full_path] = {'hits': 0, 'total': 0}
                
                coverage[full_path]['hits'] += branch_hits
                coverage[full_path]['total'] += branch_total
    
    # Calculate percentages
    result = {}
    for path, data in coverage.items():
        if data['total'] > 0:
            result[path] = (data['hits'] / data['total']) * 100
        else:
            result[path] = 100.0
    
    return result


def main() -> int:
    """Check critical module coverage floors."""
    repo_root = Path(__file__).parent.parent
    config_path = repo_root / '.coverage-critical.toml'
    coverage_path = repo_root / 'coverage.xml'
    
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
        return 2
    
    failures = []
    
    for module, floor in sorted(floors.items()):
        measured = coverage.get(module, 0.0)
        status = "✓" if measured >= floor else "✗"
        
        print(f"{status} {module:60s} {measured:6.2f}% (floor: {floor:3d}%)")
        
        if measured < floor:
            failures.append((module, measured, floor))
    
    if failures:
        print(f"\n{len(failures)} module(s) below floor:")
        for module, measured, floor in failures:
            shortfall = floor - measured
            print(f"  {module}: {measured:.2f}% < {floor}% (shortfall: {shortfall:.2f}%)")
        return 1
    
    print(f"\nAll {len(floors)} critical modules meet or exceed their coverage floors.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
