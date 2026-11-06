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
def built_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
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
    return wheel


@pytest.fixture(scope="module")
def wheel_entries(built_wheel: Path) -> list[PurePosixPath]:
    with zipfile.ZipFile(built_wheel) as archive:
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


def test_installed_wheel_reads_the_ocr_pin_without_a_checkout(
    built_wheel: Path, tmp_path: Path
) -> None:
    """``verdict doctor`` and ``verdict setup`` read the ocr pin from the installed package."""
    from tests.test_ocr_release import read_pin_from
    from verdict import ocr_release

    site = tmp_path / "site-packages"
    with zipfile.ZipFile(built_wheel) as archive:
        archive.extractall(site)
    assert not (site / ".github").exists()
    where, pin = read_pin_from(site, cwd=tmp_path)
    assert where == site / "verdict" / "ocr_release.py"
    assert pin == ocr_release.ocr_pin()


# Runs the doctor ocr row and the setup ocr action from the extracted wheel. The
# HostEnvironment is the one ``from_host()`` builds; only ``which`` (no ocr on
# PATH) and ``fetch`` (bytes that cannot match the pin; no network) are replaced.
_OCR_HOST_STEPS = """
import dataclasses, json, sys
sys.path.insert(0, sys.argv[1])
from verdict import setup_host
fetched = []
def fetch(url):
    fetched.append(url)
    return b"not the pinned asset"
env = dataclasses.replace(
    setup_host.HostEnvironment.from_host(), which=lambda name: None, fetch=fetch
)
problems = setup_host._observe(env, setup_host.OCR)
result = setup_host.run_host_action(setup_host._action(setup_host.OCR, problems), env)
print(json.dumps({
    "module": setup_host.__file__,
    "row": setup_host._ocr_row(env),
    "problems": problems,
    "fetched": fetched,
    "result": {"status": result["status"], "note": result["note"]},
}))
"""


def test_installed_wheel_runs_the_ocr_doctor_row_and_setup_action(
    built_wheel: Path, tmp_path: Path
) -> None:
    """Live defect, end to end: doctor and setup run from the wheel with no checkout."""
    import json
    import os
    import sys

    from verdict import ocr_release

    version, digest = ocr_release.ocr_pin()
    site = tmp_path / "site-packages"
    with zipfile.ZipFile(built_wheel) as archive:
        archive.extractall(site)
    home = tmp_path / "home"
    home.mkdir()
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("OMNIROUTE", "VERDICT_OMNIROUTE", "XDG_"))
    }
    env.update(HOME=str(home), VERDICT_HOME=str(home / "state"), VERDICT_RECEIPTS_DB=":memory:")
    run = subprocess.run(
        [sys.executable, "-I", "-c", _OCR_HOST_STEPS, str(site)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    seen = json.loads(run.stdout.splitlines()[-1])
    assert Path(seen["module"]) == site / "verdict" / "setup_host.py"
    missing = f"not on PATH; verdict.ocr_release pins {version}"
    assert seen["row"]["status"] == "failed"
    assert seen["row"]["detail"] == f"{missing}; repair: verdict setup --apply --allow host.ocr"
    assert seen["problems"] == [missing]
    assert seen["fetched"] == [ocr_release.OCR_ASSET_URL.format(version=version)]
    assert seen["result"]["status"] == "failed"
    assert digest in seen["result"]["note"] and "nothing installed" in seen["result"]["note"]
    assert not ocr_release.install_target(home).exists()


def test_wheel_demo_modules_are_documented_entry_points(wheel_entries: list[PurePosixPath]) -> None:
    demos = {str(path) for path in wheel_entries if fnmatch.fnmatch(path.name, "*_demo*.py")}
    assert demos == ALLOWED_DEMO_MODULES, (
        "a *_demo*.py module ships without being a documented entry point; move test-only "
        "fixtures under tests/, or update ALLOWED_DEMO_MODULES with the documented command"
    )
