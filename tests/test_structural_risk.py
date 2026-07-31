"""Focused tests for the local structural-risk line-count checker."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _load_checker() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "check_structural_risk.py"
    spec = importlib.util.spec_from_file_location("check_structural_risk", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


def _repository(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "repository"
    (root / "verdict").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    for relative_path, content in files.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "verdict"], cwd=root, check=True)
    return root


def _write_baseline(path: Path, entries: list[dict[str, object]]) -> None:
    path.write_text(json.dumps(entries), encoding="utf-8")


def test_clean_tracked_files_pass_and_untracked_files_are_ignored(tmp_path: Path) -> None:
    root = _repository(tmp_path, {"verdict/clean.py": "one\ntwo\n"})
    (root / "verdict" / "untracked.py").write_text("line\n" * 20, encoding="utf-8")

    result = checker.check(root, max_lines=2)

    assert result.ok is True
    assert result.checked_files == 1
    assert result.findings == ()


def test_missing_explicit_baseline_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(checker.BaselineError, match="cannot read baseline"):
        checker.load_baseline(tmp_path / "missing.json")


def test_baseline_exception_uses_its_own_ceiling(tmp_path: Path) -> None:
    root = _repository(tmp_path, {"verdict/legacy.py": "line\n" * 4})
    baseline = tmp_path / "baseline.json"
    _write_baseline(
        baseline,
        [{"path": "verdict/legacy.py", "max_lines": 4, "rationale": "migration in progress"}],
    )

    assert checker.check(root, max_lines=2, baseline_path=baseline).ok is True

    (root / "verdict" / "legacy.py").write_text("line\n" * 5, encoding="utf-8")
    result = checker.check(root, max_lines=2, baseline_path=baseline)
    assert [finding.kind for finding in result.findings] == ["line_limit"]
    assert result.findings[0].max_lines == 4


@pytest.mark.parametrize("state", ["missing", "no_longer_needed"])
def test_stale_baseline_entries_fail(tmp_path: Path, state: str) -> None:
    content = "line\n"
    root = _repository(tmp_path, {"verdict/current.py": content})
    baseline_path = "verdict/missing.py" if state == "missing" else "verdict/current.py"
    baseline = tmp_path / "baseline.json"
    _write_baseline(
        baseline, [{"path": baseline_path, "max_lines": 4, "rationale": "temporary exception"}]
    )

    result = checker.check(root, max_lines=2, baseline_path=baseline)

    assert result.ok is False
    assert [(finding.path, finding.kind) for finding in result.findings] == [
        (baseline_path, "stale_baseline")
    ]


@pytest.mark.parametrize(
    "entry",
    [
        {"path": "../outside.py", "max_lines": 10, "rationale": "unsafe"},
        {"path": "verdict/nested/../../outside.py", "max_lines": 10, "rationale": "unsafe"},
        {"path": "verdict\\outside.py", "max_lines": 10, "rationale": "unsafe"},
        {"path": "verdict/ok.py", "max_lines": 0, "rationale": "bad limit"},
        {"path": "verdict/ok.py", "max_lines": 10, "rationale": ""},
        {"path": "verdict/ok.py", "max_lines": 10, "rationale": "ok", "extra": True},
    ],
)
def test_malformed_or_unsafe_baseline_entries_are_rejected(
    tmp_path: Path, entry: dict[str, object]
) -> None:
    baseline = tmp_path / "baseline.json"
    _write_baseline(baseline, [entry])

    with pytest.raises(checker.BaselineError):
        checker.load_baseline(baseline)


def test_json_and_text_output_are_deterministic_and_sorted(tmp_path: Path) -> None:
    root = _repository(
        tmp_path, {"verdict/z_last.py": "line\n" * 3, "verdict/a_first.py": "line\n" * 4}
    )

    first = checker.check(root, max_lines=2)
    second = checker.check(root, max_lines=2)

    assert checker.render_json(first) == checker.render_json(second)
    assert checker.render_text(first) == checker.render_text(second)
    assert [finding.path for finding in first.findings] == [
        "verdict/a_first.py",
        "verdict/z_last.py",
    ]
    payload = json.loads(checker.render_json(first))
    assert [finding["path"] for finding in payload["findings"]] == [
        "verdict/a_first.py",
        "verdict/z_last.py",
    ]
