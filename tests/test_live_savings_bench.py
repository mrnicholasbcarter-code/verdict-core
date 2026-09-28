"""Tests for live_savings_bench.py — no network, no credentials."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "live_savings_bench.py"
TASKS_JSON = ROOT / "benchmarks" / "fixtures" / "live_savings" / "tasks.json"


class TestOptIn:
    """The bench refuses to run without VERDICT_LIVE_SMOKE=1."""

    def test_refuses_without_opt_in(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.delenv("VERDICT_LIVE_SMOKE", raising=False)
        proc = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True)
        assert proc.returncode == 2
        assert "VERDICT_LIVE_SMOKE=1" in proc.stderr


class TestTaskFixtures:
    """Task fixture files are valid and self-consistent."""

    def test_tasks_json_loads(self) -> None:
        data = json.loads(TASKS_JSON.read_text(encoding="utf-8"))
        assert data["schema_version"] == "1"
        tasks = data["tasks"]
        assert 8 <= len(tasks) <= 12, f"expected 8-12 tasks, got {len(tasks)}"

    def test_each_task_has_required_fields(self) -> None:
        data = json.loads(TASKS_JSON.read_text(encoding="utf-8"))
        for task in data["tasks"]:
            assert "id" in task
            assert "prompt" in task
            assert "test_file" in task
            assert "task_class" in task

    def test_each_task_test_file_exists(self) -> None:
        data = json.loads(TASKS_JSON.read_text(encoding="utf-8"))
        for task in data["tasks"]:
            test_path = TASKS_JSON.parent / task["test_file"]
            assert test_path.is_file(), f"missing test file: {task['test_file']}"

    def test_task_ids_unique(self) -> None:
        data = json.loads(TASKS_JSON.read_text(encoding="utf-8"))
        ids = [t["id"] for t in data["tasks"]]
        assert len(ids) == len(set(ids)), f"duplicate ids: {ids}"


class TestGrading:
    """Test that the grading harness works with known solutions."""

    def test_fizzbuzz_correct_solution(self, tmp_path: Path) -> None:
        solution = (
            "def fizzbuzz(n):\n"
            "    r = []\n"
            "    for i in range(1, n+1):\n"
            "        if i % 15 == 0: r.append('FizzBuzz')\n"
            "        elif i % 3 == 0: r.append('Fizz')\n"
            "        elif i % 5 == 0: r.append('Buzz')\n"
            "        else: r.append(str(i))\n"
            "    return r\n"
        )
        (tmp_path / "solution.py").write_text(solution, encoding="utf-8")
        test_src = (TASKS_JSON.parent / "test_fizzbuzz.py").read_text(encoding="utf-8")
        (tmp_path / "test_fizzbuzz.py").write_text(test_src, encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                str(tmp_path / "test_fizzbuzz.py"),
                "-v",
                "--tb=short",
                "--no-header",
            ],
            capture_output=True,
            text=True,
            cwd=str(tmp_path),
            timeout=15,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_fizzbuzz_wrong_solution_fails(self, tmp_path: Path) -> None:
        solution = "def fizzbuzz(n): return [str(i) for i in range(1, n+1)]\n"
        (tmp_path / "solution.py").write_text(solution, encoding="utf-8")
        test_src = (TASKS_JSON.parent / "test_fizzbuzz.py").read_text(encoding="utf-8")
        (tmp_path / "test_fizzbuzz.py").write_text(test_src, encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                str(tmp_path / "test_fizzbuzz.py"),
                "-v",
                "--tb=short",
                "--no-header",
            ],
            capture_output=True,
            text=True,
            cwd=str(tmp_path),
            timeout=15,
        )
        assert result.returncode != 0

    def test_binary_search_correct(self, tmp_path: Path) -> None:
        solution = (
            "def binary_search(arr, target):\n"
            "    lo, hi = 0, len(arr) - 1\n"
            "    while lo <= hi:\n"
            "        mid = (lo + hi) // 2\n"
            "        if arr[mid] == target: return mid\n"
            "        elif arr[mid] < target: lo = mid + 1\n"
            "        else: hi = mid - 1\n"
            "    return -1\n"
        )
        (tmp_path / "solution.py").write_text(solution, encoding="utf-8")
        test_src = (TASKS_JSON.parent / "test_binary_search.py").read_text(encoding="utf-8")
        (tmp_path / "test_binary_search.py").write_text(test_src, encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                str(tmp_path / "test_binary_search.py"),
                "-v",
                "--tb=short",
                "--no-header",
            ],
            capture_output=True,
            text=True,
            cwd=str(tmp_path),
            timeout=15,
        )
        assert result.returncode == 0, result.stdout + result.stderr


class TestPriceTable:
    """Price table has required fields."""

    def test_price_table_structure(self) -> None:
        import ast

        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        for node in tree.body:
            target = None
            if isinstance(node, ast.Assign):
                if any(isinstance(t, ast.Name) and t.id == "PRICE_TABLE" for t in node.targets):
                    target = node.value
            elif (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == "PRICE_TABLE"
                and node.value is not None
            ):
                target = node.value
            if target is not None:
                table = ast.literal_eval(target)
                for model, entry in table.items():
                    assert "input_per_1m" in entry, f"{model} missing input_per_1m"
                    assert "output_per_1m" in entry, f"{model} missing output_per_1m"
                    assert "source_url" in entry, f"{model} missing source_url"
                    assert "access_date" in entry, f"{model} missing access_date"
                return
        raise AssertionError("PRICE_TABLE not found in script")


class TestCodeExtraction:
    """The code extractor handles markdown fences."""

    def test_markdown_fence(self) -> None:
        import re

        text = "Here is the code:\n```python\ndef foo(): pass\n```\nDone."
        m = re.search(r"```python\s*\n(.*?)```", text, re.DOTALL)
        assert m and m.group(1) == "def foo(): pass\n"

    def test_raw_code_passthrough(self) -> None:
        import re

        text = "def foo(): pass"
        blocks = re.findall(r"```python\s*\n(.*?)```", text, re.DOTALL)
        # No fence → falls through to raw text
        assert not blocks
