"""The built wheel ships only runtime package files.

Test fixtures, the TypeScript client SDK and stray docs once shipped inside
``verdict/`` because hatch packages the whole directory. This test builds the
wheel the way ``tests/test_flagship_demo.py`` does and checks every entry
against an allowlist of roots and file types, and then against a deny-list.
"""

from __future__ import annotations

import fnmatch
import shutil
import subprocess
import zipfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath

import pytest

ROOT = Path(__file__).parents[1]

ALLOWED_PACKAGE_SUFFIXES = {".py", ".json", ".typed", ".md"}

# Exact non-suffix package data the runtime reads from the installed package.
ALLOWED_PACKAGE_FILES = {
    # Hermes plugin manifest copied by verdict.harness_hermes_memory.install().
    "verdict/hermes_plugin/verdict_memory/plugin.yaml"
}

# Each shipped demo module must be a documented entry point or a runtime import of one.
ALLOWED_DEMO_MODULES = {
    # `verdict quickstart` (README quickstart block).
    "verdict/flagship_demo.py",
    # `python -m verdict.routing_demo` (docs/benchmarks/routing-demo.md).
    "verdict/routing_demo.py",
    # Imported by verdict.routing_demo for its --recorded and --mock modes.
    "verdict/routing_demo_capture.py",
    "verdict/routing_demo_mock.py",
}

DENY_RULES: dict[str, Callable[[PurePosixPath], bool]] = {
    "TypeScript source (*.ts)": lambda path: path.suffix == ".ts",
    "ES module script (*.mjs)": lambda path: path.suffix == ".mjs",
    "tests/ directory": lambda path: "tests" in path.parts[:-1],
    "TypeScript client SDK (npm package)": lambda path: path.parts[:2] == ("verdict", "client-sdk"),
    "Markdown outside verdict/data/": lambda path: (
        path.suffix == ".md" and path.parts[:2] != ("verdict", "data")
    ),
}


@pytest.fixture(scope="module")
def wheel_entries(tmp_path_factory: pytest.TempPathFactory) -> list[PurePosixPath]:
    if shutil.which("uv") is None:
        pytest.skip("uv is required to build the wheel")
    out_dir = tmp_path_factory.mktemp("wheel")
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(out_dir), str(ROOT)],
        check=True,
        capture_output=True,
        text=True,
    )
    (wheel,) = out_dir.glob("verdict_core-*.whl")
    with zipfile.ZipFile(wheel) as archive:
        return [PurePosixPath(name) for name in archive.namelist() if not name.endswith("/")]


def test_wheel_entries_match_the_allowlist(wheel_entries: list[PurePosixPath]) -> None:
    unexpected = [
        str(path)
        for path in wheel_entries
        if not fnmatch.fnmatch(path.parts[0], "verdict_core-*.dist-info")
        and not (path.parts[0] == "verdict" and path.suffix in ALLOWED_PACKAGE_SUFFIXES)
        and str(path) not in ALLOWED_PACKAGE_FILES
    ]
    assert not unexpected, f"wheel ships files outside the allowlist: {unexpected}"


@pytest.mark.parametrize("rule", sorted(DENY_RULES))
def test_wheel_has_no_denied_entries(wheel_entries: list[PurePosixPath], rule: str) -> None:
    denied = [str(path) for path in wheel_entries if DENY_RULES[rule](path)]
    assert not denied, f"wheel ships {rule}: {denied}"


def test_wheel_demo_modules_are_documented_entry_points(wheel_entries: list[PurePosixPath]) -> None:
    demos = {str(path) for path in wheel_entries if fnmatch.fnmatch(path.name, "*_demo*.py")}
    assert demos == ALLOWED_DEMO_MODULES, (
        "a *_demo*.py module ships without being a documented entry point; move test-only "
        "fixtures under tests/, or update ALLOWED_DEMO_MODULES with the documented command"
    )
