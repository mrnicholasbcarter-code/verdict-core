import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path("scripts/verify_release_versions.py")


def test_verifier_has_no_python_311_only_tomllib_dependency():
    source = SCRIPT.read_text(encoding="utf-8")

    assert "tomllib" not in source


def _write_candidate(root: Path, *, python: str, contracts: str, client: str) -> None:
    (root / "contracts").mkdir()
    (root / "verdict/client-sdk").mkdir(parents=True)
    (root / "verdict/__init__.py").write_text(f'__version__ = "{python}"\n', encoding="utf-8")
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "verdict-core"\nversion = "{python}"\n', encoding="utf-8"
    )
    (root / "contracts/package.json").write_text(
        json.dumps({"name": "@bodanglin/verdict-contracts", "version": contracts}), encoding="utf-8"
    )
    (root / "verdict/client-sdk/package.json").write_text(
        json.dumps(
            {
                "name": "@bodanglin/verdict-client",
                "version": client,
                "peerDependencies": {"@bodanglin/verdict-contracts": f"^{contracts}"},
            }
        ),
        encoding="utf-8",
    )


def _verify(root: Path, tag: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT.resolve()), "--root", str(root), "--tag", tag],
        capture_output=True,
        text=True,
    )


def test_accepts_one_synchronized_semantic_version(tmp_path: Path):
    _write_candidate(tmp_path, python="0.2.0", contracts="0.2.0", client="0.2.0")

    result = _verify(tmp_path, "v0.2.0")

    assert result.returncode == 0
    assert "verified synchronized release version: 0.2.0" in result.stdout


def test_rejects_version_drift(tmp_path: Path):
    _write_candidate(tmp_path, python="0.2.0", contracts="0.2.0", client="0.2.1")

    result = _verify(tmp_path, "v0.2.0")

    assert result.returncode != 0
    assert "versions are not synchronized" in result.stderr


def test_rejects_tag_mismatch(tmp_path: Path):
    _write_candidate(tmp_path, python="0.2.0", contracts="0.2.0", client="0.2.0")

    result = _verify(tmp_path, "v0.2.1")

    assert result.returncode != 0
    assert "tag v0.2.1 does not match package version 0.2.0" in result.stderr


def test_rejects_peer_dependency_drift(tmp_path: Path):
    _write_candidate(tmp_path, python="0.2.0", contracts="0.2.0", client="0.2.0")
    client_path = tmp_path / "verdict/client-sdk/package.json"
    client = json.loads(client_path.read_text(encoding="utf-8"))
    client["peerDependencies"]["@bodanglin/verdict-contracts"] = "^0.1.0"
    client_path.write_text(json.dumps(client), encoding="utf-8")

    result = _verify(tmp_path, "v0.2.0")

    assert result.returncode != 0
    assert "client peer dependency must be ^0.2.0" in result.stderr


def test_rejects_runtime_version_drift(tmp_path: Path):
    _write_candidate(tmp_path, python="0.2.0", contracts="0.2.0", client="0.2.0")
    (tmp_path / "verdict/__init__.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")

    result = _verify(tmp_path, "v0.2.0")

    assert result.returncode != 0
    assert "runtime version must be 0.2.0" in result.stderr
