"""Focused tests for the local reachability checker (BOD-316)."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _load_checker() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "check_reachability.py"
    spec = importlib.util.spec_from_file_location("check_reachability", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


def _repository(tmp_path: Path, files: dict[str, str]) -> Path:
    """A throwaway local git repo (no network) with ``files`` tracked."""
    root = tmp_path / "repository"
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    for relative_path, content in files.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    return root


def _write_baseline(path: Path, entries: list[dict[str, object]]) -> None:
    path.write_text(json.dumps(entries), encoding="utf-8")


_PRODUCTION_CALLED = """\
def production_called() -> int:
    return 1
"""

_TEST_ONLY_CALLED = """\
def test_only_called() -> int:
    return 2
"""

_BASELINED_UNCALLED = """\
def baselined_uncalled() -> int:
    return 3
"""

_CALLER_MODULE = """\
from verdict.fixture import production_called

def use_it() -> int:
    return production_called()
"""

_TEST_CALLER_MODULE = """\
from verdict.fixture import test_only_called

def test_calls_it() -> None:
    assert test_only_called() == 2
"""


def test_production_called_function_is_not_flagged(tmp_path: Path) -> None:
    root = _repository(
        tmp_path, {"verdict/fixture.py": _PRODUCTION_CALLED, "verdict/caller.py": _CALLER_MODULE}
    )

    result = checker.check(root, use_vulture=False)

    flagged = {f.qualified_name for f in result.findings}
    assert "verdict.fixture.production_called" not in flagged


def test_test_only_called_function_is_flagged(tmp_path: Path) -> None:
    root = _repository(
        tmp_path,
        {"verdict/fixture.py": _TEST_ONLY_CALLED, "tests/test_fixture.py": _TEST_CALLER_MODULE},
    )

    result = checker.check(root, use_vulture=False)

    matches = [f for f in result.findings if f.qualified_name == "verdict.fixture.test_only_called"]
    assert len(matches) == 1
    assert matches[0].kind == "test_only_caller"
    assert matches[0] in result.new_findings


def test_baselined_entry_passes_and_is_excluded_from_new_findings(tmp_path: Path) -> None:
    root = _repository(tmp_path, {"verdict/fixture.py": _BASELINED_UNCALLED})
    baseline = tmp_path / "reachability-baseline.json"
    _write_baseline(
        baseline,
        [
            {
                "qualified_name": "verdict.fixture.baselined_uncalled",
                "reason": "fixture: pre-existing exception for this test",
            }
        ],
    )

    result = checker.check(root, baseline_path=baseline, use_vulture=False)

    flagged = {f.qualified_name for f in result.findings}
    assert "verdict.fixture.baselined_uncalled" in flagged
    new_flagged = {f.qualified_name for f in result.new_findings}
    assert "verdict.fixture.baselined_uncalled" not in new_flagged
    assert result.ok is True


def test_uncalled_function_without_baseline_fails(tmp_path: Path) -> None:
    root = _repository(tmp_path, {"verdict/fixture.py": _BASELINED_UNCALLED})

    result = checker.check(root, use_vulture=False)

    assert result.ok is False
    assert any(
        f.qualified_name == "verdict.fixture.baselined_uncalled" for f in result.new_findings
    )


def test_stub_factory_with_off_default_health_is_flagged(tmp_path: Path) -> None:
    stub_module = """\
def make_example_stub(*, health: str = "unavailable") -> dict[str, str]:
    return {"health": health}
"""
    root = _repository(tmp_path, {"verdict/fixture.py": stub_module})

    result = checker.check(root, use_vulture=False)

    stub_findings = [f for f in result.findings if f.kind == "stub_default"]
    assert any(f.qualified_name == "verdict.fixture.make_example_stub" for f in stub_findings)


def test_json_and_text_rendering_are_stable(tmp_path: Path) -> None:
    root = _repository(
        tmp_path,
        {"verdict/fixture.py": _TEST_ONLY_CALLED, "tests/test_fixture.py": _TEST_CALLER_MODULE},
    )

    first = checker.check(root, use_vulture=False)
    second = checker.check(root, use_vulture=False)

    assert json.dumps(first.to_dict(), sort_keys=True) == json.dumps(
        second.to_dict(), sort_keys=True
    )
    assert checker.render_text(first) == checker.render_text(second)


def test_malformed_baseline_entries_are_rejected(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    _write_baseline(baseline, [{"qualified_name": "verdict.fixture.x"}])

    with pytest.raises(checker.BaselineError):
        checker.load_baseline(baseline)


def test_duplicate_baseline_entries_are_rejected(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    _write_baseline(
        baseline,
        [
            {"qualified_name": "verdict.fixture.x", "reason": "a"},
            {"qualified_name": "verdict.fixture.x", "reason": "b"},
        ],
    )

    with pytest.raises(checker.BaselineError, match="duplicate"):
        checker.load_baseline(baseline)


@pytest.mark.skipif(shutil.which("vulture") is None, reason="vulture not on PATH")
def test_vulture_cross_check_annotates_uncalled_findings(tmp_path: Path) -> None:
    root = _repository(tmp_path, {"verdict/fixture.py": _BASELINED_UNCALLED})

    result = checker.check(root, use_vulture=True)

    matches = [
        f for f in result.findings if f.qualified_name == "verdict.fixture.baselined_uncalled"
    ]
    assert len(matches) == 1
    assert "vulture" in matches[0].detail


def test_qualified_name_uses_the_full_module_path(tmp_path: Path) -> None:
    """verdict/release/normalize.py's symbols must be verdict.release.normalize.*,

    not verdict.normalize.* (Path(...).stem drops the package directory).
    """
    root = _repository(tmp_path, {"verdict/release/normalize.py": _BASELINED_UNCALLED})

    result = checker.check(root, use_vulture=False)

    flagged = {f.qualified_name for f in result.findings}
    assert "verdict.release.normalize.baselined_uncalled" in flagged
    assert "verdict.normalize.baselined_uncalled" not in flagged


def test_two_modules_sharing_a_filename_stem_do_not_collide(tmp_path: Path) -> None:
    """Two same-stem modules each defining the same symbol name must produce

    two distinct qualified names, so one baseline entry cannot silently
    exempt both.
    """
    orphan = "def orphan_api() -> int:\n    return 1\n"
    root = _repository(
        tmp_path, {"verdict/one/fixture.py": orphan, "verdict/two/fixture.py": orphan}
    )

    result = checker.check(root, use_vulture=False)

    flagged = {f.qualified_name for f in result.findings}
    assert "verdict.one.fixture.orphan_api" in flagged
    assert "verdict.two.fixture.orphan_api" in flagged


@pytest.mark.parametrize(
    ("caller_path", "caller", "kind"),
    [
        ("caller.py", "conn.f()", "uncalled"),
        ("caller.py", "", "uncalled"),
        ("caller.py", "from verdict.m import f as z; z()", None),
        ("tests/test_m.py", "from verdict.m import f as z; z()", "test_only_caller"),
        (".vulture-whitelist.py", "from verdict.m import f; f()", "uncalled"),
        ("caller.py", "import verdict.m as mod; mod.f()", None),
        ("caller.py", "import verdict.m; verdict.m.f()", None),
    ],
)
def test_module_resolution(tmp_path: Path, caller_path: str, caller: str, kind: str | None) -> None:
    root = _repository(tmp_path, {"verdict/m.py": "def f(): return f()\n", caller_path: caller})
    result = checker.check(root, use_vulture=False)
    assert [f.kind for f in result.findings if f.qualified_name == "verdict.m.f"] == (
        [] if kind is None else [kind]
    )


def test_same_stem_call_and_unrelated_rollback_do_not_hide_functions(tmp_path: Path) -> None:
    root = _repository(
        tmp_path,
        {
            "verdict/a/m.py": "def f(): pass\n",
            "verdict/b/m.py": "def f(): pass\n",
            "verdict/x.py": "def rollback(): pass\n",
            "caller.py": "from verdict.a.m import f; f(); conn.rollback()\n",
        },
    )
    names = {f.qualified_name for f in checker.check(root, use_vulture=False).findings}
    assert names == {"verdict.b.m.f", "verdict.x.rollback"}
