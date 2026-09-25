"""
Tests for scripts/check_critical_coverage.py

These tests are HERMETIC: they build synthetic coverage.xml files and do not
depend on a repo-wide coverage.xml being present.
"""

import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def build_coverage_xml(modules: dict[str, tuple[int, int]], tmp_path: Path) -> Path:
    """
    Build a synthetic coverage.xml file.

    Args:
        modules: {filename: (branch_hits, branch_total)} mapping
                 e.g. {"eligibility.py": (30, 34), "orchestration/planner.py": (63, 70)}
        tmp_path: Temporary directory

    Returns:
        Path to the generated coverage.xml
    """
    root = ET.Element("coverage")
    root.set("version", "7.15.2")
    root.set("timestamp", "1234567890")
    root.set("lines-valid", "1000")
    root.set("lines-covered", "900")
    root.set("line-rate", "0.9")
    root.set("branches-valid", "500")
    root.set("branches-covered", "400")
    root.set("branch-rate", "0.8")

    sources = ET.SubElement(root, "sources")
    source = ET.SubElement(sources, "source")
    source.text = "/fake/verdict"

    packages_elem = ET.SubElement(root, "packages")

    # Group by package
    by_package = {}
    for filename, (hits, total) in modules.items():
        package_name = filename.rsplit("/", 1)[0] if "/" in filename else "."

        if package_name not in by_package:
            by_package[package_name] = []
        by_package[package_name].append((filename, hits, total))

    for package_name, files in by_package.items():
        package = ET.SubElement(packages_elem, "package")
        package.set("name", package_name)
        package.set("line-rate", "0.9")
        package.set("branch-rate", "0.8")

        classes = ET.SubElement(package, "classes")

        for filename, hits, total in files:
            class_elem = ET.SubElement(classes, "class")
            class_elem.set("filename", filename)
            class_elem.set("line-rate", "0.9")
            class_elem.set("branch-rate", f"{hits / total if total > 0 else 1.0:.4f}")

            ET.SubElement(class_elem, "methods")
            lines = ET.SubElement(class_elem, "lines")

            # Add some branch lines
            for i in range(total):
                line = ET.SubElement(lines, "line")
                line.set("number", str(i + 1))
                line.set("hits", "1" if i < hits else "0")
                line.set("branch", "true")
                if i < hits:
                    line.set("condition-coverage", "100% (2/2)")
                else:
                    line.set("condition-coverage", "0% (0/2)")

    tree = ET.ElementTree(root)
    coverage_xml = tmp_path / "coverage.xml"
    tree.write(coverage_xml, encoding="utf-8", xml_declaration=True)

    return coverage_xml


def build_config_toml(modules: dict[str, int], tmp_path: Path) -> Path:
    """
    Build a synthetic .coverage-critical.toml file.

    Args:
        modules: {module_path: floor_percentage} mapping
        tmp_path: Temporary directory

    Returns:
        Path to the generated config file
    """
    config_content = "# Test critical module coverage floors\n\n"

    for module, floor in sorted(modules.items()):
        config_content += f'[critical_modules."{module}"]\n'
        config_content += f"branch_coverage_floor = {floor}\n"
        config_content += f"measured_at_baseline = {floor + 5.0}\n\n"

    config_toml = tmp_path / "config.toml"
    with open(config_toml, "w") as f:
        f.write(config_content)

    return config_toml


def test_checker_passes_when_all_modules_meet_floors(tmp_path):
    """The checker should pass when all modules meet or exceed their floors."""
    # Build synthetic coverage.xml
    coverage_xml = build_coverage_xml(
        {
            "eligibility.py": (30, 34),  # 88.24%
            "contracts.py": (320, 398),  # 80.40%
            "orchestration/planner.py": (63, 70),  # 90.00%
        },
        tmp_path,
    )

    # Build config with floors below measured
    config = build_config_toml(
        {
            "verdict/eligibility.py": 85,  # 88.24% > 85% ✓
            "verdict/contracts.py": 80,  # 80.40% > 80% ✓
            "verdict/orchestration/planner.py": 90,  # 90.00% >= 90% ✓
        },
        tmp_path,
    )

    # Run checker
    result = subprocess.run(
        [
            sys.executable,
            "scripts/check_critical_coverage.py",
            "--coverage-xml",
            str(coverage_xml),
            "--config",
            str(config),
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"Checker failed: {result.stderr}\n{result.stdout}"
    assert "All 3 critical modules meet or exceed their coverage floors" in result.stdout


def test_checker_fails_when_module_below_floor(tmp_path):
    """The checker should exit 1 when a module is below its floor."""
    # Build synthetic coverage.xml with low coverage
    coverage_xml = build_coverage_xml(
        {
            "eligibility.py": (15, 34)  # 44.12% (below 85% floor)
        },
        tmp_path,
    )

    # Build config with high floor
    config = build_config_toml({"verdict/eligibility.py": 85}, tmp_path)

    # Run checker
    result = subprocess.run(
        [
            sys.executable,
            "scripts/check_critical_coverage.py",
            "--coverage-xml",
            str(coverage_xml),
            "--config",
            str(config),
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1, f"Checker should have failed but got {result.returncode}"
    assert "module(s) below floor" in result.stderr
    assert "verdict/eligibility.py" in result.stderr


def test_checker_fails_on_missing_module(tmp_path):
    """The checker should exit 1 when a configured module is not measured."""
    # Build synthetic coverage.xml WITHOUT eligibility.py
    coverage_xml = build_coverage_xml({"contracts.py": (320, 398)}, tmp_path)

    # Build config that includes eligibility.py
    config = build_config_toml(
        {
            "verdict/eligibility.py": 85,  # Not in coverage.xml
            "verdict/contracts.py": 80,
        },
        tmp_path,
    )

    # Run checker
    result = subprocess.run(
        [
            sys.executable,
            "scripts/check_critical_coverage.py",
            "--coverage-xml",
            str(coverage_xml),
            "--config",
            str(config),
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1, f"Checker should have failed but got {result.returncode}"
    assert "NOT MEASURED" in result.stdout
    assert "not measured in coverage.xml" in result.stderr


def test_checker_fails_on_missing_coverage_xml(tmp_path):
    """The checker should exit 2 when coverage.xml is missing."""
    # Build config
    config = build_config_toml({"verdict/eligibility.py": 85}, tmp_path)

    # Point to non-existent coverage.xml
    missing_coverage = tmp_path / "missing.xml"

    result = subprocess.run(
        [
            sys.executable,
            "scripts/check_critical_coverage.py",
            "--coverage-xml",
            str(missing_coverage),
            "--config",
            str(config),
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2, f"Expected exit 2 but got {result.returncode}"
    assert "not found" in result.stderr


def test_checker_correctly_maps_package_paths(tmp_path):
    """The checker should correctly build verdict/{package}/{file} paths."""
    # Build coverage.xml with orchestration package structure
    coverage_xml = build_coverage_xml(
        {
            "orchestration/planner.py": (63, 70),  # Package prefix in filename
            "eligibility.py": (30, 34),  # No package prefix
        },
        tmp_path,
    )

    # Build config with correct verdict/* paths
    config = build_config_toml(
        {
            "verdict/orchestration/planner.py": 90,  # verdict/ + orchestration/planner.py
            "verdict/eligibility.py": 85,  # verdict/ + eligibility.py
        },
        tmp_path,
    )

    result = subprocess.run(
        [
            sys.executable,
            "scripts/check_critical_coverage.py",
            "--coverage-xml",
            str(coverage_xml),
            "--config",
            str(config),
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"Path mapping failed: {result.stderr}\n{result.stdout}"
    assert "verdict/orchestration/planner.py" in result.stdout
    assert "verdict/eligibility.py" in result.stdout


def test_all_configured_modules_in_real_config_exist_as_files():
    """All modules in the real .coverage-critical.toml must exist as files."""
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
