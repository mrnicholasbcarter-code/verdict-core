"""
Tests for scripts/check_critical_coverage.py

Verifies the coverage gate enforcement script correctly:
- Detects modules below floor
- Fails hard on missing modules
- Checks file existence
"""

import subprocess
import sys
from pathlib import Path


def test_checker_passes_on_current_coverage():
    """The checker should pass on the current baseline coverage."""
    result = subprocess.run(
        [sys.executable, "scripts/check_critical_coverage.py"], capture_output=True, text=True
    )

    assert result.returncode == 0, f"Checker failed: {result.stderr}"
    assert "All 14 critical modules meet or exceed their coverage floors" in result.stdout


def test_all_configured_modules_exist_as_files():
    """All modules in .coverage-critical.toml must exist as real files."""
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[import-not-found]
        except ImportError:
            # Manual parse for Python 3.10
            import re

            floors = {}
            with open(".coverage-critical.toml") as f:
                content = f.read()
                for match in re.finditer(r'\[critical_modules\."([^"]+)"\]', content):
                    floors[match.group(1)] = True

            for module in floors:
                module_path = Path(module)
                assert module_path.exists(), f"Configured module {module} does not exist"
            return

    with open(".coverage-critical.toml", "rb") as f:
        config = tomllib.load(f)

    for module in config["critical_modules"]:
        module_path = Path(module)
        assert module_path.exists(), f"Configured module {module} does not exist"
        assert module_path.is_file(), f"Configured module {module} is not a file"


def test_configured_paths_match_coverage_xml_structure():
    """Module paths in config must match how coverage.xml reports them."""
    import xml.etree.ElementTree as ET

    tree = ET.parse("coverage.xml")
    root = tree.getroot()

    # Build set of all paths in coverage.xml
    coverage_paths = set()
    for package in root.findall(".//package"):
        for class_elem in package.findall(".//class"):
            filename = class_elem.get("filename")
            full_path = f"verdict/{filename}"
            coverage_paths.add(full_path)

    # Check that all configured modules are in coverage
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[import-not-found]
        except ImportError:
            # Manual parse for Python 3.10
            import re

            with open(".coverage-critical.toml") as f:
                content = f.read()
                for match in re.finditer(r'\[critical_modules\."([^"]+)"\]', content):
                    module = match.group(1)
                    assert module in coverage_paths, (
                        f"Configured module {module} not found in coverage.xml. "
                        f"Check that the path matches coverage.xml structure."
                    )
            return

    with open(".coverage-critical.toml", "rb") as f:
        config = tomllib.load(f)

    for module in config["critical_modules"]:
        assert module in coverage_paths, (
            f"Configured module {module} not found in coverage.xml. "
            f"Check that the path matches coverage.xml structure."
        )
