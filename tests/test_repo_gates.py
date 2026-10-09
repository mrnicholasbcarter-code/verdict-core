"""Tests for pure repo gate discovery (``verdict.orchestration.repo_gates``)."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

import pytest

from verdict.orchestration.repo_gates import RepoGate, describe_gates, discover_repo_gates


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _names(gates: Iterable[RepoGate]) -> list[str]:
    return [g.name for g in gates]


def test_ruff_and_mypy_strict_pyproject(tmp_path: Path) -> None:
    _write(
        tmp_path / "pyproject.toml",
        '[project]\nname = "pkg"\n\n[tool.ruff]\nline-length = 100\n\n'
        '[tool.mypy]\nstrict = true\nfiles = ["src/pkg"]\n',
    )
    gates = discover_repo_gates(tmp_path)
    assert _names(gates) == ["ruff-check", "ruff-format", "mypy"]
    assert gates[0].argv == ("ruff", "check", ".")
    assert gates[1].argv == ("ruff", "format", "--check", ".")
    assert gates[2].argv == ("mypy", "--strict", "src/pkg")
    assert all(g.source == "pyproject:tool.ruff" for g in gates[:2])
    assert gates[2].source == "pyproject:tool.mypy"
    assert describe_gates(gates)["declared"] is True


def test_ruff_only(tmp_path: Path) -> None:
    _write(tmp_path / "pyproject.toml", "[tool.ruff]\nline-length = 100\n")
    gates = discover_repo_gates(tmp_path)
    assert _names(gates) == ["ruff-check", "ruff-format"]
    assert "mypy" not in _names(gates)


def test_mypy_packages_derived_from_project_name_dir(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    _write(tmp_path / "pyproject.toml", '[project]\nname = "pkg"\n\n[tool.mypy]\n')
    gates = discover_repo_gates(tmp_path)
    assert _names(gates) == ["mypy"]
    assert gates[0].argv == ("mypy", "pkg")


def test_package_json_scripts(tmp_path: Path) -> None:
    _write(
        tmp_path / "package.json",
        json.dumps(
            {
                "scripts": {
                    "test": "jest",
                    "lint": "eslint .",
                    "format:check": "prettier --check .",
                    "typecheck": "tsc --noEmit",
                }
            }
        ),
    )
    gates = discover_repo_gates(tmp_path)
    assert _names(gates) == ["typecheck", "lint", "format:check"]
    assert gates[0].argv == ("npm", "run", "typecheck")
    assert gates[1].argv == ("npm", "run", "lint")
    assert gates[2].argv == ("npm", "run", "format:check")
    assert all(g.source == "package.json:scripts" for g in gates)


def test_both_pyproject_and_package_json(tmp_path: Path) -> None:
    _write(tmp_path / "pyproject.toml", '[tool.ruff]\n\n[tool.mypy]\nfiles = ["pkg"]\n')
    _write(
        tmp_path / "package.json",
        json.dumps({"scripts": {"typecheck": "tsc", "lint": "eslint", "format:check": "prettier"}}),
    )
    gates = discover_repo_gates(tmp_path)
    assert _names(gates) == [
        "ruff-check",
        "ruff-format",
        "mypy",
        "typecheck",
        "lint",
        "format:check",
    ]
    assert gates[2].argv == ("mypy", "pkg")


def test_nothing_declared(tmp_path: Path) -> None:
    gates = discover_repo_gates(tmp_path)
    assert gates == ()
    assert describe_gates(gates) == {"gates": [], "declared": False, "note": "none declared"}


def test_malformed_pyproject_raises_value_error(tmp_path: Path) -> None:
    _write(tmp_path / "pyproject.toml", "this is = = not valid toml [[[")
    with pytest.raises(ValueError, match=r"pyproject\.toml"):
        discover_repo_gates(tmp_path)


def test_mypy_files_as_comma_string(tmp_path: Path) -> None:
    _write(tmp_path / "pyproject.toml", '[tool.mypy]\nfiles = "pkg1,pkg2"\n')
    gates = discover_repo_gates(tmp_path)
    assert _names(gates) == ["mypy"]
    assert gates[0].argv == ("mypy", "pkg1", "pkg2")


def test_mypy_packages_flag(tmp_path: Path) -> None:
    _write(tmp_path / "pyproject.toml", '[tool.mypy]\npackages = ["pkg1", "pkg2"]\n')
    gates = discover_repo_gates(tmp_path)
    assert _names(gates) == ["mypy"]
    assert gates[0].argv == ("mypy", "-p", "pkg1", "-p", "pkg2")


def test_mypy_packages_as_comma_string(tmp_path: Path) -> None:
    _write(tmp_path / "pyproject.toml", '[tool.mypy]\npackages = "pkg1,pkg2"\n')
    gates = discover_repo_gates(tmp_path)
    assert gates[0].argv == ("mypy", "-p", "pkg1", "-p", "pkg2")


def test_mypy_hyphenated_name_resolves_via_src_layout(tmp_path: Path) -> None:
    (tmp_path / "src" / "foo_bar").mkdir(parents=True)
    _write(tmp_path / "pyproject.toml", '[project]\nname = "foo-bar"\n\n[tool.mypy]\n')
    gates = discover_repo_gates(tmp_path)
    assert _names(gates) == ["mypy"]
    assert gates[0].argv == ("mypy", "src/foo_bar")


def test_mypy_unresolvable_raises_value_error(tmp_path: Path) -> None:
    _write(
        tmp_path / "pyproject.toml", '[project]\nname = "ghost-pkg"\n\n[tool.mypy]\nstrict = true\n'
    )
    with pytest.raises(ValueError, match=r"no target resolvable"):
        discover_repo_gates(tmp_path)


def test_ruff_toml_beats_pyproject(tmp_path: Path) -> None:
    _write(tmp_path / "pyproject.toml", "[tool.ruff]\nline-length = 100\n")
    _write(tmp_path / "ruff.toml", "line-length = 80\n")
    gates = discover_repo_gates(tmp_path)
    assert _names(gates) == ["ruff-check", "ruff-format"]
    assert all(g.source == "ruff.toml" for g in gates)


def test_dot_ruff_toml_source(tmp_path: Path) -> None:
    _write(tmp_path / ".ruff.toml", "line-length = 88\n")
    gates = discover_repo_gates(tmp_path)
    assert _names(gates) == ["ruff-check", "ruff-format"]
    assert gates[0].source == ".ruff.toml"
    assert gates[1].source == ".ruff.toml"


def test_argv_uses_bare_executables(tmp_path: Path) -> None:
    _write(tmp_path / "pyproject.toml", '[tool.ruff]\n\n[tool.mypy]\nfiles = ["pkg"]\n')
    gates = discover_repo_gates(tmp_path)
    assert _names(gates) == ["ruff-check", "ruff-format", "mypy"]
    assert all(g.argv[0] in ("ruff", "mypy") for g in gates)
    assert all("-m" not in g.argv and "python" not in g.argv for g in gates)


def test_package_json_non_object_root_raises_value_error(tmp_path: Path) -> None:
    _write(tmp_path / "package.json", "[1, 2, 3]")
    with pytest.raises(ValueError, match=r"package\.json"):
        discover_repo_gates(tmp_path)


def test_deterministic_across_separate_repos(tmp_path: Path) -> None:
    # Identical content written to two separate repos in different file/key order.
    pyproject = '[tool.ruff]\n\n[tool.mypy]\nstrict = true\nfiles = ["pkg"]\n'
    scripts_a = {"scripts": {"format:check": "f", "lint": "l", "typecheck": "t"}}
    scripts_b = {"scripts": {"typecheck": "t", "lint": "l", "format:check": "f"}}
    repo_a = tmp_path / "a"
    repo_b = tmp_path / "b"
    repo_a.mkdir()
    repo_b.mkdir()
    _write(repo_a / "pyproject.toml", pyproject)
    _write(repo_a / "package.json", json.dumps(scripts_a))
    _write(repo_b / "package.json", json.dumps(scripts_b))
    _write(repo_b / "pyproject.toml", pyproject)
    first = discover_repo_gates(repo_a)
    second = discover_repo_gates(repo_b)
    assert isinstance(first, tuple)
    assert first == second
    assert describe_gates(first) == describe_gates(second)
    # fixed expected ordering and shape for a repo declaring everything
    assert _names(first) == [
        "ruff-check",
        "ruff-format",
        "mypy",
        "typecheck",
        "lint",
        "format:check",
    ]
    expected = (
        RepoGate("ruff-check", ("ruff", "check", "."), "pyproject:tool.ruff"),
        RepoGate("ruff-format", ("ruff", "format", "--check", "."), "pyproject:tool.ruff"),
        RepoGate("mypy", ("mypy", "--strict", "pkg"), "pyproject:tool.mypy"),
        RepoGate("typecheck", ("npm", "run", "typecheck"), "package.json:scripts"),
        RepoGate("lint", ("npm", "run", "lint"), "package.json:scripts"),
        RepoGate("format:check", ("npm", "run", "format:check"), "package.json:scripts"),
    )
    assert first == expected
