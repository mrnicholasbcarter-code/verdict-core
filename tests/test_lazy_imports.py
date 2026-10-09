"""BOD-321: `import verdict` must stay cheap (lazy public re-exports).

Importing the top-level `verdict` package must not eagerly pull in heavy
submodules such as `verdict.benchmarking`, `verdict.intelligence`, or the
`httpx` dependency. Every name previously defined in `verdict.__all__` must
still resolve as a plain attribute access / `from verdict import X`, and
`python -m verdict --version` output must be unchanged from before BOD-321.
"""

from __future__ import annotations

import subprocess
import sys

import verdict

# Modules that were eagerly imported by `verdict/__init__.py` before BOD-321
# (measured via `python -X importtime -c "import verdict.cli"` on the
# pre-change tree): `import verdict` alone pulled in the full chain
# benchmarking -> comparison -> gate -> intelligence -> admit_prove_confirm
# -> free_tier_admit -> httpx. None of these may load from a bare
# `import verdict`.
_FORBIDDEN_ON_BARE_IMPORT = (
    "httpx",
    "verdict.benchmarking",
    "verdict.comparison",
    "verdict.gate",
    "verdict.intelligence",
    "verdict.admit_prove_confirm",
    "verdict.free_tier_admit",
)


def test_bare_import_does_not_load_heavy_modules() -> None:
    """`import verdict` alone must not import httpx or the heavy submodules.

    Runs in a subprocess so the assertion observes a clean `sys.modules`,
    independent of whatever this test process has already imported.
    """
    probe = (
        "import sys\n"
        "import verdict\n"
        f"leaked = [m for m in sys.modules if m in {set(_FORBIDDEN_ON_BARE_IMPORT)!r}]\n"
        "print(','.join(sorted(leaked)))\n"
    )
    result = subprocess.run(
        (sys.executable, "-c", probe), check=True, capture_output=True, text=True
    )
    leaked = [m for m in result.stdout.strip().split(",") if m]
    assert leaked == [], f"bare `import verdict` pulled in: {leaked}"


def test_all_public_names_are_importable() -> None:
    """Every name in `verdict.__all__` resolves via attribute access."""
    missing: list[str] = []
    for name in verdict.__all__:
        try:
            getattr(verdict, name)
        except AttributeError:
            missing.append(name)
    assert missing == [], f"verdict.__all__ names not resolvable: {missing}"


def test_from_verdict_import_still_works_for_all_names() -> None:
    """`from verdict import X` keeps working for every public name.

    Exercises the import-system codepath (distinct from plain
    ``getattr``), which also depends on PEP 562 ``module.__getattr__``.
    Runs a single subprocess (not one per name) to keep this test fast.
    """
    probe = (
        "import verdict\n"
        "missing = []\n"
        "for name in verdict.__all__:\n"
        "    stmt = f'from verdict import {name}'\n"
        "    try:\n"
        "        exec(stmt, {})\n"
        "    except ImportError:\n"
        "        missing.append(name)\n"
        "print(','.join(missing))\n"
    )
    result = subprocess.run(
        (sys.executable, "-c", probe), check=True, capture_output=True, text=True
    )
    missing = [m for m in result.stdout.strip().split(",") if m]
    assert missing == [], f"`from verdict import X` failed for: {missing}"


def test_version_cli_output_unchanged() -> None:
    """`python -m verdict --version` output is unchanged by the lazy-import move."""
    result = subprocess.run(
        (sys.executable, "-m", "verdict", "--version"), check=True, capture_output=True, text=True
    )
    assert result.stdout.strip() == f"verdict-core __main__.py {verdict.__version__}"
