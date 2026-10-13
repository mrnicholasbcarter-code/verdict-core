"""``verdict.paths.verdict_home`` is the one VERDICT_HOME resolver."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from verdict.paths import verdict_home

VERDICT_ROOT = Path(__file__).resolve().parents[1] / "verdict"


def test_verdict_home_wins_and_is_stripped(tmp_path: Path) -> None:
    env = {"HOME": str(tmp_path / "home"), "VERDICT_HOME": f"  {tmp_path / 'state'}\n"}
    assert verdict_home(env) == tmp_path / "state"


@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_blank_verdict_home_falls_back_to_home(tmp_path: Path, value: str) -> None:
    assert verdict_home({"HOME": str(tmp_path), "VERDICT_HOME": value}) == tmp_path / ".verdict"


def test_injected_mapping_overrides_process_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "real"))
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / "real-state"))
    assert verdict_home({"HOME": str(tmp_path / "fake")}) == tmp_path / "fake" / ".verdict"
    assert verdict_home({"HOME": f" {tmp_path / 'fake'} "}) == tmp_path / "fake" / ".verdict"


def test_process_env_is_the_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("VERDICT_HOME", raising=False)
    assert verdict_home() == tmp_path / ".verdict"
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / "state"))
    assert verdict_home() == tmp_path / "state"


def test_tilde_expands_against_the_resolved_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "real"))
    monkeypatch.setenv("VERDICT_HOME", "~/state")
    assert verdict_home() == tmp_path / "real" / "state"
    env = {"HOME": str(tmp_path / "fake"), "VERDICT_HOME": " ~/state "}
    assert verdict_home(env) == tmp_path / "fake" / "state"
    assert verdict_home({"HOME": str(tmp_path / "fake"), "VERDICT_HOME": "~"}) == tmp_path / "fake"


def test_paths_module_imports_only_stdlib() -> None:
    tree = ast.parse((VERDICT_ROOT / "paths.py").read_text(encoding="utf-8"))
    modules = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    modules |= {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    non_stdlib = {
        m for m in modules if m.split(".")[0] not in sys.stdlib_module_names | {"__future__"}
    }
    assert non_stdlib == set()


def test_no_other_module_reads_verdict_home_directly() -> None:
    """Any ``"VERDICT_HOME"`` literal (environ.get, getenv, subscript) outside paths.py is drift."""
    offenders = [
        f"{path.relative_to(VERDICT_ROOT)}:{node.lineno}"
        for path in sorted(VERDICT_ROOT.rglob("*.py"))
        if path != VERDICT_ROOT / "paths.py"
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant) and node.value == "VERDICT_HOME"
    ]
    assert offenders == [], f"use verdict.paths.verdict_home(): {offenders}"


def test_no_other_module_hard_codes_home_dot_verdict() -> None:
    """BOD-330: all Verdict state defaults must honor VERDICT_HOME."""
    offenders: list[str] = []
    for path in sorted(VERDICT_ROOT.rglob("*.py")):
        if path == VERDICT_ROOT / "paths.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Div):
                continue
            if not isinstance(node.right, ast.Constant) or node.right.value != ".verdict":
                continue
            left = node.left
            if (
                isinstance(left, ast.Call)
                and isinstance(left.func, ast.Attribute)
                and left.func.attr == "home"
                and isinstance(left.func.value, ast.Name)
                and left.func.value.id == "Path"
            ):
                offenders.append(f"{path.relative_to(VERDICT_ROOT)}:{node.lineno}")
    assert offenders == [], f"use verdict.paths.verdict_home(): {offenders}"
