"""Wheel must contain real modules; isolated install must import without source/PYTHONPATH."""

from __future__ import annotations

import os
import subprocess
import sys
import zipfile
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_MODULE_PREFIXES = (
    "verdict_memory/__init__.py",
    "verdict_memory/contracts.py",
    "verdict_memory/fake.py",
    "verdict_memory/policy.py",
    "verdict_memory/provider.py",
    "verdict_memory/outbox.py",
    "verdict_memory/client.py",
    "verdict_memory/vectors/lifecycle_event_v1_minimal.json",
    "verdict_memory/vectors/golden/lifecycle-event-v1/SHA256SUMS",
    "verdict_memory/vectors/golden/lifecycle-event-v1/manifest.json",
    "verdict_memory/vectors/golden/lifecycle-event-v1/README.md",
    "verdict_memory/vectors/golden/lifecycle-event-v1/valid/minimal_source_event/canonical.json",
    "verdict_memory/vectors/golden/lifecycle-event-v1/invalid/unknown_kind.json",
    "verdict_memory/scripts/gen_golden_vectors.py",
)


def _build_wheel(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir()
    # Build in an isolated build env; do not rely on editable install.
    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(dist)],
        cwd=PKG_ROOT,
        check=True,
        env={**os.environ, "PYTHONPATH": ""},
    )
    wheels = list(dist.glob("verdict_memory-*.whl"))
    assert wheels, "wheel was not produced"
    return wheels[0]


def test_wheel_contains_public_modules(tmp_path: Path) -> None:
    wheel = _build_wheel(tmp_path)
    with zipfile.ZipFile(wheel) as zf:
        names = set(zf.namelist())
    missing = [p for p in REQUIRED_MODULE_PREFIXES if p not in names]
    # Preserve the empty-wheel failure mode as an explicit assertion.
    assert names - {n for n in names if n.startswith("verdict_memory-") and "dist-info" in n}, (
        "empty wheel regression: only dist-info present"
    )
    assert not missing, f"wheel missing required files: {missing}; had={sorted(names)}"


def test_isolated_install_imports_without_source(tmp_path: Path) -> None:
    wheel = _build_wheel(tmp_path)
    venv = tmp_path / "isolated"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    pip = venv / "bin" / "pip"
    py = venv / "bin" / "python"
    subprocess.run([str(pip), "install", str(wheel)], check=True)
    # Prove import works with cwd outside package and empty PYTHONPATH.
    outside = tmp_path / "outside"
    outside.mkdir()
    probe = (
        "import verdict_memory, verdict_memory.contracts, verdict_memory.client, "
        "verdict_memory.outbox, verdict_memory.fake\n"
        "from verdict_memory import LifecycleEvent, CONTRACT_API_VERSION, LIFECYCLE_EVENT_SCHEMA\n"
        "from importlib.resources import files\n"
        "vec = files('verdict_memory.vectors').joinpath('lifecycle_event_v1_minimal.json')\n"
        "assert vec.is_file(), 'golden vector missing from installed package'\n"
        "assert CONTRACT_API_VERSION == '0.1.0a1'\n"
        "print('ok', verdict_memory.__file__)\n"
    )
    env = {**os.environ, "PYTHONPATH": ""}
    # Remove any accidental src path
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [str(py), "-c", probe],
        cwd=outside,
        env={**env, "PYTHONPATH": ""},
        check=True,
        capture_output=True,
        text=True,
    )
    assert "ok" in result.stdout
    assert "packages/verdict-memory/src" not in result.stdout
    # The shipped generator must verify the shipped golden bytes from the install.
    installed_pkg = Path(result.stdout.split()[-1]).parent
    gen = installed_pkg / "scripts" / "gen_golden_vectors.py"
    check = subprocess.run(
        [str(py), str(gen), "--check"],
        cwd=outside,
        env={**env, "PYTHONPATH": ""},
        capture_output=True,
        text=True,
        check=False,
    )
    assert check.returncode == 0, check.stdout + check.stderr
    sums = installed_pkg / "vectors" / "golden" / "lifecycle-event-v1" / "SHA256SUMS"
    assert sums.is_file()
