"""Focused tests for the local reachability checker (BOD-316)."""

from __future__ import annotations

import importlib.util
import json
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
    # The module is imported from production; only the function is unused.
    root = _repository(
        tmp_path,
        {
            "verdict/fixture.py": _BASELINED_UNCALLED,
            "verdict/__main__.py": "import verdict.fixture\n",
        },
    )
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


@pytest.mark.skipif(
    importlib.util.find_spec("vulture") is None, reason="vulture module not installed"
)
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


@pytest.mark.parametrize("prefix", ["def", "async def"])
def test_lazy_trampolines_with_local_aliases_are_production_callers(
    tmp_path: Path, prefix: str
) -> None:
    root = _repository(
        tmp_path,
        {
            "verdict/fixture.py": "def run(): pass\ndef format_report(): pass\n",
            "caller.py": f"""\
{prefix} run(*args, **kwargs):
    from verdict.fixture import run as _impl
    return _impl(*args, **kwargs)

{prefix} format_report(*args, **kwargs):
    from verdict.fixture import format_report as _impl
    return _impl(*args, **kwargs)
""",
        },
    )

    assert checker.check(root, use_vulture=False).findings == ()


@pytest.mark.parametrize(
    ("local_import", "unrelated_call"),
    [
        ("from verdict.fixture import run as _impl", "_impl()"),
        ("import verdict.fixture as _impl", "_impl.run()"),
        ("import verdict.fixture as _impl", 'getattr(_impl, "run")()'),
    ],
)
def test_local_import_alias_does_not_leak_to_sibling_function(
    tmp_path: Path, local_import: str, unrelated_call: str
) -> None:
    root = _repository(
        tmp_path,
        {
            "verdict/fixture.py": "def run(): pass\n",
            "caller.py": f"""\
def function_a():
    {local_import}

def function_b():
    return {unrelated_call}
""",
        },
    )

    assert [f.qualified_name for f in checker.check(root, use_vulture=False).findings] == [
        "verdict.fixture.run"
    ]


@pytest.mark.parametrize(
    ("local_import", "call"),
    [
        ("import verdict.fixture as mod", "mod.run()"),
        ("import verdict.fixture", "verdict.fixture.run()"),
        ("import verdict.fixture as mod", 'getattr(mod, "run")()'),
    ],
)
def test_local_module_imports_resolve_production_callers(
    tmp_path: Path, local_import: str, call: str
) -> None:
    root = _repository(
        tmp_path,
        {
            "verdict/fixture.py": "def run(): pass\n",
            "caller.py": f"def caller():\n    {local_import}\n    return {call}\n",
        },
    )

    assert checker.check(root, use_vulture=False).findings == ()


@pytest.mark.parametrize("outside_reference", ["_impl()", "def sibling(arg=_impl()): pass"])
def test_local_alias_does_not_resolve_outside_function(
    tmp_path: Path, outside_reference: str
) -> None:
    root = _repository(
        tmp_path,
        {
            "verdict/fixture.py": "def run(): pass\n",
            "caller.py": f"""\
def caller():
    from verdict.fixture import run as _impl

{outside_reference}
""",
        },
    )

    assert [f.qualified_name for f in checker.check(root, use_vulture=False).findings] == [
        "verdict.fixture.run"
    ]


def test_enclosing_local_alias_resolves_in_nested_function(tmp_path: Path) -> None:
    root = _repository(
        tmp_path,
        {
            "verdict/fixture.py": "def run(): pass\n",
            "caller.py": """\
def caller():
    from verdict.fixture import run as _impl
    def nested():
        return _impl()
    return nested()
""",
        },
    )

    assert checker.check(root, use_vulture=False).findings == ()


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


@pytest.mark.parametrize("decorator", ["@app.get('/')", "@router.post('/')", "@register('event')"])
def test_registration_decorators_are_production_roots(tmp_path: Path, decorator: str) -> None:
    root = _repository(tmp_path, {"verdict/m.py": f"{decorator}\ndef f(): pass\n"})
    assert checker.check(root, use_vulture=False).findings == ()


def test_script_entry_point_and_literal_getattr_are_roots(tmp_path: Path) -> None:
    root = _repository(
        tmp_path,
        {
            "verdict/m.py": "def main(): pass\ndef f(): pass\n",
            "pyproject.toml": '[project.scripts]\ncli = "verdict.m:main"\n',
            "caller.py": 'import verdict.m as mod; getattr(mod, "f")()\n',
        },
    )
    assert checker.check(root, use_vulture=False).findings == ()


@pytest.mark.parametrize("prefix", ["def", "async def"])
@pytest.mark.parametrize(
    "signature", ['health="disabled"', 'health="off", /', '*, health="unhealthy"']
)
def test_stub_positional_and_async_defaults(tmp_path: Path, prefix: str, signature: str) -> None:
    root = _repository(tmp_path, {"verdict/m.py": f"{prefix} make_stub({signature}): pass\n"})
    assert any(f.kind == "stub_default" for f in checker.check(root, use_vulture=False).findings)


def test_stale_baseline_entries_are_reported_without_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repository(tmp_path, {"verdict/m.py": "", "verdict/__main__.py": "import verdict.m\n"})
    baseline = root / "reachability-baseline.json"
    _write_baseline(baseline, [{"qualified_name": "verdict.m.removed", "reason": "removed API"}])
    result = checker.check(root, baseline_path=baseline, use_vulture=False)
    assert result.ok
    assert result.to_dict()["stale_baseline_entries"] == ["verdict.m.removed"]
    monkeypatch.setattr(checker, "check", lambda **kwargs: result)
    assert checker.main(["--skip-vulture", "--json"]) == 0
    captured = capsys.readouterr()
    assert "warning: stale baseline entries: verdict.m.removed" in captured.err
    assert json.loads(captured.out)["stale_baseline_entries"] == ["verdict.m.removed"]


def test_vulture_uses_current_interpreter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(
            args, 1, "verdict/m.py:1: unused function 'f' (60% confidence)", ""
        )

    monkeypatch.setattr(checker.subprocess, "run", fake_run)
    assert checker.run_vulture(tmp_path) == {
        ("verdict/m.py", 1): "unused function 'f' (60% confidence)"
    }
    assert calls[0][:3] == [sys.executable, "-m", "vulture"]


def _commit_all(root: Path, message: str = "x") -> None:
    for args in (["add", "-A"], ["commit", "-qm", message]):
        subprocess.run(
            ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", *args],
            cwd=root,
            check=True,
        )


@pytest.mark.parametrize(
    ("files", "orphans"),
    [
        ({"verdict/m.py": "", "verdict/__main__.py": ""}, ["verdict.m"]),
        ({"verdict/m.py": "", "verdict/__main__.py": "import verdict.m\n"}, []),
        ({"verdict/m.py": "", "scripts/tool.py": "from verdict import m\n"}, []),
        (
            {"verdict/m.py": "", "verdict/r.py": 'MODULES = ["verdict.m"]\n'},
            ["verdict.m", "verdict.r"],
        ),
        (
            {
                "verdict/m.py": "",
                "verdict/r.py": 'MODULES = ["verdict.m"]\n',
                "verdict/__main__.py": "import verdict.r\n",
            },
            [],
        ),
        (
            {
                "verdict/m.py": "",
                "verdict/r.py": 'X = "verdict.m:run"\n',
                "scripts/t.py": "import verdict.r",
            },
            [],
        ),
        ({"verdict/m.py": "", "pyproject.toml": '[project.scripts]\nm = "verdict.m:main"\n'}, []),
        ({"verdict/m.py": "", "tests/test_m.py": "import verdict.m\n"}, ["verdict.m"]),
        (
            {
                "verdict/a/m.py": "",
                "verdict/a/b.py": "from . import m\n",
                "scripts/t.py": "import verdict.a.b",
            },
            [],
        ),
    ],
)
def test_orphan_modules(tmp_path: Path, files: dict[str, str], orphans: list[str]) -> None:
    root = _repository(tmp_path, files)
    result = checker.check(root, use_vulture=False)
    found = [f.qualified_name for f in result.module_findings]
    assert found == orphans
    assert result.ok is (not orphans)


def test_forbidden_symbol_reference_outside_allowed_modules_fails(tmp_path: Path) -> None:
    rule = [{"symbol": "verdict.gate.Gate", "allowed_modules": ["verdict.cli"], "reason": "legacy"}]
    root = _repository(
        tmp_path,
        {
            "verdict/gate.py": "class Gate: pass\n",
            "verdict/cli.py": "from verdict.gate import Gate\nGate()\n",
            "verdict/__main__.py": "import verdict.cli, verdict.api\n",
            "verdict/api.py": "",
            "tests/test_gate.py": "from verdict.gate import Gate\nGate()\n",
            "reachability-forbidden.json": json.dumps(rule),
        },
    )
    assert checker.check(root, use_vulture=False).ok
    (root / "verdict/api.py").write_text(
        "from verdict.gate import Gate\nGate()\n", encoding="utf-8"
    )
    result = checker.check(root, use_vulture=False)
    assert result.forbidden_references == ("verdict.gate.Gate in verdict.api",)
    assert not result.ok


@pytest.fixture
def ratchet_repo(tmp_path: Path) -> Path:
    root = _repository(
        tmp_path,
        {
            "verdict/__main__.py": "import verdict.used\n",
            "verdict/used.py": "def unused_a(): pass\ndef unused_b(): pass\n",
            "verdict/orphan.py": "",
            "reachability-baseline.json": json.dumps(
                [
                    {"qualified_name": "verdict.used.unused_a", "reason": "UNTRIAGED: a"},
                    {"qualified_name": "verdict.used.unused_b", "reason": "WIRE: b"},
                ]
            ),
            "reachability-modules-baseline.json": json.dumps(
                [{"qualified_name": "verdict.orphan", "reason": "UNTRIAGED: orphan"}]
            ),
        },
    )
    _commit_all(root, "base")
    subprocess.run(["git", "branch", "base"], cwd=root, check=True)
    return root


def _ratchet(root: Path, min_burn: int = 0) -> list[str]:
    result = checker.check(root, use_vulture=False)
    assert result.ok, result.new_findings
    return checker.ratchet(root, result, "base", min_burn=min_burn)


def test_ratchet_passes_when_nothing_changes(ratchet_repo: Path) -> None:
    assert _ratchet(ratchet_repo) == []


def test_ratchet_fails_on_a_growing_baseline(ratchet_repo: Path) -> None:
    (ratchet_repo / "verdict/used.py").write_text(
        "def unused_a(): pass\ndef unused_b(): pass\ndef unused_c(): pass\n", encoding="utf-8"
    )
    baseline = json.loads((ratchet_repo / "reachability-baseline.json").read_text())
    baseline.append({"qualified_name": "verdict.used.unused_c", "reason": "WIRE: later"})
    _write_baseline(ratchet_repo / "reachability-baseline.json", baseline)
    _commit_all(ratchet_repo)
    errors = _ratchet(ratchet_repo)
    assert any("new entry verdict.used.unused_c" in e for e in errors), errors


def test_ratchet_fails_on_untriaged_in_a_touched_module(ratchet_repo: Path) -> None:
    (ratchet_repo / "verdict/used.py").write_text(
        "def unused_a(): pass\ndef unused_b(): pass\nX = 1\n", encoding="utf-8"
    )
    _commit_all(ratchet_repo)
    errors = _ratchet(ratchet_repo)
    assert errors == [
        "verdict.used.unused_a is UNTRIAGED in a module this branch changed (verdict.used): "
        "wire it, delete it, or give it a triage reason"
    ]


def test_ratchet_fails_on_a_stale_entry_and_on_too_little_burn(ratchet_repo: Path) -> None:
    (ratchet_repo / "verdict/orphan.py").unlink()
    _commit_all(ratchet_repo)
    errors = _ratchet(ratchet_repo, min_burn=1)
    assert any("stale baseline entry verdict.orphan" in e for e in errors), errors
    (ratchet_repo / "reachability-modules-baseline.json").write_text("[]", encoding="utf-8")
    _commit_all(ratchet_repo)
    assert _ratchet(ratchet_repo, min_burn=1) == []
    assert any("must burn at least 2" in e for e in _ratchet(ratchet_repo, min_burn=2))


def test_ratchet_seeds_a_baseline_absent_at_base(ratchet_repo: Path) -> None:
    subprocess.run(
        ["git", "rm", "-q", "reachability-modules-baseline.json"], cwd=ratchet_repo, check=True
    )
    _commit_all(ratchet_repo)
    subprocess.run(["git", "branch", "-f", "base"], cwd=ratchet_repo, check=True)
    (ratchet_repo / "reachability-modules-baseline.json").write_text(
        json.dumps([{"qualified_name": "verdict.orphan", "reason": "WIRE: seeded"}]),
        encoding="utf-8",
    )
    _commit_all(ratchet_repo)
    assert _ratchet(ratchet_repo) == []


def test_ratchet_rejects_growing_or_stale_forbidden_allowlists(ratchet_repo: Path) -> None:
    def write(name: str, text: str) -> None:
        (ratchet_repo / name).write_text(text, encoding="utf-8")

    use = "from verdict.used import unused_a\nunused_a()\n"
    rule = {"symbol": "verdict.used.unused_a", "allowed_modules": ["verdict.x"], "reason": "legacy"}
    write("verdict/x.py", use)
    write("verdict/__main__.py", "import verdict.used, verdict.x\n")
    baseline = json.loads((ratchet_repo / "reachability-baseline.json").read_text())
    baseline = [e for e in baseline if e["qualified_name"] != "verdict.used.unused_a"]
    _write_baseline(ratchet_repo / "reachability-baseline.json", baseline)
    write("reachability-forbidden.json", json.dumps([rule]))
    _commit_all(ratchet_repo)
    subprocess.run(["git", "branch", "-f", "base"], cwd=ratchet_repo, check=True)
    assert _ratchet(ratchet_repo) == []

    write("verdict/y.py", use)
    write("verdict/__main__.py", "import verdict.used, verdict.x, verdict.y\n")
    rule["allowed_modules"] = ["verdict.x", "verdict.y"]
    write("reachability-forbidden.json", json.dumps([rule]))
    _commit_all(ratchet_repo)
    assert any("allowed_modules grew: verdict.y" in e for e in _ratchet(ratchet_repo))

    write("verdict/y.py", "")
    write("verdict/x.py", "")
    write("verdict/z.py", use)
    write("verdict/__main__.py", "import verdict.used, verdict.x, verdict.y, verdict.z\n")
    rule["allowed_modules"] = ["verdict.x"]
    write("reachability-forbidden.json", json.dumps([rule]))
    _commit_all(ratchet_repo)
    result = checker.check(ratchet_repo, use_vulture=False)
    assert result.forbidden_references == ("verdict.used.unused_a in verdict.z",)
    errors = checker.ratchet(ratchet_repo, result, "base")
    assert any("no longer used by verdict.x" in e for e in errors), errors


def test_ratchet_compares_with_the_fork_point_not_a_newer_base(ratchet_repo: Path) -> None:
    """A branch behind its base keeps an entry the base removed: not a new entry."""
    subprocess.run(["git", "checkout", "-q", "-b", "story", "base"], cwd=ratchet_repo, check=True)
    subprocess.run(["git", "checkout", "-q", "base"], cwd=ratchet_repo, check=True)
    (ratchet_repo / "verdict/used.py").write_text("def unused_a(): pass\n", encoding="utf-8")
    baseline = json.loads((ratchet_repo / "reachability-baseline.json").read_text())
    _write_baseline(
        ratchet_repo / "reachability-baseline.json",
        [e for e in baseline if e["qualified_name"] != "verdict.used.unused_b"],
    )
    _commit_all(ratchet_repo, "base removes unused_b")
    subprocess.run(["git", "checkout", "-q", "story"], cwd=ratchet_repo, check=True)
    (ratchet_repo / "scripts").mkdir(exist_ok=True)
    (ratchet_repo / "scripts/tool.py").write_text("X = 1\n", encoding="utf-8")
    _commit_all(ratchet_repo, "unrelated story change")
    assert _ratchet(ratchet_repo) == []


def test_unknown_ratchet_base_is_an_error(ratchet_repo: Path) -> None:
    result = checker.check(ratchet_repo, use_vulture=False)
    with pytest.raises(RuntimeError, match="unknown ratchet base"):
        checker.ratchet(ratchet_repo, result, "no-such-ref")


# These five still have no production importer. execution_hosts, the experimental
# memory provider, and workflows.autodev await a controller decision; their parent
# packages stay with them. Every other orphan module must leave the baseline.
PENDING_CONTROLLER_ORPHANS = frozenset(
    {
        "verdict.execution_hosts",
        "verdict.memory_providers",
        "verdict.memory_providers.ai_memory_experimental",
        "verdict.workflows",
        "verdict.workflows.autodev",
    }
)


def test_untriaged_orphan_modules_are_only_the_pending_controller_set() -> None:
    """Only the five controller-pending orphan modules may stay UNTRIAGED."""
    root = Path(__file__).parents[1]
    entries = checker.load_baseline(root / checker.MODULE_BASELINE_NAME)
    untriaged = {
        entry.qualified_name
        for entry in entries
        if entry.reason.startswith(checker.UNTRIAGED)
    }
    assert untriaged == PENDING_CONTROLLER_ORPHANS
