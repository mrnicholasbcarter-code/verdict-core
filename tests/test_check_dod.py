"""The executable definition of done (scripts/check_dod.py) rejects false "done"."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUN_URL = "https://github.com/owner/repo/actions/runs/123"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_dod", ROOT / "scripts" / "check_dod.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check_dod = _load()


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def _artifact(root: Path, name: str, text: str) -> dict[str, str]:
    path = root / "docs/proof/dod" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return {"path": f"docs/proof/dod/{name}", "sha256": hashlib.sha256(text.encode()).hexdigest()}


def _run(
    root: Path, sha: str, at: str, name: str, command: str = "gh run view 123"
) -> dict[str, Any]:
    return {
        "at": at,
        "sha": sha,
        "environment": "github-actions",
        "command": command,
        "real_state_marker": RUN_URL,
        "exit_code": 0,
        "raw_output": _artifact(
            root, name, f"{RUN_URL}\ndrill: orphan module FAILED as intended\n"
        ),
    }


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    root = tmp_path / "repo"
    (root / "proof").mkdir(parents=True)
    shutil.copy(ROOT / "proof/dod.schema.json", root / "proof/dod.schema.json")
    archived = root / "openspec/changes/archive/2026-10-13-gate"
    archived.mkdir(parents=True)
    (archived / "tasks.md").write_text("# Tasks\n- [x] build it\n", encoding="utf-8")
    (root / "scripts").mkdir()
    (root / "scripts/tool.py").write_text("print(1)\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "add", "-A")
    _git(root, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-qm", "gate")
    sha = _git(root, "rev-parse", "HEAD")
    evidence = {
        "schema_version": 1,
        "story": "gate",
        "kind": "repo-tooling",
        "author": "worker-a",
        "merged_sha": sha,
        "pr": 1,
        "on_main": _run(root, sha, "2099-01-01T00:00:00Z", "main.txt", "gh pr checks 1 --required"),
        "acceptance": [
            {
                "criterion": "an orphan module fails CI",
                "expect": "FAILED as intended",
                "first_run": _run(root, sha, "2099-01-01T00:00:00Z", "first.txt"),
                "fresh_run": _run(root, sha, "2099-01-02T01:00:00Z", "fresh.txt"),
            }
        ],
        "freshness_window_hours": 24,
        "stays_true_job": "gate-drills.yml (daily)",
        "setup_doctor": {"status": "not_applicable", "reason": "repo tooling, no service"},
        "openspec": {"change": "gate"},
        "parent_confirmation": {
            "by": "parent",
            "at": "2099-01-02T02:00:00Z",
            "command": "gh run view 123",
            "raw_output": _artifact(
                root, "confirm.txt", "drill: orphan module FAILED as intended\n"
            ),
        },
    }
    return root, evidence


def _check(root: Path, evidence: dict[str, Any]) -> list[str]:
    path = root / "docs/proof/dod/gate.json"
    path.write_text(json.dumps(evidence), encoding="utf-8")
    return check_dod.check_evidence(root, path)


def test_complete_evidence_is_done(repo: tuple[Path, dict[str, Any]]) -> None:
    root, evidence = repo
    assert _check(root, evidence) == []


@pytest.mark.parametrize(
    "field", sorted(json.loads((ROOT / "proof/dod.schema.json").read_text())["required"])
)
def test_each_missing_field_fails(repo: tuple[Path, dict[str, Any]], field: str) -> None:
    root, evidence = repo
    del evidence[field]
    assert any(field in error for error in _check(root, evidence))


def _mutate(evidence: dict[str, Any], dotted: str, value: object) -> dict[str, Any]:
    changed = copy.deepcopy(evidence)
    node: Any = changed
    *parents, leaf = dotted.split(".")
    for part in parents:
        node = node[int(part)] if part.isdigit() else node[part]
    node[leaf] = value
    return changed


@pytest.mark.parametrize(
    ("dotted", "value", "reason"),
    [
        ("merged_sha", "0" * 40, "not a commit"),
        ("acceptance.0.first_run.sha", "1" * 40, "not the merged sha or a later commit"),
        ("acceptance.0.first_run.raw_output.sha256", "0" * 64, "does not match its sha256"),
        ("acceptance.0.first_run.real_state_marker", "https://example.invalid/x", "run URL"),
        ("acceptance.0.expect", "something else", "expected text"),
        ("acceptance.0.fresh_run.at", "2099-01-01T12:00:00Z", "freshness window"),
        ("acceptance.0.fresh_run.command", "gh run view 999", "command differs"),
        ("acceptance.0.first_run.exit_code", 1, "exit code 1"),
        ("acceptance.0.first_run.at", "2000-01-01T00:00:00Z", "before the merge"),
        ("on_main.exit_code", 1, "required checks not green"),
        ("parent_confirmation.by", "worker-a", "cannot confirm their own"),
        ("parent_confirmation.command", "echo ok", "not one of the acceptance commands"),
        ("openspec.change", "other", "not in openspec/changes/archive"),
        ("kind", "product", "operator host"),
        ("story", "other", "does not match the file name"),
    ],
)
def test_mismatched_field_fails(
    repo: tuple[Path, dict[str, Any]], dotted: str, value: object, reason: str
) -> None:
    root, evidence = repo
    errors = _check(root, _mutate(evidence, dotted, value))
    assert any(reason in error for error in errors), errors


def test_unticked_task_and_unarchived_change_fail(repo: tuple[Path, dict[str, Any]]) -> None:
    root, evidence = repo
    tasks = root / "openspec/changes/archive/2026-10-13-gate/tasks.md"
    tasks.write_text("# Tasks\n- [ ] not done\n", encoding="utf-8")
    assert any("unticked" in e for e in _check(root, evidence))
    (root / "openspec/changes/gate").mkdir(parents=True)
    assert any("not archived" in e for e in _check(root, evidence))


def test_unredacted_secret_in_raw_output_fails(repo: tuple[Path, dict[str, Any]]) -> None:
    root, evidence = repo
    secret = "api_key=" + "x" * 24
    evidence["parent_confirmation"]["raw_output"] = _artifact(
        root, "confirm.txt", f"drill: orphan module FAILED as intended\n{secret}\n"
    )
    assert any("unredacted credential" in e for e in _check(root, evidence))


def test_repo_tooling_story_must_not_change_product_code(repo: tuple[Path, dict[str, Any]]) -> None:
    root, evidence = repo
    (root / "verdict").mkdir()
    (root / "verdict/x.py").write_text("x = 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-qm", "x")
    sha = _git(root, "rev-parse", "HEAD")
    evidence = _mutate(evidence, "merged_sha", sha)
    errors = _check(root, evidence)
    assert any("changed product code: verdict/x.py" in e for e in errors), errors


def test_all_flags_an_archive_without_evidence(
    repo: tuple[Path, dict[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    root, evidence = repo
    (root / "openspec/changes/archive/2026-10-14-unproven").mkdir(parents=True)
    (root / "openspec/changes/archive/2026-10-01-before-gate").mkdir(parents=True)
    (root / "docs/proof/dod/gate.json").write_text(json.dumps(evidence), encoding="utf-8")
    assert check_dod.main(["--all", "--root", str(root)]) == 1
    out = capsys.readouterr().out
    assert "DONE: " in out
    assert "openspec change unproven was archived without DoD evidence" in out
    assert "before-gate" not in out


def test_repository_evidence_passes() -> None:
    assert check_dod.main(["--all"]) == 0
