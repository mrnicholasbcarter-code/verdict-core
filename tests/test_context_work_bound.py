"""Native code providers parse only files that can mention the target symbols."""

from pathlib import Path

import pytest

import verdict.context_sources as cs
from verdict.context_sources import NativeCodeProvider, NativeGraphProvider


def _repo(tmp_path: Path, unrelated: int) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "target.py").write_text("def target_fn():\n    return helper()\n")
    (tmp_path / "pkg" / "caller.py").write_text(
        "from pkg.target import target_fn\n\ndef use():\n    return target_fn()\n"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_target.py").write_text("def test_target_fn():\n    pass\n")
    for i in range(unrelated):
        (tmp_path / "pkg" / f"other_{i}.py").write_text(f"def other_{i}():\n    return {i}\n")
    skipped = tmp_path / "node_modules" / "dep"
    skipped.mkdir(parents=True)
    (skipped / "target_copy.py").write_text("def target_fn():\n    pass\n")
    return tmp_path


@pytest.fixture
def parsed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    real = cs._extract_definitions

    def counting(path: Path, root: Path, symbols):  # type: ignore[no-untyped-def]
        seen.append(path.name)
        return real(path, root, symbols)

    monkeypatch.setattr(cs, "_extract_definitions", counting)
    return seen


def test_definitions_parse_only_files_naming_the_symbol(tmp_path: Path, parsed: list[str]) -> None:
    repo = _repo(tmp_path, unrelated=200)
    result = NativeCodeProvider().provide(
        capability_id="code.definitions", query="", repo_root=repo, hints={"symbols": ["target_fn"]}
    )
    assert [u.key for u in result.units] == ["def:pkg/target.py:target_fn:1"]
    assert sorted(parsed) == ["caller.py", "target.py", "test_target.py"]
    assert result.gap_reason is None


def test_graph_still_finds_callers_and_tests_case_insensitively(tmp_path: Path) -> None:
    repo = _repo(tmp_path, unrelated=50)
    (repo / "tests" / "test_target.py").write_text("def test_Target_Fn_contract():\n    pass\n")
    result = NativeGraphProvider().provide(
        capability_id="tests.impact", query="", repo_root=repo, hints={"symbols": ["target_fn"]}
    )
    text = result.units[0].content
    assert "test_Target_Fn_contract" in text
    assert "pkg/caller.py" in text


def test_scan_bound_is_reported_not_silent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path, unrelated=30)
    monkeypatch.setattr(cs, "MAX_SCAN_FILES", 5)
    result = NativeCodeProvider().provide(
        capability_id="code.definitions", query="", repo_root=repo, hints={"symbols": ["target_fn"]}
    )
    assert result.gap_reason == "traversal_bounded"


def test_bound_counts_python_files_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path, unrelated=0)
    for i in range(40):
        (repo / "pkg" / f"data_{i}.json").write_text("{}")
    monkeypatch.setattr(cs, "MAX_SCAN_FILES", 10)
    result = NativeCodeProvider().provide(
        capability_id="code.definitions", query="", repo_root=repo, hints={"symbols": ["target_fn"]}
    )
    assert result.gap_reason is None
    assert [u.key for u in result.units] == ["def:pkg/target.py:target_fn:1"]


def test_prefilter_reads_a_bounded_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    big = tmp_path / "big.py"
    big.write_text("x = 1\n" * 1000 + "target_fn()\n")
    monkeypatch.setattr(cs, "MAX_SCAN_FILE_BYTES", 100)
    assert not cs._mentions(big, (b"target_fn",))
    monkeypatch.setattr(cs, "MAX_SCAN_FILE_BYTES", 1 << 20)
    assert cs._mentions(big, (b"target_fn",))


def test_matched_files_are_bounded_by_count_and_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BOD-431 (E): with cwd=/tmp as the workspace, plain words matched 6,835 files
    (135 MB). Each one was parsed and cached as an AST (~35x the source), and serve
    was OOM-killed. The matched set is capped by count and bytes and reported."""
    for i in range(30):
        (tmp_path / f"m_{i}.py").write_text(f"def target_fn_{i}():\n    return 'target_fn'\n")
    monkeypatch.setattr(cs, "MAX_MATCHED_FILES", 10)
    files, bounded = cs._iter_py_files(tmp_path, None, ("target_fn",))
    assert len(files) == 10
    assert bounded is True

    monkeypatch.setattr(cs, "MAX_MATCHED_FILES", 1_000)
    one = (tmp_path / "m_0.py").stat().st_size
    monkeypatch.setattr(cs, "MAX_MATCHED_BYTES", one * 5)
    files, bounded = cs._iter_py_files(tmp_path, None, ("target_fn",))
    assert sum(p.stat().st_size for p in files) <= one * 5
    assert bounded is True


def test_oversized_file_is_skipped_by_size_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "small.py").write_text("def target_fn():\n    pass\n")
    (tmp_path / "huge.py").write_text("def target_fn():\n    pass\n" + "x = 1\n" * 1000)
    monkeypatch.setattr(cs, "MAX_SCAN_FILE_BYTES", 100)
    opened: list[str] = []
    real = cs._mentions

    def spy(path: Path, needles: tuple[bytes, ...]) -> bool:
        opened.append(path.name)
        return real(path, needles)

    monkeypatch.setattr(cs, "_mentions", spy)
    files, bounded = cs._iter_py_files(tmp_path, None, ("target_fn",))
    assert [p.name for p in files] == ["small.py"]
    assert "huge.py" not in opened
    assert bounded is False
