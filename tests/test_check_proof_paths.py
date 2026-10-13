"""scripts/check_proof_paths.py: docs/proof/ changes need a Proof: trailer and a valid receipt."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load() -> ModuleType:
    path = ROOT / "scripts" / "check_proof_paths.py"
    spec = importlib.util.spec_from_file_location("check_proof_paths", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


proof_paths = _load()


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


def _commit(root: Path, message: str) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", message)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    shutil.copytree(ROOT / "docs/proof/demo-run", tmp_path / "docs/proof/demo-run")
    (tmp_path / "README.md").write_text("x\n", encoding="utf-8")
    _commit(tmp_path, "base")
    _git(tmp_path, "branch", "base")
    return tmp_path


def test_unrelated_change_passes(repo: Path) -> None:
    (repo / "README.md").write_text("y\n", encoding="utf-8")
    _commit(repo, "docs")
    assert proof_paths.check(repo, "base") == []


def test_proof_change_without_trailer_fails(repo: Path) -> None:
    (repo / "docs/proof/demo-run/graph.json").write_text("{}\n", encoding="utf-8")
    _commit(repo, "regenerate")
    errors = proof_paths.check(repo, "base")
    assert "without a 'Proof: <path>' line" in errors[0]
    assert errors[1].startswith("docs/proof/demo-run: receipt does not verify")


def test_tampered_bundle_with_trailer_fails_receipt_check(repo: Path) -> None:
    events = repo / "docs/proof/demo-run/events.jsonl"
    events.write_text(
        events.read_text(encoding="utf-8").replace("quota_exhausted", "rate_limited", 1),
        encoding="utf-8",
    )
    _commit(repo, "regenerate\n\nProof: docs/proof/demo-run")
    errors = proof_paths.check(repo, "base")
    assert len(errors) == 1 and "receipt does not verify" in errors[0]


def test_change_outside_named_path_fails(repo: Path) -> None:
    (repo / "docs/proof/other.md").write_text("x\n", encoding="utf-8")
    _commit(repo, "add\n\nProof: docs/proof/demo-run")
    errors = proof_paths.check(repo, "base")
    assert len(errors) == 1 and "docs/proof/other.md" in errors[0]


def test_declared_valid_bundle_passes(repo: Path) -> None:
    (repo / "docs/proof/notes.md").write_text("x\n", encoding="utf-8")
    _commit(repo, "add notes\n\nProof: docs/proof/notes.md")
    assert proof_paths.check(repo, "base") == []


def test_trailer_naming_one_file_still_verifies_its_bundle(repo: Path) -> None:
    events = repo / "docs/proof/demo-run/events.jsonl"
    events.write_text(
        events.read_text(encoding="utf-8").replace("quota_exhausted", "rate_limited", 1),
        encoding="utf-8",
    )
    _commit(repo, "edit\n\nProof: docs/proof/demo-run/events.jsonl")
    errors = proof_paths.check(repo, "base")
    assert errors == [errors[0]] and "docs/proof/demo-run: receipt does not verify" in errors[0]
